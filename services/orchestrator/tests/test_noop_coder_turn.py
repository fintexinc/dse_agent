"""A fix turn that moves no file must not re-arm the gates.

The loop was Coder -> Tester -> checkpoint -> L1, unconditionally, every round.
Nothing asked whether the Coder had produced anything. When it had not, the
Tester and L1 spent ~1000s re-deciding a byte-identical tree and returned the
verdict they had already returned.

Measured on `wi_t1-f0a824a0`: three consecutive turns reported
`files_changed=[]`, the checkpoint after each wrote the same git_ref, and L1
answered `{typecheck, build}` all three times — 1041s + 1023s + 1014s of Tester
and L1, fifty-one minutes, over a tree nothing had touched. Raising the retry
cap would only have bought more rounds of it.

`files_changed` is trustworthy for this: the Activity computes it from git
against the turn's base_sha and only falls back to the agent's own list when git
found something, and `commit()` runs under `has_changes()`, so an uncommitted
edit would still have moved HEAD.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


def _wf_input(work_item_id: str, **kw) -> WorkItemLifecycleInput:
    return WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit", **kw,
    )


@pytest.mark.asyncio
async def test_a_fix_turn_that_changes_nothing_does_not_rerun_the_gates(time_skipping_env):
    """The saving. L1 fails once, the next Coder turn moves no file, and the
    Tester and L1 must NOT be asked again about the same tree."""
    work_item_id = new_work_item_id("noop")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=1,
        # turn 1 writes; the fix turn writes nothing; then it recovers
        coder_files_changed_by_turn=[["app.py"], [], ["app.py"]],
    )
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready", "awaiting_human_review", "done"})

        # Three Coder turns ran; the middle one produced nothing.
        assert state.coder_turn_calls == 3, state.calls_log
        # …and the gates ran only for the two turns that produced something.
        assert state.l1_calls == 2, (
            f"L1 ran {state.l1_calls} times for 2 trees — the no-op turn re-armed it"
        )
        assert state.tester_calls == 2, (
            f"the Tester ran {state.tester_calls} times for 2 trees"
        )
        assert "coder_turn_made_no_change" in read_audit_actions(work_item_id)


@pytest.mark.asyncio
async def test_a_first_turn_that_writes_nothing_still_goes_through_the_gates(
    time_skipping_env,
):
    """The skip is gated on there being a previous verdict to reuse. A FIRST
    turn that writes nothing is a different failure — there is no earlier gate
    result for this tree — and must still be judged.

    Evoluiu com o wi_f1d2d66d (2026-08-10): julgado continua sendo (L1 roda),
    mas um L1 verde sobre diff vazio NÃO finaliza mais — o gate devolve ao
    Coder e, seguindo vazio, o item termina nomeando o não-trabalho. O que
    este pin garante é o julgamento; review_ready de fachada deixou de ser
    um desfecho possível."""
    work_item_id = new_work_item_id("noopfirst")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low", coder_files_changed=[])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        status = await wait_for_status(
            handle, {"review_ready", "awaiting_human_review", "done",
                     "failed", "escalated"})

        assert state.l1_calls >= 1, "the first turn was never judged"
        assert status not in {"review_ready", "awaiting_human_review", "done"}, (
            "diff vazio com L1 verde não pode finalizar (wi_f1d2d66d)"
        )


@pytest.mark.asyncio
async def test_two_no_op_turns_in_a_row_escalate_instead_of_burning_the_cap(
    time_skipping_env,
):
    """Twice in a row is not slow convergence, it is a Coder that cannot act on
    what it was told. Spending the rest of the retry cap re-reading the same
    tree buys nothing a human reading the reason would not get sooner."""
    work_item_id = new_work_item_id("noop2")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=1,
        coder_files_changed_by_turn=[["app.py"], [], []],
    )
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"escalated", "failed"})

        actions = read_audit_actions(work_item_id)
        assert "escalated" in actions, actions
        assert state.l1_calls == 1, (
            f"L1 ran {state.l1_calls} times — the second no-op re-armed it"
        )
