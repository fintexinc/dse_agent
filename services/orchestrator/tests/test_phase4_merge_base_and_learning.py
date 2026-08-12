"""Phase 4 (WS-B wiring) — three deliverables of loop hardening & learning:

1. **Merge-base in the review loop (WSE-E6-T16).** On the changes_requested
   path, BEFORE re-running the Coder, the workflow calls
   ACTIVITY_UPDATE_BASE_BRANCH with `first_human_review_done=True` (the human
   already reviewed -> never rebase, only merge-base -> zero orphaned threads).
   An unresolvable conflict -> escalate to a human (never force a resolution,
   P6). The fakes decode the payload with the REAL contract MODEL
   (UpdateBaseBranchInput) and return UpdateBaseBranchResult — if the call site
   drifts from the contract, it breaks here.

2. **Clarification episode (WS-C source, WSC-E4-T2).** The clarification gate
   emits a skill_episode (source=clarification) into the migration 0019 table
   when the SAME gap recurs — NO skill is created here (boundary tested in
   packages/contracts), only the governable input.

3. **PR quality metric (pilot gate).** review rounds, changes_requested count,
   time to merge and evidence refreshes are emitted via OTel at the PR's
   terminal boundary (merge/escalation).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from temporalio.worker import Worker

from dse_contracts.constants import OTEL_ATTR_TENANT, OTEL_ATTR_WORK_ITEM
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator import metrics
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import (
    wait_for_status,
    insert_work_item,
    new_work_item_id,
    read_audit_actions,
    read_skill_episodes,
)
from fakes import FakeControlPlane, build_fake_activities


async def _wait_until(predicate, attempts: int = 400, msg: str = "condition never became true"):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(msg)


def _wf_input(work_item_id: str, **kw) -> WorkItemLifecycleInput:
    return WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit", **kw,
    )


def _collect_points(reader: InMemoryMetricReader, metric_name: str):
    data = reader.get_metrics_data()
    points = []
    if data is None:
        return points
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == metric_name:
                    points.extend(metric.data.data_points)
    return points


# ---------------------------------------------------------------------------
# 1) Merge-base on the changes_requested path (WSE-E6-T16)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_changes_requested_runs_merge_base_before_coder(time_skipping_env):
    work_item_id = new_work_item_id("mb")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane()  # base_has_drift=True by default -> merge_base strategy
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})

        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "tweak X"})
        await _wait_until(lambda: state.update_base_calls >= 1,
                          msg="merge-base never ran on changes_requested")
        await wait_for_status(handle, {"review_ready"})

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    # merge-base ran exactly once (one batch of changes_requested)
    assert state.update_base_calls == 1
    # BEFORE the Coder: the payload carries first_human_review_done=True (never rebase)
    assert state.last_update_base_payload["first_human_review_done"] is True
    assert state.last_update_base_payload["branch"] == f"dse/{work_item_id}"
    assert state.last_update_base_payload["base_branch"] == "main"

    actions = read_audit_actions(work_item_id)
    assert "base_branch_updated" in actions
    # order: merge-base BEFORE the Coder's fix (P8 evidence, not assertion)
    assert actions.index("base_branch_updated") < actions.index("coder_fix_applied")


@pytest.mark.asyncio
async def test_merge_base_conflict_escalates_and_does_not_rerun_coder(time_skipping_env):
    """An unresolvable merge-base conflict -> escalate to a human; the Coder is
    NOT re-run (P6: never force a resolution, never keep guessing)."""
    work_item_id = new_work_item_id("mbconf")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(base_conflict=True)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "rebase"})
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    assert "base_branch_merge_conflict" in (result.detail or "")
    assert state.update_base_calls == 1
    # the fix cycle's Coder NEVER ran (only the implementation's initial turn)
    assert state.coder_turn_calls == 1
    actions = read_audit_actions(work_item_id)
    assert "base_branch_updated" in actions
    assert "coder_fix_applied" not in actions
    assert "escalated" in actions


@pytest.mark.asyncio
async def test_update_base_branch_failure_is_bounded_and_lands_in_the_ledger(time_skipping_env):
    """Medido em produção (wi_a8b760de, 2026-08-12): uma falha PERMANENTE do
    update_base_branch (workspace inexistente no pod) retentou por HORAS — o
    call site não tinha retry_policy, então valia o retry infinito default do
    Temporal até o schedule_to_close estourar. E o estouro é o pior desfecho
    possível: ActivityError não é capturado pelo run(), o workflow morre
    Failed no Temporal SEM linha no ledger e o status congela em
    review_feedback — para quem olha, o item simplesmente parou.

    Com o cap: a falha vira _ActivityRetriesExhausted → _finish_failed
    auditado. Item que não anda termina DIZENDO POR QUÊ."""
    work_item_id = new_work_item_id("mbboom")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(update_base_fail_times=99)  # falha permanente
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "fix"})
        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value, (
        f"a falha permanente tem de terminar o item, não pendurá-lo: {result.status!r}"
    )
    assert "activity_retries_exhausted" in (result.detail or "")
    assert "update_base_branch" in (result.detail or "")
    assert state.update_base_calls == 3, (
        f"o cap (activity_retry_cap=3) limita as tentativas — houve "
        f"{state.update_base_calls}, e infinito era exatamente o defeito"
    )
    actions = read_audit_actions(work_item_id)
    assert "activity_retries_exhausted" in actions, "o desfecho chega ao ledger"
    assert "coder_fix_applied" not in actions, "o fix do Coder nunca chegou a rodar"


@pytest.mark.asyncio
async def test_merge_base_orphaned_threads_violation_escalates(time_skipping_env):
    """Phase 4 exit invariant: merge-base NEVER orphans threads. If the owner
    (WS-E) reports orphaned_threads>0, the workflow escalates (it never proceeds
    with the invariant violated)."""
    work_item_id = new_work_item_id("mborph")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(base_orphaned_threads=2)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "x"})
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    assert "orphaned_threads" in (result.detail or "")
    assert state.coder_turn_calls == 1  # the fix cycle never ran


# ---------------------------------------------------------------------------
# 2) Recurring clarification episode (WS-C source, WSC-E4-T2)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recurring_clarification_gap_emits_skill_episode(time_skipping_env):
    """The same gap (base_branch/acceptance_criteria still missing after we
    already asked for clarification) produces a skill_episode
    source=clarification with provenance — the input WS-C consumes. NO skill is
    created here."""
    work_item_id = new_work_item_id("clarep")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(FakeControlPlane())

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo=None, base_branch=None, acceptance_criteria=None,
            clarification_round_cap=3,
            clarification_reminder_hours=1.0,
            clarification_escalation_days=10.0,  # keeps the escalation timer from firing
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        # always answers only the repo -> base_branch/acceptance_criteria RECUR as missing
        for _ in range(3):
            await wait_for_status(handle, {"needs_clarification"})
            await handle.signal("clarification_answer", {"repo": "acme/repo"})
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value  # round cap

    episodes = read_skill_episodes(work_item_id)
    assert episodes, "no skill_episode recorded for the recurring gap"
    sources = {e[0] for e in episodes}
    assert sources == {"clarification"}
    # pattern_key groups the recurring fields; provenance carries the context
    source, pattern_key, occurrence_n, provenance = episodes[0]
    assert pattern_key.startswith("clarification_missing:")
    assert "base_branch" in pattern_key and "acceptance_criteria" in pattern_key
    assert occurrence_n >= 1
    assert provenance["requester"] == "usr_test"
    assert set(provenance["recurring_missing"]) == {"base_branch", "acceptance_criteria"}

    actions = read_audit_actions(work_item_id)
    assert "skill_episode_recorded" in actions


@pytest.mark.asyncio
async def test_non_recurring_clarification_emits_no_episode(time_skipping_env):
    """The FIRST gap (initial round, before any request) NEVER becomes an
    episode — only the RECURRENCE counts. A clarification answered on the first
    try produces no input."""
    work_item_id = new_work_item_id("clarno")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(FakeControlPlane())

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo=None, base_branch=None, acceptance_criteria=None,
            clarification_reminder_hours=0.001, clarification_escalation_days=0.001,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"needs_clarification"})
        # answers EVERYTHING at once -> completes without a recurrence
        await handle.signal("clarification_answer",
                            {"repo": "acme/repo", "base_branch": "main", "acceptance_criteria": "X"})
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert read_skill_episodes(work_item_id) == []
    assert "skill_episode_recorded" not in read_audit_actions(work_item_id)


# ---------------------------------------------------------------------------
# 3) PR quality metric (pilot gate "PR quality thresholds")
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pr_quality_metric_emitted_on_merge(time_skipping_env):
    reader = InMemoryMetricReader()
    metrics.configure_for_tests(reader)

    work_item_id = new_work_item_id("prq")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(coder_files_changed=["frontend/App.tsx"])  # produces evidence/refresh
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        # one changes_requested round -> counts toward the rate + 1 evidence refresh
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "y"})
        await _wait_until(lambda: state.finalize_calls >= 2, msg="the fix cycle never completed")
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value

    rounds = _collect_points(reader, metrics.METRIC_PR_REVIEW_ROUNDS)
    mine = [p for p in rounds if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert mine, "no review_rounds data point for the PR"
    attrs = dict(mine[0].attributes)
    assert attrs[OTEL_ATTR_TENANT] == "test-tenant"
    assert attrs[metrics.ATTR_PR_OUTCOME] == "merged"
    # review_round counts the fix cycles (changes_requested/ci-red); 1 here
    assert max(p.max for p in mine) >= 1

    cr = _collect_points(reader, metrics.METRIC_PR_CHANGES_REQUESTED)
    cr_mine = [p for p in cr if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert cr_mine and max(p.max for p in cr_mine) >= 1

    ttm = _collect_points(reader, metrics.METRIC_PR_TIME_TO_MERGE)
    ttm_mine = [p for p in ttm if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert ttm_mine, "time-to-merge was never emitted on merge"

    ev = _collect_points(reader, metrics.METRIC_PR_EVIDENCE_REFRESHES)
    ev_mine = [p for p in ev if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert ev_mine  # evidence-consumption (proxy) emitted


@pytest.mark.asyncio
async def test_pr_quality_metric_emitted_on_escalation(time_skipping_env):
    """PRs that never merge (escalated after the PR exists) also emit the metric
    — the pilot gate needs data from both outcomes."""
    reader = InMemoryMetricReader()
    metrics.configure_for_tests(reader)

    work_item_id = new_work_item_id("prqesc")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane()
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _wf_input(work_item_id, review_round_cap=1, coder_retry_cap=99),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "r1"})
        await _wait_until(lambda: state.finalize_calls >= 2, msg="round 1 never completed")
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "r2"})
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    rounds = _collect_points(reader, metrics.METRIC_PR_REVIEW_ROUNDS)
    mine = [p for p in rounds if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert mine and dict(mine[0].attributes)[metrics.ATTR_PR_OUTCOME] == "escalated"
