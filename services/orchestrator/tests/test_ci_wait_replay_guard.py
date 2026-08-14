"""The two CI-wait patch markers, proven against a real pre-patch history.

`ci-poll-writes-only-on-change-v1` REMOVES commands from the CI wait (a status
write and two audit rows per pending poll) and `ci-wait-continue-as-new-v1` ADDS
one (the continue_as_new). Both directions are nondeterminism if they reach a
history that recorded the other shape — and the histories at stake are not
hypothetical: two executions were live inside this loop with more than 30,000
events each when the change was written.

Such a history cannot be faked, so it is MANUFACTURED the way
`test_tester_infra_outcomes.py` and `test_plan_approval_timeout.py` already do
it: the real workflow issues the real commands with `workflow.patched()`
returning False for exactly these two ids — and recording no marker — while
every other patch id keeps its genuine behaviour. The same history is then
replayed against the CURRENT definition:

  - as shipped -> neither marker is found, both branches stay on the old path,
    replay PASSES;
  - with either guard bypassed (the code you get by deleting its `if`) -> the
    loop stops issuing commands the history contains, or issues one it does not,
    and replay FAILS as nondeterministic.

Runs WITHOUT Postgres on purpose, like the two files above: nothing here touches
the DB boundary, and a replay guard whose test nobody can run on a laptop is a
replay guard that rots — which is the failure this whole change is about.
"""
from __future__ import annotations

import base64
import json
import uuid
from collections import Counter
from typing import Any

import pytest
import temporalio.workflow
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from dse_contracts.activities import ACTIVITY_EMIT_AUDIT, ACTIVITY_POST_TRACKING_COMMENT
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import (
    LOCAL_ACTIVITY_POST_STATUS_TRANSITION,
    LOCAL_ACTIVITY_RECORD_GATE,
    LOCAL_ACTIVITY_UPDATE_STATUS,
    check_clarification_completeness,
    emit_history_metric,
    emit_pr_quality_metric,
    resolve_budget_cap,
)
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import new_work_item_id
from fakes import FakeControlPlane, build_fake_activities

_QUIET_POLL_PATCH = "ci-poll-writes-only-on-change-v1"
_CAN_PATCH = "ci-wait-continue-as-new-v1"
_NEW_PATCHES = (_QUIET_POLL_PATCH, _CAN_PATCH)

#: Pending polls the manufactured history contains before the cap ends the wait.
#: Small on purpose — the shape of a pass is what replay checks, not how many.
_POLL_CAP = 6


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides conftest's autouse skip — see the module docstring."""
    yield


def _db_free_activities(state: FakeControlPlane) -> list[Any]:
    async def emit_audit_event(payload: dict[str, Any]) -> None:
        return None

    async def update_work_item_status(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def record_plan_approval(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def post_tracking_comment(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    async def post_status_transition(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "target_status": None}

    async def check_group_plan_gate(payload: dict[str, Any]) -> dict[str, Any]:
        # rc.93: a barreira de grupo pergunta aqui depois do gate próprio —
        # item sem grupo nunca segura (e activity ausente = retry storm).
        return {"in_group": False, "holding": False, "abort": False, "reason": ""}

    return [
        activity.defn(name=ACTIVITY_EMIT_AUDIT)(emit_audit_event),
        activity.defn(name=ACTIVITY_POST_TRACKING_COMMENT)(post_tracking_comment),
        activity.defn(name=LOCAL_ACTIVITY_UPDATE_STATUS)(update_work_item_status),
        activity.defn(name=LOCAL_ACTIVITY_RECORD_GATE)(record_plan_approval),
        activity.defn(name=LOCAL_ACTIVITY_POST_STATUS_TRANSITION)(post_status_transition),
        activity.defn(name="check_group_plan_gate")(check_group_plan_gate),
        # Real ones: pure, env-only or OTel-only.
        check_clarification_completeness,
        resolve_budget_cap,
        emit_history_metric,
        emit_pr_quality_metric,
    ] + build_fake_activities(state)


def _wf_input(work_item_id: str, **overrides: Any) -> WorkItemLifecycleInput:
    base = dict(
        work_item_id=work_item_id,
        tenant_id="test-tenant",
        requester="usr_test",
        repo="acme/repo",
        base_branch="main",
        acceptance_criteria="crit",
        ci_poll_interval_seconds=0.01,
        ci_pending_poll_cap=_POLL_CAP,
        # the wall clock is not what is under test, and it would end the wait
        # before the cap does under time-skipping.
        ci_wait_deadline_hours=0,
        # ONE event: every pass of the wait is over the threshold, so a
        # continue_as_new that is not held back by its guard fires immediately.
        # This is what makes the negative half below sharp.
        history_continue_as_new_threshold=1,
    )
    base.update(overrides)
    return WorkItemLifecycleInput(**base)


async def _history_events(client, work_item_id: str) -> list[dict[str, Any]]:
    from google.protobuf.json_format import MessageToDict

    history = await client.get_workflow_handle(work_item_id).fetch_history()
    return [MessageToDict(event) for event in history.events]


def _patch_marker(events: list[dict[str, Any]], patch_id: str) -> dict[str, Any] | None:
    """The `workflow.patched()` marker as recorded: markerName `core_patch`,
    with the patch id inside a base64 JSON payload."""
    for event in events:
        attrs = event.get("markerRecordedEventAttributes") or {}
        if attrs.get("markerName") != "core_patch":
            continue
        payloads = (attrs.get("details") or {}).get("patch-data", {}).get("payloads") or []
        for payload in payloads:
            if json.loads(base64.b64decode(payload["data"])).get("id") == patch_id:
                return event
    return None


def _scheduled(events: list[dict[str, Any]]) -> Counter:
    counts: Counter = Counter()
    for event in events:
        attrs = event.get("activityTaskScheduledEventAttributes") or {}
        name = (attrs.get("activityType") or {}).get("name")
        if name:
            counts[name] += 1
    return counts


async def _replay(events: list[dict[str, Any]], work_item_id: str) -> None:
    await Replayer(
        workflows=[WorkItemLifecycleWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
        data_converter=pydantic_data_converter,
    ).replay_workflow(WorkflowHistory.from_json(work_item_id, {"events": events}))


@pytest.mark.asyncio
async def test_a_pre_patch_ci_wait_replays_only_because_of_the_two_guards(
    time_skipping_env, monkeypatch
):
    """THE replay-safety test for this change: it fails if either
    `workflow.patched()` is removed."""
    work_item_id = new_work_item_id("ciwait-prepatch")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # Never green: the wait ends on the poll cap, so the manufactured history is
    # a pure CI wait that closes itself without a human signal.
    state = FakeControlPlane(ci_sequence=["pending"] * (_POLL_CAP + 2))
    real_patched = temporalio.workflow.patched

    def patched_before_the_deploy(patch_id: str) -> bool:
        if patch_id in _NEW_PATCHES:
            return False  # and, crucially, records no marker
        return real_patched(patch_id)

    monkeypatch.setattr(temporalio.workflow, "patched", patched_before_the_deploy)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      workflow_runner=UnsandboxedWorkflowRunner(),
                      activities=_db_free_activities(state)):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )

    assert result.status == WorkItemStatus.escalated.value
    assert "ci_pending_poll_cap_exhausted" in (result.detail or "")

    events = await _history_events(time_skipping_env.client, work_item_id)
    for patch_id in _NEW_PATCHES:
        assert _patch_marker(events, patch_id) is None, f"not a pre-patch history: {patch_id}"

    # The recorded shape IS the old one, which is what makes replaying it a
    # test of anything: a status write and two audit rows per pending poll,
    # and NO continue_as_new even though every pass was over the threshold.
    scheduled = _scheduled(events)
    assert scheduled["consume_ci_status"] == _POLL_CAP
    assert scheduled["update_work_item_status"] >= 2 * _POLL_CAP
    assert scheduled["emit_audit_event"] >= 2 * _POLL_CAP
    assert not [e for e in events if e.get("workflowExecutionContinuedAsNewEventAttributes")]

    # As shipped: neither marker is in the history, both guards read False, the
    # loop reproduces the old command sequence one for one.
    monkeypatch.setattr(temporalio.workflow, "patched", real_patched)
    await _replay(events, work_item_id)

    # The negative half. Without it this file would pass with either guard
    # deleted — which is the mistake that would take the two live executions
    # with it.
    for bypassed in _NEW_PATCHES:
        def guard_deleted(patch_id: str, _bypassed: str = bypassed) -> bool:
            return True if patch_id == _bypassed else real_patched(patch_id)

        monkeypatch.setattr(temporalio.workflow, "patched", guard_deleted)
        with pytest.raises(Exception) as excinfo:
            await _replay(events, work_item_id)
        assert "nondeterminism" in str(excinfo.value).lower(), (
            f"replaying a pre-patch CI wait with {bypassed} bypassed must be "
            f"rejected as nondeterministic, got: {excinfo.value!r}"
        )


@pytest.mark.asyncio
async def test_the_guard_only_reaches_runs_that_enter_the_wait_after_the_deploy(
    time_skipping_env, monkeypatch
):
    """The other half of the guard's contract, and the part the runbook needs.

    `workflow.patched()` MEMOIZES its first answer for the whole execution
    (temporalio `_workflow_instance.workflow_patch`), so an execution that
    replayed even ONE pass of this loop before the deploy answers False for the
    rest of its life — it stays on the old per-poll cost and still dies at the
    ceiling. It is NOT rescued by shipping this; it has to be reset.

    Asserted on the definition itself rather than argued in a comment: a run
    that reaches the wait for the first time with the markers available records
    both, polls quietly and continues as new — the exact opposite of the run
    above, which was identical except for what `patched()` answered.
    """
    work_item_id = new_work_item_id("ciwait-postdeploy")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(ci_sequence=["pending"] * (_POLL_CAP + 2))

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      workflow_runner=UnsandboxedWorkflowRunner(),
                      activities=_db_free_activities(state)):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )

    assert result.status == WorkItemStatus.escalated.value
    events = await _history_events(time_skipping_env.client, work_item_id)
    for patch_id in _NEW_PATCHES:
        assert _patch_marker(events, patch_id) is not None, f"{patch_id} was never recorded"

    # It closed the run instead of growing the history. `events` is the LAST run
    # of the chain, so the proof is on its start event: it was continued, not
    # started fresh, and it carries a single CI poll instead of the whole wait.
    started = [e["workflowExecutionStartedEventAttributes"] for e in events
               if e.get("workflowExecutionStartedEventAttributes")][0]
    assert started.get("continuedExecutionRunId"), "the wait never continued as new"
    assert _scheduled(events)["consume_ci_status"] < _POLL_CAP
    # ... and the counter that ends the wait crossed every hop: the cap is what
    # escalated it, so all _POLL_CAP polls were still counted.
    assert f"ci_pending_poll_cap_exhausted:{_POLL_CAP}" in (result.detail or "")
