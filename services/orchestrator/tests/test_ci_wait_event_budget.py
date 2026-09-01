"""What a pending CI poll COSTS in history events, measured, against the cap.

The four work items that died were not killed by a missing continue_as_new. They
were killed by an arithmetic relation nothing in this repo asserted:

    ci_pending_poll_cap x (history events per pending poll)  vs  51_200

At ~41 events per poll the 1440-poll cap wanted ~59_000 events, so the server's
hard ceiling always arrived first (poll ~1249; observed 1242) and terminated the
execution with no audit row, no escalation and no sandbox teardown — the cap
bounded something it could never reach.

`ci-poll-writes-only-on-change-v1` is what repairs that relation, by cutting the
pass from 6 Activities to 2. This test is the one that will notice if a later
edit puts the cost back: it MEASURES the per-poll cost differentially from two
real histories (so fixed start-up cost cancels out) and checks the relation
against the SHIPPED cap, not against a number written in a comment.

Runs WITHOUT Postgres, like `test_ci_wait_replay_guard.py` and for the same
reason: a budget nobody can check on a laptop is a budget that rots.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import wait_for_status, new_work_item_id
from fakes import FakeControlPlane
from test_ci_wait_replay_guard import _db_free_activities

#: Temporal's hard per-execution ceiling. The server TERMINATES at this count.
_HISTORY_CEILING = 51_200

#: Two wait lengths far enough apart that the difference is all steady-state
#: polling and none of the fixed intake/implementation cost.
_SHORT_POLLS = 6
_LONG_POLLS = 26


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides conftest's autouse skip — see the module docstring."""
    yield


async def _events_for_a_wait_of(env, polls: int) -> int:
    work_item_id = new_work_item_id(f"cibudget{polls}")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # never green: the wait ends on the cap, so the history is a pure CI wait
    state = FakeControlPlane(ci_sequence=["pending"] * (polls + 2))
    async with Worker(env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      workflow_runner=UnsandboxedWorkflowRunner(),
                      activities=_db_free_activities(state)):
        handle = await env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            WorkItemLifecycleInput(
                work_item_id=work_item_id, tenant_id="test-tenant",
                requester="usr_test", repo="acme/repo", base_branch="main",
                acceptance_criteria="crit", ci_poll_interval_seconds=0.01,
                ci_pending_poll_cap=polls,
                # the wall clock and the continue_as_new must not end the wait:
                # what is being measured is the cost of a pass, and both of them
                # would truncate the history that carries it.
                ci_wait_deadline_hours=0,
                history_continue_as_new_threshold=_HISTORY_CEILING,
            ),
            id=work_item_id, task_queue=task_queue,
        )
        # rc.130: the cap PARKS the item for review instead of escalating it —
        # the wait still ends on the cap, which is what this file measures.
        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        final = await handle.query(WorkItemLifecycleWorkflow.get_state)
        await handle.signal("cancel", "measured")
        result = await handle.result()
    assert result.status == WorkItemStatus.cancelled.value
    assert str(final.get("ci_wait_exhausted") or "").startswith(f"poll_cap:{polls}")
    handle = env.client.get_workflow_handle(work_item_id)
    return len((await handle.fetch_history()).events)


@pytest.mark.asyncio
async def test_the_shipped_poll_cap_fits_under_the_history_ceiling(time_skipping_env):
    short = await _events_for_a_wait_of(time_skipping_env, _SHORT_POLLS)
    long = await _events_for_a_wait_of(time_skipping_env, _LONG_POLLS)

    per_poll = (long - short) / (_LONG_POLLS - _SHORT_POLLS)
    cap = WorkItemLifecycleInput(
        work_item_id="x", tenant_id="t", requester="r").ci_pending_poll_cap

    # THE relation. Before the change: 1440 x ~41 = ~59_000 > 51_200, so the cap
    # was unreachable and the server ended the item instead. It has to hold with
    # room to spare — the wait is not the only thing in the run's history.
    assert cap * per_poll < _HISTORY_CEILING * 0.75, (
        f"a pending poll costs {per_poll} events, so the shipped "
        f"ci_pending_poll_cap={cap} wants {cap * per_poll:.0f} events against a "
        f"{_HISTORY_CEILING} ceiling: the cap cannot fire and the SERVER ends "
        f"the work item instead — the exact failure that killed four of them"
    )
    # and the cost itself, so a regression is reported as what it is rather than
    # as a mysterious cap failure.
    assert per_poll <= 20, f"a pending poll costs {per_poll} history events (was 41, budget 20)"
