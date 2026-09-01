"""WHERE the CI wait writes the work_items row, measured event by event.

`ci-poll-writes-only-on-change-v1` stopped the per-poll write, which was right —
45 identical rows to move one integer, and the arithmetic that killed four work
items. It overshot: with only the two edge writes left, a 45-poll wait wrote
`{status: ci_pending, ci_pending: 0}` going in and `{status: review_ready,
ci_pending: 45}` coming out, so for 45 minutes the row asserted that the item had
asked CI ZERO times. An operator opening the panel at minute 30 read a wait that
had just started.

`ci_wait_persist_every_n_polls` is the middle, and the middle is only worth
anything if it lands where it says it does. Counting the writes is not enough —
two writes in a 25-poll wait is the same count whether they fell on polls 10 and
20 or on polls 1 and 2 — so these tests recover the POLL NUMBER each write landed
on, from the same histories the event budget is measured on.

Runs WITHOUT Postgres, like `test_ci_wait_replay_guard.py` and
`test_ci_wait_event_budget.py`: nothing here touches the DB boundary, and the
counterpart that reads the actual row lives in `test_ci_wait_history.py`.
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

#: High enough that the whole wait stays in ONE history: a `continue_as_new`
#: would truncate the very sequence these tests read.
_NO_CONTINUE_AS_NEW = 10**9

#: Longest a wait may go on lying, in seconds, at the deployed poll interval.
#: Ten minutes is the number the period was chosen against.
_STALENESS_BUDGET_S = 600


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides conftest's autouse skip — see the module docstring."""
    yield


async def _writes_by_poll_number(env, *, polls: int, every: int) -> list[tuple[str, int]]:
    """Every Activity the CI wait scheduled, paired with how many CI polls had
    already been made when it landed. The pairing is what makes "on the interval"
    checkable: the entry write happens before poll 1 and is excluded, so what
    comes back is exactly what the wait does while it is waiting."""
    work_item_id = new_work_item_id(f"ciper{polls}x{every}")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # never green: the wait ends on its own poll cap, so the history is a pure
    # CI wait that closes itself with no human signal.
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
                # the wall clock is not what is under test and would end the
                # wait before the cap does under time-skipping.
                ci_wait_deadline_hours=0,
                ci_wait_persist_every_n_polls=every,
                history_continue_as_new_threshold=_NO_CONTINUE_AS_NEW,
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
    shape: list[tuple[str, int]] = []
    polls_so_far = 0
    for event in (await handle.fetch_history()).events:
        name = event.activity_task_scheduled_event_attributes.activity_type.name
        if not name:
            continue
        if name == "consume_ci_status":
            polls_so_far += 1
        elif polls_so_far:
            shape.append((name, polls_so_far))
    return shape


def _during_the_wait(shape, activity: str, *, polls: int) -> list[int]:
    """The poll numbers `activity` was scheduled on while polling was still going
    on. Strictly BEFORE the last poll, so the escalation's own writes — which all
    land after it — are not counted as part of the wait."""
    return [n for name, n in shape if name == activity and n < polls]


@pytest.mark.asyncio
async def test_the_row_is_rewritten_every_tenth_poll_and_on_no_other(time_skipping_env):
    """The whole claim, on the poll numbers rather than on a count: a 25-poll
    wait rewrites the row on polls 10 and 20, and on nothing else."""
    shape = await _writes_by_poll_number(time_skipping_env, polls=25, every=10)

    assert _during_the_wait(shape, "update_work_item_status", polls=25) == [10, 20]
    # and it stays a WRITE, not a story: the audit rows this loop was cut for
    # must not come back with it.
    assert _during_the_wait(shape, "emit_audit_event", polls=25) == []


@pytest.mark.asyncio
async def test_a_wait_shorter_than_the_period_leaves_the_row_alone(time_skipping_env):
    """Including the first poll after the entry, which is the one that must NOT
    rewrite: the entry has just written the same row, and repeating it would be
    the per-poll write with extra steps."""
    shape = await _writes_by_poll_number(time_skipping_env, polls=9, every=10)

    assert _during_the_wait(shape, "update_work_item_status", polls=9) == []


@pytest.mark.asyncio
async def test_zero_turns_the_periodic_write_off(time_skipping_env):
    """`<= 0` disables it, back to the two edge writes — the behaviour this
    change replaces. It is also the control for the test above: same wait, same
    length, and the two writes disappear."""
    shape = await _writes_by_poll_number(time_skipping_env, polls=25, every=0)

    assert _during_the_wait(shape, "update_work_item_status", polls=25) == []


def test_the_shipped_period_bounds_how_long_the_row_may_be_wrong():
    """The period was picked against a promise — "the row is never more than ten
    minutes behind" — and a promise made of two independent defaults is worth
    asserting: lengthening either one silently doubles how long the panel lies."""
    shipped = WorkItemLifecycleInput(work_item_id="x", tenant_id="t", requester="r")
    stale_seconds = shipped.ci_wait_persist_every_n_polls * shipped.ci_poll_interval_seconds

    assert 0 < stale_seconds <= _STALENESS_BUDGET_S, (
        f"a CI wait can go {stale_seconds:.0f}s without rewriting the row "
        f"({shipped.ci_wait_persist_every_n_polls} polls x "
        f"{shipped.ci_poll_interval_seconds:.0f}s), over the {_STALENESS_BUDGET_S}s "
        f"the period was chosen against"
    )
