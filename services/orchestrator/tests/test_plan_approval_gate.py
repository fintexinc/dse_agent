"""WSB-E3-T2/T3 — plan approval gate by risk class + rejection path.

Proves (against real Postgres + Temporal, only the WS-C/WS-E boundaries faked):
  - low risk -> auto-approved by policy (no gate, no human);
  - high risk -> parks on `awaiting_plan_approval`, resolves the approver via
    the cascade, and only proceeds after SIGNAL_PLAN_APPROVAL (P1: no decision
    by an LLM);
  - an EMPTY approver cascade -> Blocked (it NEVER auto-approves on absence);
  - deterministic risk classification (defense in depth): a plan that touches
    `migrations/` is high EVEN IF the Planner says low;
  - rejection path (3 routes): re_plan / re_clarify / cancel, all audited with
    identity + justification, none of them starts implementation without going
    through the corresponding gate again.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator import policy
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import (
    insert_work_item,
    new_work_item_id,
    read_audit_actions,
    read_gate_row,
    wait_for_status,
)
from fakes import FakeControlPlane, build_fake_activities


@pytest.fixture(autouse=True)
def _reset_codeowners():
    """Isolates the CODEOWNERS reader between tests (swappable global)."""
    policy.set_codeowners_reader(None)
    yield
    policy.set_codeowners_reader(None)


def _with_codeowners(owners: list[str]):
    policy.set_codeowners_reader(lambda tenant_id, repo: "* " + " ".join(owners))


@pytest.mark.asyncio
async def test_low_risk_auto_approves_without_gate(time_skipping_env):
    work_item_id = new_work_item_id("low")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "planner_completed" in actions
    assert "plan_auto_approved" in actions
    assert "awaiting_plan_approval" not in actions  # never parked
    gate = read_gate_row(work_item_id)
    assert gate[0] == "approved" and gate[1] is True  # status=approved, auto_approved=True


@pytest.mark.asyncio
async def test_high_risk_parks_and_requires_named_approval(time_skipping_env):
    work_item_id = new_work_item_id("high")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    _with_codeowners(["@alice", "@bob"])
    state = FakeControlPlane(plan_risk_class="high")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)

        # parks at the gate: does NOT provision a sandbox or call the Coder until approved
        await wait_for_status(handle, {"awaiting_plan_approval"})
        assert state.provision_calls == 0
        assert state.coder_turn_calls == 0
        gate = read_gate_row(work_item_id)
        assert gate[0] == "pending"
        assert set(gate[2]) == {"@alice", "@bob"}  # resolved_approvers (CODEOWNERS)

        # approval by a named human -> proceeds
        await handle.signal("plan_approval",
                            {"verdict": "approved", "actor": "usr_alice"})
        await wait_for_status(handle, {"review_ready"})
        assert state.coder_turn_calls == 1
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "awaiting_plan_approval" in actions
    assert "plan_approved" in actions
    gate = read_gate_row(work_item_id)
    assert gate[0] == "approved" and gate[1] is False and gate[3] == "usr_alice"


@pytest.mark.asyncio
async def test_empty_approver_cascade_blocks_never_auto_approves(time_skipping_env):
    work_item_id = new_work_item_id("noappr")
    insert_work_item(work_item_id, tenant_id=f"tenant-noappr-{uuid.uuid4().hex[:6]}")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # no CODEOWNERS and (assumed) no access bundle for this ephemeral tenant
    policy.set_codeowners_reader(lambda t, r: None)
    state = FakeControlPlane(plan_risk_class="high")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id,
            tenant_id=f"tenant-noappr-{uuid.uuid4().hex[:6]}", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    assert result.status == WorkItemStatus.blocked.value
    assert state.coder_turn_calls == 0  # never implemented
    actions = read_audit_actions(work_item_id)
    assert "plan_gate_no_approver_blocked" in actions
    assert "escalated" in actions
    assert "plan_auto_approved" not in actions  # NEVER auto-approves on absence
    gate = read_gate_row(work_item_id)
    assert gate[0] == "blocked"


@pytest.mark.asyncio
async def test_migrations_path_forces_high_even_when_planner_says_low(time_skipping_env):
    """Deterministic defense-in-depth classification (P1): the Planner declares
    low, but the plan touches `migrations/` -> high-risk gate anyway (a model
    that under-classifies does not weaken the gate)."""
    work_item_id = new_work_item_id("mighigh")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    _with_codeowners(["@dba"])
    state = FakeControlPlane(plan_risk_class="low",
                             plan_expected_files=["migrations/0099_x.sql", "app.py"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"awaiting_plan_approval"})
        gate = read_gate_row(work_item_id)
        assert gate[6] == "high"  # effective risk_class, despite the Planner saying low
        await handle.signal("plan_approval", {"verdict": "approved", "actor": "usr_dba"})
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_rejection_cancel_is_terminal_cancelled_and_audited(time_skipping_env):
    work_item_id = new_work_item_id("rejcancel")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    _with_codeowners(["@alice"])
    state = FakeControlPlane(plan_risk_class="high")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"awaiting_plan_approval"})
        await handle.signal("plan_approval", {
            "verdict": "rejected", "route": "cancel", "actor": "usr_alice",
            "justification": "out of regulatory scope",
        })
        result = await handle.result()

    assert result.status == WorkItemStatus.cancelled.value
    assert state.coder_turn_calls == 0  # a rejection never starts implementation
    actions = read_audit_actions(work_item_id)
    assert "plan_rejected" in actions
    assert "plan_rejected_cancelled" in actions
    gate = read_gate_row(work_item_id)
    assert gate[0] == "rejected" and gate[4] == "cancel"
    assert gate[5] == "out of regulatory scope"  # justification recorded


@pytest.mark.asyncio
async def test_rejection_re_plan_reruns_planner_then_gate(time_skipping_env):
    work_item_id = new_work_item_id("rejreplan")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    _with_codeowners(["@alice"])
    state = FakeControlPlane(plan_risk_class="high")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"awaiting_plan_approval"})
        assert state.planner_calls == 1
        # reject asking for re_plan -> the planner runs again and re-parks at the gate
        await handle.signal("plan_approval", {
            "verdict": "rejected", "route": "re_plan", "actor": "usr_alice",
            "justification": "revise o blast radius",
        })
        # wait for the RE-planning (the status is still awaiting from the 1st
        # parking; we wait for the observable effect that the Planner ran again).
        for _ in range(400):
            if state.planner_calls == 2:
                break
            await asyncio.sleep(0.05)
        assert state.planner_calls == 2  # RE-planned
        await wait_for_status(handle, {"awaiting_plan_approval"})
        assert state.coder_turn_calls == 0  # never implemented without passing the gate
        # now approve
        await handle.signal("plan_approval", {"verdict": "approved", "actor": "usr_alice"})
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "plan_rejected_route_re_plan" in actions


@pytest.mark.asyncio
async def test_rejection_re_clarify_returns_to_clarification_gate(time_skipping_env):
    work_item_id = new_work_item_id("rejreclar")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    _with_codeowners(["@alice"])
    state = FakeControlPlane(plan_risk_class="high")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
            clarification_reminder_hours=100.0, clarification_escalation_days=100.0,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"awaiting_plan_approval"})
        await handle.signal("plan_approval", {
            "verdict": "rejected", "route": "re_clarify", "actor": "usr_alice",
            "justification": "ambiguous acceptance criteria",
        })
        # back to the clarification gate (needs_clarification) — NOT to
        # implementation. re_clarify reopens the clarification round (clears
        # acceptance_criteria).
        await wait_for_status(handle, {"needs_clarification"})
        assert state.coder_turn_calls == 0
        actions = read_audit_actions(work_item_id)
        assert "plan_rejected_route_re_clarify" in actions

        # answering the clarification reopens planner + gate (it never jumps to impl):
        await handle.signal("clarification_answer", {"acceptance_criteria": "now clear"})
        await wait_for_status(handle, {"awaiting_plan_approval"})
        assert state.planner_calls == 2  # re-planned after re-clarifying
        assert state.coder_turn_calls == 0  # still has not implemented (went through the gate)
        # close cleanly via an operator cancel (not the focus of this test)
        await handle.signal("cancel", "end of test")
        result = await handle.result()
    assert result.status in (WorkItemStatus.cancelled.value, WorkItemStatus.escalated.value)
