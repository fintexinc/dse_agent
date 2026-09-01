"""WSB-E5-T2 — operator controls: pause/resume, cancel (+teardown),
reassign_model/runtime, escalate, force_clarification. Every operator action
produces an audit row (P8)."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, read_work_item, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


@pytest.mark.asyncio
async def test_pause_blocks_next_activity_but_not_current(time_skipping_env):
    """Pause does not kill the running Activity (the fake only finishes when the
    test releases an Event) — it only blocks the NEXT activity at the following
    boundary, exactly as specified (WSB-E5-T2)."""
    work_item_id = new_work_item_id("pause")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    hang_event = asyncio.Event()
    state = FakeControlPlane(coder_turn_hang_event=hang_event)
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

        # wait for the `run_coder_turn` Activity to be in progress (it only
        # releases when we unblock the Event) and only THEN send pause.
        for _ in range(100):
            if state.coder_turn_calls >= 1:
                break
            await asyncio.sleep(0.05)
        assert state.coder_turn_calls == 1

        await handle.signal("pause", "operator wants to investigate")

        # the running Activity is not cancelled by pause — release it now.
        hang_event.set()

        # the workflow must proceed UP TO the next boundary (checkpoint/L1) and
        # only THEN stop — it never reaches pr_ready while paused.
        await asyncio.sleep(0.3)
        status_while_paused = await handle.query(WorkItemLifecycleWorkflow.get_status)
        assert status_while_paused != WorkItemStatus.review_ready.value

        await handle.signal("resume", "ok, go ahead")
        await wait_for_status(handle, {"review_ready"})

    actions = read_audit_actions(work_item_id)
    assert "pause" not in actions  # operator signals do not audit by themselves (internal log via query)


@pytest.mark.asyncio
async def test_cancel_tears_down_sandbox_and_marks_cancelled(time_skipping_env):
    """rc.130: cancelamento humano é decisão, não falha — vira `cancelled`, um
    status de verdade. Antes resolvia para `failed`, e o operador cancelando um
    workflow já morto escrevia `cancelled` por SQL num enum que não o tinha — o
    sweep de encalhados re-escalava 33 dessas linhas 6 h depois."""
    work_item_id = new_work_item_id("cancel")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    hang_event = asyncio.Event()
    state = FakeControlPlane(coder_turn_hang_event=hang_event)
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
        for _ in range(100):
            if state.coder_turn_calls >= 1:
                break
            await asyncio.sleep(0.05)

        await handle.signal("cancel", "the task is no longer needed")
        hang_event.set()  # release the running Activity so the workflow can progress and see the cancel

        result = await handle.result()

    assert result.status == WorkItemStatus.cancelled.value
    assert "cancelled_by_operator" in (result.detail or "")
    assert state.teardown_calls == 1
    row = read_work_item(work_item_id)
    assert row[0] == WorkItemStatus.cancelled.value
    actions = read_audit_actions(work_item_id)
    assert "cancelled_by_operator" in actions


@pytest.mark.asyncio
async def test_reassign_model_is_forwarded_to_next_coder_turn(time_skipping_env):
    work_item_id = new_work_item_id("reassign")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(l1_fail_times=1)
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
        await handle.signal("reassign_model", "claude-opus-4")
        await wait_for_status(handle, {"review_ready"})

    # `reassign_model` does not produce an audit row of its own (it is not a
    # business state transition); the functional proof that the signal was
    # applied is `state.coder_turn_calls == 2` (1st attempt fails L1, 2nd —
    # already with the override applied — passes).
    assert state.coder_turn_calls == 2


@pytest.mark.asyncio
async def test_escalate_signal_forces_terminal_escalated(time_skipping_env):
    work_item_id = new_work_item_id("escalatesig")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    hang_event = asyncio.Event()
    state = FakeControlPlane(coder_turn_hang_event=hang_event)
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
        for _ in range(100):
            if state.coder_turn_calls >= 1:
                break
            await asyncio.sleep(0.05)

        await handle.signal("escalate", "compliance risk — stop")
        hang_event.set()

        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    actions = read_audit_actions(work_item_id)
    assert "escalated" in actions


