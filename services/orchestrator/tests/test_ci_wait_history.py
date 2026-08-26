"""Bloco 5 — the CI wait stopped narrating every poll, and stopped dying at
Temporal's history ceiling.

Measured on the live cluster before this change: three actions were 81.9% of
`audit_log` (`ci_pending` 6495, `ci_status_consumed` 6494, `ci_status_observed`
6492) and FOUR work items stopped at exactly 1242 CI polls — at ~41.4 history
events per poll that is ~51_400 events against the server's hard ceiling of
51_200, so the server terminated them with no audit row, no escalation and no
sandbox teardown.

What is asserted here:

* a long wait writes TWO CI audit rows (entry + transition), not two per poll;
* the transition row carries the shape of the wait it closes;
* the row still says what the wait is doing WHILE it waits — the first cut at
  this wrote nothing at all between the two edges, so a 45-minute wait spent all
  45 of them claiming `ci_pending: 0`;
* the number of audit rows does not depend on how many polls the wait took, and
  the number of status writes depends on it only through
  `ci_wait_persist_every_n_polls` — one per period, not one per poll (the
  comparison between a short and a long wait);
* the wait closes the run with `continue_as_new` at the history threshold, and
  the poll counter, the wait epoch and the retry counters come out the other
  side intact;
* it refuses to close the run while a signal is parked in instance state —
  the one thing `continue_as_new` would silently drop.

Real Temporal + real Postgres; only the other services' boundaries are faked.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter

import psycopg2
import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import DSN, insert_work_item, new_work_item_id, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

_MERGE_SIGNAL = {"merged_by": "usr_test", "pr_number": 1000}


def _input(work_item_id: str, **overrides) -> WorkItemLifecycleInput:
    values = dict(
        work_item_id=work_item_id,
        tenant_id="test-tenant",
        requester="usr_test",
        repo="acme/repo",
        base_branch="main",
        acceptance_criteria="verifiable criterion",
        ci_poll_interval_seconds=0.01,
        ci_pending_poll_cap=500,
        # the wall clock is not what is under test here, and time-skipping makes
        # a 6h default trip on a 500-poll wait.
        ci_wait_deadline_hours=0,
        activity_retry_cap=2,
    )
    values.update(overrides)
    return WorkItemLifecycleInput(**values)


def _read_audit(work_item_id: str) -> list[tuple[str, dict]]:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, details FROM audit_log WHERE work_item_id = %s ORDER BY id",
                (work_item_id,),
            )
            return [(a, d if isinstance(d, dict) else json.loads(d or "{}"))
                    for a, d in cur.fetchall()]
    finally:
        conn.close()


def _read_validation_attempts(work_item_id: str) -> dict:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT validation_attempts FROM work_items WHERE id = %s", (work_item_id,)
            )
            row = cur.fetchone()
            return (row[0] or {}) if row else {}
    finally:
        conn.close()


def _scheduled_activities(history) -> Counter:
    """How many times each Activity was SCHEDULED — i.e. what the loop actually
    cost in history events, which is the quantity that killed the four items."""
    counts: Counter = Counter()
    for event in history.events:
        name = event.activity_task_scheduled_event_attributes.activity_type.name
        if name:
            counts[name] += 1
    return counts


async def _wait_for_state(handle, predicate, attempts: int = 240, sleep_s: float = 0.25):
    """`wait_for_status` for anything else in `get_state` — here, the poll
    counter, which is the only live view of a wait that no longer writes a row
    per poll."""
    state = None
    for _ in range(attempts):
        try:
            state = await handle.query(WorkItemLifecycleWorkflow.get_state)
        except Exception as exc:  # noqa: BLE001 - only a query deadline is transient
            if "deadline" not in str(exc).lower() and "timeout" not in str(exc).lower():
                raise
            await asyncio.sleep(sleep_s)
            continue
        if predicate(state):
            return state
        await asyncio.sleep(sleep_s)
    raise AssertionError(f"state never satisfied the predicate, last={state!r}")


async def _frozen_state(handle, key: str, attempts: int = 60, sleep_s: float = 0.25):
    """The state once `key` has stopped moving. Used right after `pause`, which
    parks the review loop at the top of its next pass: without it, reading a row
    "during" a wait is a race against a time-skipping clock that runs the whole
    wait in milliseconds."""
    previous = object()
    state = None
    for _ in range(attempts):
        state = await handle.query(WorkItemLifecycleWorkflow.get_state)
        if state.get(key) == previous:
            return state
        previous = state.get(key)
        await asyncio.sleep(sleep_s)
    raise AssertionError(f"{key} never stopped moving, last={state!r}")


async def _result_bounded(handle, *, segundos: float = 90.0):
    """`handle.result()` COM prazo.

    Sem isto o arquivo pendura para sempre quando um veredito se perde na
    janela do continue_as_new — e como o pytest-timeout roda em modo thread,
    ele mata o PROCESSO: a suite inteira do orchestrator morre e o CI reporta
    um travamento mudo em vez do defeito. Prazo transforma isso em falha
    legível, que é o mínimo que um teste deve entregar."""
    try:
        return await asyncio.wait_for(handle.result(), timeout=segundos)
    except asyncio.TimeoutError:  # noqa: UP041 — pareado com o alias do runtime
        raise AssertionError(
            f"o workflow não terminou em {segundos}s após os signals — "
            "veredito perdido (suspeita: janela do continue_as_new)"
        ) from None


@pytest.mark.asyncio
async def test_the_row_reports_the_wait_while_the_wait_is_still_running(time_skipping_env):
    """The regression an OPERATOR sees, and the only assertion in this file made
    before the wait is over.

    With only the two edge writes, a 45-poll wait wrote `{status: ci_pending,
    ci_pending: 0}` going in and `{status: review_ready, ci_pending: 45}` coming
    out. Every other test here reads the row afterwards, by which time the exit
    write has already made it right — so none of them could see that for 45
    minutes the row asserted the item had asked CI zero times, and that whoever
    opened the panel at minute 30 read a wait that had just started.

    The wait is frozen with `pause` (consumed by `_boundary_gate` at the top of a
    pass) so this reads a defined instant instead of racing the clock."""
    work_item_id = new_work_item_id("cirow")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # long enough that the pause cannot possibly land past the end of the wait,
    # which would leave the row holding the exit write instead of a periodic one.
    state = FakeControlPlane(ci_sequence=["pending"] * 400 + ["green"])

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
        period = _input(work_item_id).ci_wait_persist_every_n_polls
        await _wait_for_state(handle, lambda s: (s.get("ci_pending_polls") or 0) > period)
        await handle.signal("pause", "reading the row mid-wait")
        frozen = await _frozen_state(handle, "ci_pending_polls")
        polls = frozen["ci_pending_polls"]

        row = _read_validation_attempts(work_item_id).get("ci_pending")
        assert row, (
            f"after {polls} polls the row still says the item has never asked CI "
            f"(ci_pending={row!r}) — the panel reads a wait that just started"
        )
        # it is the PERIODIC write and not some other transition sneaking one in:
        # every write inside the wait lands on a multiple of the period, and none
        # of them can be ahead of the counter it copied.
        assert row % period == 0, row
        assert period <= row <= polls

        await handle.signal("resume", "done reading")
        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", _MERGE_SIGNAL)
        result = await _result_bounded(handle)

    assert result.status == WorkItemStatus.done.value
    # and the exit write still lands: the periodic one is a floor on freshness,
    # never a replacement for the number the wait actually ended on.
    assert _read_validation_attempts(work_item_id).get("ci_pending") == 400


@pytest.mark.asyncio
async def test_long_ci_wait_writes_two_ci_audit_rows(time_skipping_env):
    """A12: 45 polls of waiting must cost TWO rows written by the WORKFLOW —
    "I started waiting" and "it changed" — instead of the 90 (45 x `ci_pending`
    + 45 x `ci_status_observed`) it used to cost.

    The third row, `ci_status_consumed`, is emitted inside the Activity
    (`services/validation/dse_validation/github/ci_status.py`) and is not
    exercised by the fake here, so this file cannot see the system total. The
    same changeset cut that one too, from one per poll to one per real change,
    and the end-to-end count — real Activity, real Postgres — is FOUR per wait
    and constant in its length (measured at 3, 45 and 150 polls): `ci_pending` +
    `ci_status_consumed` on the first observation, then `ci_status_consumed` +
    `ci_status_observed` on the transition. Two facts, each recorded once by the
    workflow and once by the Activity. Going lower means one of the two stops
    recording, which is a cross-service decision about who owns the CI ledger,
    not a loop fix — see `services/validation/tests/test_ci_status.py`."""
    work_item_id = new_work_item_id("ciquiet")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(ci_sequence=["pending"] * 45 + ["green"])

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", _MERGE_SIGNAL)
        result = await _result_bounded(handle)

    assert result.status == WorkItemStatus.done.value
    # the wait really happened — 45 pending polls + the green one
    assert state.calls_log.count("consume_ci_status") == 46

    actions = [a for a, _ in _read_audit(work_item_id)]
    assert actions.count("ci_pending") == 1, "the entry into the wait is ONE fact"
    assert actions.count("ci_status_observed") == 1, "only the transition is a fact"
    # the exit is still audited, and after the entry — the wait is greppable
    # end to end without the 44 rows in between.
    assert actions.index("awaiting_human_review") > actions.index("ci_pending")

    # the poll counter still lands in the durable projection at the exit
    # transition, which is where an operator reads it after the fact.
    assert _read_validation_attempts(work_item_id).get("ci_pending") == 45


@pytest.mark.asyncio
async def test_ci_transition_row_carries_the_shape_of_the_wait(time_skipping_env):
    """The row that replaces 44 identical ones has to be worth more than they
    were: it names what it came from, how many polls it took and how long."""
    work_item_id = new_work_item_id("cishape")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(ci_sequence=["pending"] * 12 + ["green"])

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", _MERGE_SIGNAL)
        await _result_bounded(handle)

    observed = [d for a, d in _read_audit(work_item_id) if a == "ci_status_observed"]
    assert len(observed) == 1
    assert observed[0]["status"] == "green"
    assert observed[0]["from_status"] == "pending"
    assert observed[0]["polls"] == 12
    assert observed[0]["waited_seconds"] >= 0


@pytest.mark.asyncio
async def test_polls_cost_ci_calls_plus_one_status_write_per_period(time_skipping_env):
    """The direct measurement of what the loop costs per poll: run the SAME
    lifecycle with a 5-poll wait and with a 45-poll wait and compare the
    Activities each history SCHEDULED.

    The 40 extra polls buy 40 extra `consume_ci_status` calls, FOUR extra status
    writes — one per `ci_wait_persist_every_n_polls`, at polls 10/20/30/40 — and
    no audit rows at all. The four are not a leak in change A: writing zero rows
    for the whole wait is what made the row claim `ci_pending: 0` at minute 30,
    and the periodic write is what buys it back at 6 history events a time. What
    change A removed for good is the part that grew with the wait ONE FOR ONE.
    `test_ci_wait_periodic_persist.py` pins which polls those four land on."""
    counts = {}
    lengths = {"short": 5, "long": 45}
    for label, polls in lengths.items():
        work_item_id = new_work_item_id(f"cicost{label}")
        insert_work_item(work_item_id)
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        state = FakeControlPlane(ci_sequence=["pending"] * polls + ["green"])

        async with Worker(
            time_skipping_env.client, task_queue=task_queue,
            workflows=[WorkItemLifecycleWorkflow],
            activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
        ):
            handle = await time_skipping_env.client.start_workflow(
                WorkItemLifecycleWorkflow.run, _input(work_item_id),
                id=work_item_id, task_queue=task_queue,
            )
            await wait_for_status(handle, {WorkItemStatus.review_ready.value})
            await handle.signal("review_comment", {"verdict": "approved"})
            await handle.signal("merged_by_human", _MERGE_SIGNAL)
            await _result_bounded(handle)
            counts[label] = _scheduled_activities(await handle.fetch_history())

    period = _input("x").ci_wait_persist_every_n_polls
    extra_polls = lengths["long"] - lengths["short"]
    # derived from the shipped period rather than written out, so changing the
    # period moves this expectation instead of breaking the test for no reason.
    extra_writes = lengths["long"] // period - lengths["short"] // period

    assert (counts["long"]["consume_ci_status"]
            - counts["short"]["consume_ci_status"]) == extra_polls
    assert (counts["long"]["update_work_item_status"]
            - counts["short"]["update_work_item_status"]) == extra_writes
    assert counts["long"]["emit_audit_event"] == counts["short"]["emit_audit_event"]


@pytest.mark.asyncio
async def test_continue_as_new_at_the_threshold_carries_the_whole_wait(time_skipping_env):
    """The change that keeps the item alive. With the threshold pushed down to
    one event, every pass of the wait is over it, so the run closes and reopens
    on every poll — and the counters that describe the work item have to come
    out the far side unchanged. If any of them lived in instance state instead
    of the input, this is where the data loss would show up.

    It is also the control for the guard test below: same threshold, same
    mechanism, so a `continue_as_new_count` that does not move there can only be
    the guard."""
    work_item_id = new_work_item_id("cican")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # red first: it costs a Coder retry and a review round, so the assertions
    # below cover state that a naive continue_as_new would reset.
    state = FakeControlPlane(ci_sequence=["red"] + ["pending"] * 10 + ["green"])

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _input(work_item_id, history_continue_as_new_threshold=1),
            id=work_item_id, task_queue=task_queue,
        )
        # the epoch is stamped on the first pending poll; capture it early so
        # the assertion at the end compares against the ORIGINAL wait, not
        # against whatever a chained run might have re-stamped.
        early = await _wait_for_state(handle, lambda s: (s.get("ci_pending_polls") or 0) >= 3)
        epoch = early["ci_wait_started_at_epoch"]
        assert epoch is not None

        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        final = await handle.query(WorkItemLifecycleWorkflow.get_state)
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", _MERGE_SIGNAL)
        result = await _result_bounded(handle)

    assert result.status == WorkItemStatus.done.value
    # it really continued as new from inside the wait (1 is the pre-existing
    # intake -> implementation transition).
    assert final["continue_as_new_count"] > 1
    # Bounded on purpose. With threshold=1 every poll continues as new, so the
    # chain length is the poll count plus the intake transition; anything beyond
    # that means the wait is not converging, and an unbounded chain shows up as a
    # dead job with a stack dump rather than a failed assertion.
    assert final["continue_as_new_count"] <= len(state.ci_sequence) + 13
    # ... and nothing the loop needs was left behind in instance state.
    assert final["ci_pending_polls"] == 10
    assert final["ci_wait_started_at_epoch"] == epoch
    assert final["coder_retry_count"] == 1
    assert final["review_round"] == 1
    assert final["ci_last_audited_status"] == "green"


@pytest.mark.asyncio
async def test_continue_as_new_refuses_to_close_a_run_holding_a_signal(time_skipping_env):
    """`continue_as_new` carries the input and NOTHING else, so a signal parked
    in instance state would be dropped by it. `merged_by_human` is the sharpest
    case: it is only consumed in the `approved` branch, so it sits in memory
    across an entire CI wait.

    The threshold is one event — every pass of the wait is over it — so the run
    would close on the pass right after the first pending poll. It must not,
    while the merge signal is parked, and the merge must still be there when the
    loop finally asks for it."""
    work_item_id = new_work_item_id("ciguard")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # green first so the workflow parks on the human wait BEFORE any pending
    # poll — the one window where no continue_as_new is possible yet
    # (`_ci_polled_this_run` is still False), which is what makes the signal's
    # arrival deterministic instead of a race with the test.
    state = FakeControlPlane(ci_sequence=["green"] + ["pending"] * 12 + ["green"])

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _input(work_item_id, history_continue_as_new_threshold=1),
            id=work_item_id, task_queue=task_queue,
        )
        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        baseline = (await handle.query(
            WorkItemLifecycleWorkflow.get_state))["continue_as_new_count"]

        # parked for the whole of the CI wait that the fix cycle is about to start
        await handle.signal("merged_by_human", _MERGE_SIGNAL)
        await handle.signal("review_comment", {"verdict": "changes_requested",
                                               "comment": "tweak X"})

        during = await _wait_for_state(handle, lambda s: (s.get("ci_pending_polls") or 0) >= 8)
        assert during["continue_as_new_count"] == baseline, (
            "closed the run with a merge signal still in memory — "
            "the signal would have been lost"
        )

        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        await handle.signal("review_comment", {"verdict": "approved"})
        result = await _result_bounded(handle)

    # the parked merge signal survived the wait: no second merged_by_human was
    # ever sent, so reaching Done proves it was still there.
    assert result.status == WorkItemStatus.done.value
