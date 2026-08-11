"""Phase 3 — evidence pipeline wiring (WS-B calls the CONTRACT Activities after
finalize_pr):

  trigger_preview (files_changed from CoderTurnResult, paths-filter FR-20)
    -> if created: run_demo_evidence (preview base_url; publish is internal)
    -> run_visual_diff when there is media/a screenshot.

Failure mode 9: a degraded preview (status "degraded" OR the Activity failing
outright) NEVER blocks the PR — degraded evidence is recorded (audit +
work_item_evidence projection, migration 0014) and the flow moves on to human
review.

The WS-E fakes (tests/fakes.py) decode every payload with the REAL contract
models (TriggerPreviewInput/RunDemoEvidenceInput/RunVisualDiffInput) — a payload
that drifts from the contract breaks HERE, not on the wire (lesson from addendum
02). Real Postgres/Temporal, never mocked.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.activities import (
    RunDemoEvidenceInput,
    RunVisualDiffInput,
    TriggerPreviewInput,
)
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import (
    wait_for_status,
    insert_work_item,
    new_work_item_id,
    read_audit_actions,
    read_evidence_row,
)
from fakes import FakeControlPlane, build_fake_activities


def _wf_input(work_item_id: str, **kw) -> WorkItemLifecycleInput:
    return WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit", **kw,
    )


@pytest.mark.asyncio
async def test_ui_touching_pr_runs_full_evidence_pipeline(time_skipping_env):
    """PR that touches UI: trigger_preview -> created -> run_demo_evidence with
    the preview base_url -> run_visual_diff (1st run creates the baseline).
    Payloads validated by the real models inside the fakes AND re-validated here."""
    work_item_id = new_work_item_id("evid")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_files_changed=["frontend/App.tsx", "api/handler.py"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.trigger_preview_calls == 1
    assert state.demo_evidence_calls == 1
    assert state.visual_diff_calls == 1

    # order: evidence ONLY after the PR is finalized (never blocks the finalize)
    log = state.calls_log
    assert log.index("finalize_pr") < log.index("trigger_preview")
    assert log.index("trigger_preview") < log.index("run_demo_evidence")
    assert log.index("run_demo_evidence") < log.index("run_visual_diff")

    # exact payloads re-validated with the CONTRACT MODELS (not a lenient dict)
    prev = TriggerPreviewInput(**state.last_preview_payload)
    # A ORDEM continua asserida: desde 2026-08-11 o preview recebe o diff
    # ACUMULADO (`cumulative_files_changed`), que é `sorted()` — determinístico.
    # Trocar por comparação de conjunto perderia a asserção de forma de graça.
    assert prev.files_changed == ["api/handler.py", "frontend/App.tsx"]
    assert prev.pr_number == 1000
    demo = RunDemoEvidenceInput(**state.last_demo_payload)
    assert demo.base_url == f"http://preview-{work_item_id}.local"  # URL of the created preview
    vd = RunVisualDiffInput(**state.last_visual_diff_payload)
    assert vd.base_screenshot_key is None  # 1st run -> baseline
    assert vd.candidate_screenshot_path == f"demos/{work_item_id}/screenshot.png"

    actions = read_audit_actions(work_item_id)
    for a in ("preview_triggered", "demo_evidence_completed", "visual_diff_completed"):
        assert a in actions

    row = read_evidence_row(work_item_id)
    assert row is not None
    preview_status, preview_url, demo_passed, video_key, _, baseline_key, refresh_count, reason, detail = row
    assert preview_status == "created"
    assert demo_passed is True
    assert video_key == "evidence/demo.webm"
    assert baseline_key == f"evidence/{work_item_id}/visual.png"
    assert refresh_count == 0 and reason == "initial"


@pytest.mark.asyncio
async def test_docs_only_pr_skips_preview_deterministically(time_skipping_env):
    """FR-20 + §D: a docs-only PR (neither UI nor a deployable service) ->
    skipped_backend_only (pure paths-filter, P1), counts as success, does NOT
    run demo/visual diff and blocks nothing."""
    work_item_id = new_work_item_id("evidskip")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_files_changed=["docs/x.md", "README.md"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.trigger_preview_calls == 1
    assert state.demo_evidence_calls == 0
    assert state.visual_diff_calls == 0
    actions = read_audit_actions(work_item_id)
    assert "evidence_skipped_backend_only" in actions
    row = read_evidence_row(work_item_id)
    assert row is not None and row[0] == "skipped_backend_only"


@pytest.mark.asyncio
async def test_backend_service_pr_now_previews_and_posts_link(time_skipping_env):
    """Plan 08 §D (D1+D2): a backend service PR (.py) is NO LONGER skipped — it
    gets a preview (kind=deployable) and the LINK is posted
    (preview_link_posted in the ledger). With no binding on the tenant, the
    deploys_preview gate is fail-open."""
    work_item_id = new_work_item_id("evidback")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_files_changed=["wallet/service.py"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.demo_evidence_calls == 1  # backend now runs the pipeline
    actions = read_audit_actions(work_item_id)
    assert "preview_triggered" in actions
    assert "preview_link_posted" in actions  # D1 — link posted on the PR
    row = read_evidence_row(work_item_id)
    assert row is not None and row[0] == "created"


@pytest.mark.asyncio
async def test_degraded_preview_does_not_block_pr(time_skipping_env):
    """Failure mode 9: a degraded preview -> degraded evidence is RECORDED and
    the flow moves on to human review all the way to Done. The PR is never
    blocked."""
    work_item_id = new_work_item_id("eviddeg")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low", preview_mode="degraded",
                             coder_files_changed=["ui/page.tsx"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        # reaches pr_ready EVEN with a degraded preview
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value  # never Failed because of evidence
    assert state.demo_evidence_calls == 0  # degraded does not attempt the demo
    actions = read_audit_actions(work_item_id)
    assert "evidence_degraded" in actions
    assert "pr_finalized" in actions
    row = read_evidence_row(work_item_id)
    assert row is not None and row[0] == "degraded"


@pytest.mark.asyncio
async def test_preview_activity_crash_degrades_not_blocks(time_skipping_env):
    """The preview Activity failing OUTRIGHT (e.g. Argo CD down) is also failure
    mode 9: it audits evidence_degraded and moves on — it never propagates the
    failure into the PR lifecycle."""
    work_item_id = new_work_item_id("evidcrash")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low", preview_mode="raise",
                             coder_files_changed=["ui/page.tsx"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "evidence_degraded" in actions
    row = read_evidence_row(work_item_id)
    assert row is not None
    assert row[0] == "degraded" and row[8] == "trigger_preview_failed"
