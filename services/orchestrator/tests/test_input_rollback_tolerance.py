"""An input field this worker does not know must not wedge the work item.

`run()` is typed `Any`, so the data converter hands `_coerce_input` a DICT on
every decode — including the one after each `continue_as_new`, which is most of
them. `WorkItemLifecycleInput(**raw_input)` answered an unknown key with
`TypeError: __init__() got an unexpected keyword argument`, and `run()` has no
`except` that catches it: the workflow task fails, Temporal retries it forever,
and the item sits there with no audit row and no terminal state. Nobody is paged
for a workflow task that keeps failing.

That is exactly what a ROLLBACK does — old code decoding an input a newer worker
wrote — so it is not about the three fields this changeset adds. Every field ever
added to that dataclass has been arming it, and this is the first release where
one actually did.

BE CLEAR ABOUT WHAT THESE TESTS PROVE. They prove the version under test survives
an input from a FUTURE version. They cannot prove anything about rolling this
release back, because the code already in production has no such tolerance: the
items it wedges have to be drained or reset by hand.

Runs WITHOUT Postgres, like the other three CI-wait files: the decode is pure and
the workflow run below needs no DB boundary.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import wait_for_status, new_work_item_id
from fakes import FakeControlPlane
from test_ci_wait_replay_guard import _db_free_activities

#: Stands in for whatever the next change adds to `WorkItemLifecycleInput`. It is
#: deliberately NOT one of this changeset's three fields: the trap is permanent
#: and the test should keep meaning the same thing after they are old news.
_FROM_A_NEWER_WORKER = "ci_wait_backoff_ceiling_seconds"

#: Small on purpose: the wait only has to end by itself, without a human signal.
_POLL_CAP = 6

#: A wedged workflow does not fail, it never returns. The bounded wait is what
#: turns "stuck forever" into a named test failure instead of a dead job.
_RUN_DEADLINE_S = 60


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides conftest's autouse skip — see the module docstring."""
    yield


def _raw(work_item_id: str, **overrides) -> dict:
    values = dict(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        ci_poll_interval_seconds=0.01, ci_pending_poll_cap=_POLL_CAP,
        ci_wait_deadline_hours=0,
    )
    values.update(overrides)
    raw = dataclasses.asdict(WorkItemLifecycleInput(**values))
    raw[_FROM_A_NEWER_WORKER] = 900
    return raw


@pytest.mark.asyncio
async def test_an_unknown_field_is_dropped_and_named_while_the_known_ones_arrive(caplog):
    """The decode itself: the unknown key does not reach the constructor, every
    known field does, and the drop is not silent."""
    raw = _raw("wi-rollback", ci_pending_poll_cap=7, budget_max_usd=12.5)

    # What the code used to do, and the entire failure. Asserted here so this
    # test keeps proving the tolerance is load-bearing rather than decorative:
    # if the dataclass ever starts accepting extras on its own, this line fails
    # and the tolerance can go.
    with pytest.raises(TypeError):
        WorkItemLifecycleInput(**raw)

    with caplog.at_level(logging.WARNING, logger="dse_orchestrator.workflow"):
        coerced = await WorkItemLifecycleWorkflow()._coerce_input(raw)

    assert coerced.work_item_id == "wi-rollback"
    assert coerced.ci_pending_poll_cap == 7
    assert coerced.budget_max_usd == 12.5
    assert not hasattr(coerced, _FROM_A_NEWER_WORKER)

    # The log is the only thing standing between "the old default took over" and
    # nobody finding out, so it has to name the item AND the key: a RENAMED field
    # is dropped by the same code path, and there its old-named default silently
    # replaces a value somebody set.
    logged = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(_FROM_A_NEWER_WORKER in m and "wi-rollback" in m for m in logged), logged


@pytest.mark.asyncio
async def test_a_run_whose_input_carries_an_unknown_field_reaches_a_terminal_state(
    time_skipping_env,
):
    """End to end, because the failure was never an exception anyone would see:
    the workflow task failed and was retried forever. The assertion is that the
    run FINISHES — and that it finishes on the cap the dict carried rather than
    on the dataclass default, which is how we know the known fields crossed the
    same decode the unknown one was dropped in.

    And it runs in the SANDBOX, unlike the three other db-free files here, which
    all pass `UnsandboxedWorkflowRunner()`. `worker.py` builds `Worker(...)` with
    no `workflow_runner`, so production gets the default SandboxedWorkflowRunner
    — and the decode this test covers reaches for `dataclasses.fields()` and the
    stdlib logger from inside the workflow. Proving it under the unsandboxed
    runner would prove it in a configuration nothing deploys, for the one path
    whose entire purpose is not to wedge the item in the one that does."""
    work_item_id = new_work_item_id("rollback")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # never green: the wait ends on its own poll cap, with no human signal.
    state = FakeControlPlane(ci_sequence=["pending"] * (_POLL_CAP + 2))

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=_db_free_activities(state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _raw(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
        # rc.130: the cap PARKS the item for review instead of escalating it.
        # The run still reaches a stable state on the cap the dict carried.
        await asyncio.wait_for(
            wait_for_status(handle, {WorkItemStatus.review_ready.value}),
            timeout=_RUN_DEADLINE_S,
        )
        final = await handle.query(WorkItemLifecycleWorkflow.get_state)
        await handle.signal("cancel", "measured")
        result = await asyncio.wait_for(handle.result(), timeout=_RUN_DEADLINE_S)

    assert result.status == WorkItemStatus.cancelled.value
    assert str(final.get("ci_wait_exhausted") or "") == f"poll_cap:{_POLL_CAP}"
