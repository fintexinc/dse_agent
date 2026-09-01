"""WSB-E3-T4 — human review loop: `changes_requested` goes back to the Coder on
the SAME branch/PR (it does not recreate the sandbox or the PR), re-validates
L1, re-finalizes the SAME PR; `approved` only becomes Done after a second
explicit `merged_by_human` signal. No path in this file (or in the workflow)
calls merge — that is checked statically in `test_no_automatic_merge_path`.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


@pytest.mark.asyncio
async def test_changes_requested_cycles_back_to_coder_same_pr(time_skipping_env):
    work_item_id = new_work_item_id("cr")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane()
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )
        await wait_for_status(handle, {"review_ready"})
        first_pr_number = (await handle.query(WorkItemLifecycleWorkflow.get_state))["pr_number"]

        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "tweak X"})
        await asyncio.sleep(0.2)  # let the fix cycle run
        await wait_for_status(handle, {"review_ready"})

        second_pr_number = (await handle.query(WorkItemLifecycleWorkflow.get_state))["pr_number"]
        assert second_pr_number == first_pr_number  # SAME PR, never recreated

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.provision_calls == 1  # sandbox never reprovisioned
    assert state.finalize_calls == 2  # 1 initial + 1 re-finalize after changes_requested
    assert state.coder_turn_calls == 2  # 1 initial + 1 in the fix cycle

    actions = read_audit_actions(work_item_id)
    assert "changes_requested" in actions
    assert "coder_fix_applied" in actions
    assert "pr_refinalized" in actions
    assert "merged_by_human" in actions


@pytest.mark.asyncio
async def test_approved_waits_for_explicit_merge_signal(time_skipping_env):
    """Proves that `approved` alone is NOT enough — the workflow only finishes
    after `merged_by_human` (P3: no agent session merges its own work; only a
    human, via an explicit signal, triggers Done)."""
    work_item_id = new_work_item_id("waitmerge")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(FakeControlPlane())

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})

        # Confirm (via query/audit, NOT via handle.result() with a timeout —
        # the time-skipping environment would advance the virtual clock to the
        # 10-year default as soon as it went idle waiting on just a signal,
        # which would make `result()` "complete" prematurely on an execution
        # timeout instead of proving what we want) that the workflow is parked
        # on `approved_awaiting_merge`, with NO automatic merge.
        actions_before: list[str] = []
        for _ in range(100):
            actions_before = read_audit_actions(work_item_id)
            if "approved_awaiting_merge" in actions_before:
                break
            await asyncio.sleep(0.05)
        assert "approved_awaiting_merge" in actions_before
        assert "merged_by_human" not in actions_before
        assert "done" not in actions_before
        state = await handle.query(WorkItemLifecycleWorkflow.get_state)
        assert state["status"] == "merge_pending"

        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value


def test_no_automatic_merge_path_in_source():
    """Statically auditable (grep): no call to the GitHub merge API anywhere in
    the WS-B workflow/worker code."""
    # Real CALL patterns (not prose in a comment/docstring): `.merge(`,
    # `merge_pull_request(`, or a quoted `/merge` API URL.
    src_dir = Path(__file__).resolve().parent.parent / "src" / "dse_orchestrator"
    pattern = re.compile(r"\.merge\s*\(|merge_pull_request\s*\(|[\"']\S*/merge[\"']")
    offenders = []
    for path in src_dir.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue  # comments/docstrings may talk ABOUT not merging
            if pattern.search(line):
                offenders.append(f"{path}:{lineno}: {stripped}")
    assert not offenders, f"found a possible automatic merge call: {offenders}"


