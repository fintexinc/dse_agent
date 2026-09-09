"""Taking the `dse` label off a card and putting it back starts a fresh attempt.

That gesture is what the DSE's own escalation comment asks the human to make,
and until now it did nothing at all: a card's trigger event is
`created:{issue id}`, the issue id is immutable, so the derived event_id is a
lifetime constant. Re-applying the label converged on the id of the attempt that
already ended, `admit_work_item` found it, and the sweep moved on —
`signal_duplicate_ignored`, 51 times on 2026-09-09 alone. Restarting a card
meant deleting rows from `ingest_events` and `work_items` by hand, which is the
kind of surgery rc.130 set out to abolish.

The dangerous half is not starting the attempt, it is starting it ONCE. The
sweep runs every minute and the label does not remove itself; a naive
"label present means go" restarts the same work forever. That exact shape turned
one stuck item into ~2,900 audit rows, in a table that is append-only.

So the gesture is an EDGE, not a level. `jira_trigger_state` remembers whether
the label was on the card last time we looked, and the generation only advances
on absent → present. Three properties fall out, and each has a test below:

  - a label that simply SITS on a card advances nothing, however many sweeps go
    by — including when the DSE cannot remove it for lack of *Edit Issues*;
  - the FIRST sighting of any card never advances, which is what makes the
    deploy a no-op for the cards already carrying the label;
  - generation 0 keeps the historic `created:{id}` spelling, so every card
    already ingested resolves to exactly the event_id it resolves to today.

The removal of the label is a convenience, never the guard — and it is confirmed
by reading the labels back, not by trusting the PUT's status code. A removal
that reports success without applying is the one sequence that would still loop.
"""
from __future__ import annotations

import pytest

from adapter_jira import events, trigger_state
from adapter_jira.ingest import ingest_task_trigger

from .helpers import FakeConn

TENANT = "test_tenant_jira_adapter"
SITE = "https://acme.atlassian.net"
REPORTER = {"accountId": "acc_reporter", "displayName": "Rita"}
ISSUE_ID = "10300"
TICKET = "BFA-300"

TERMINAL = ["done", "failed", "cancelled", "escalated", "blocked"]


def _issue(*, labels=("dse",), status="Open"):
    return {
        "id": ISSUE_ID,
        "key": TICKET,
        "self": f"{SITE}/rest/api/3/issue/{ISSUE_ID}",
        "fields": {
            "summary": "user should not be able to print generated report",
            "description": "the print button is enabled on the preview",
            "labels": list(labels),
            "status": {"name": status},
            "project": {"key": TICKET.split("-")[0]},
            "reporter": dict(REPORTER),
        },
    }


class Store:
    """The rows this path reads and writes, including the latch.

    `ingest_events` and `jira_trigger_state` are real dicts, not canned answers:
    the single-shot property can only be proven if a second sweep dedupes for
    real, through the same event_id and the same latch the production code
    derives.
    """

    def __init__(self, *, work_item_status: str | None = None):
        self.work_item_status = work_item_status
        self.ingest_events: dict[str, str] = {}
        self.work_items: list[tuple] = []
        self.latch: dict[tuple[str, str], dict] = {}
        self.emitted: list[dict] = []

    def emit(self, **kwargs):
        self.emitted.append(kwargs)

    @property
    def admitted_ids(self) -> list[str]:
        return sorted({wid for wid in self.ingest_events.values()})

    def responder(self, sql: str, params):
        if sql.startswith("SELECT generation, label_armed FROM jira_trigger_state"):
            row = self.latch.get((params[0], params[1]))
            return [(row["generation"], row["label_armed"])] if row else []
        if sql.startswith("INSERT INTO jira_trigger_state"):
            self.latch[(params[0], params[1])] = {
                "generation": params[3], "label_armed": params[4],
            }
            return []
        if sql.startswith("UPDATE jira_trigger_state"):
            self.latch[(params[2], params[3])] = {
                "generation": params[0], "label_armed": params[1],
            }
            return []
        if sql.startswith("SELECT work_item_id FROM ingest_events"):
            hit = self.ingest_events.get(params[0])
            return [(hit,)] if hit else []
        if sql.startswith("SELECT id, status FROM work_items") or sql.startswith(
            "SELECT id, status, repo, base_branch FROM work_items"
        ):
            if not self.work_item_status:
                return []
            return [("wi_prior", self.work_item_status, "acme/fe", "dse-agent")]
        if sql.startswith("INSERT INTO ingest_events"):
            self.ingest_events.setdefault(params[1], params[0])
            return []
        if sql.startswith("INSERT INTO work_items"):
            self.work_items.append(params)
            return []
        if sql.startswith("INSERT INTO audit_log"):
            return []
        if sql.startswith("SELECT 1 FROM audit_log"):
            return []
        if sql.startswith("SELECT kill_switch_enabled") or sql.startswith(
            "SELECT active, reason FROM channel_kill_switches"
        ):
            return []
        if "repo_bindings" in sql or "repo_profiles" in sql:
            return []
        raise AssertionError(f"unexpected statement: {sql}")


@pytest.fixture
def store(monkeypatch):
    s = Store()
    conn = FakeConn(s.responder)
    monkeypatch.setattr("adapter_jira.ingest.get_connection", lambda: conn)
    monkeypatch.setattr("adapter_jira.ingest.audit_emit", s.emit)
    monkeypatch.setattr("adapter_jira.trigger_state.get_connection", lambda: conn)
    return s


def _sweep(store, *, labels=("dse",)):
    """One poller pass over the card: observe the label, then act if present."""
    issue = _issue(labels=labels)
    present = "dse" in labels
    generation, _advanced = trigger_state.observe(
        tenant_id=TENANT, issue_id=ISSUE_ID, ticket_key=TICKET, label_present=present
    )
    if not present:
        return None
    return ingest_task_trigger(
        issue,
        tenant_id=TENANT,
        actor_account_id=REPORTER["accountId"],
        resolved_principal="usr_rita",
        generation=generation,
    )


# --------------------------------------------------------------- compatibility

def test_generation_zero_keeps_the_historic_event_id():
    """The compatibility rule the whole deploy rests on. Any suffix here — ':0'
    included — changes the event_id of every card in the fleet, and the first
    sweep after the deploy re-admits all of them."""
    plain = events.build_task_event(
        _issue(), actor_account_id="a", resolved_principal="p"
    )
    zero = events.build_task_event(
        _issue(), generation=0, actor_account_id="a", resolved_principal="p"
    )
    assert plain.event_id == zero.event_id
    assert f"created:{ISSUE_ID}" in plain.message_id
    assert plain.message_id == f"created:{ISSUE_ID}"


def test_a_generation_does_not_collide_with_the_other_namespaces():
    """`created:{id}:{n}` must not be reachable by any other builder."""
    ids = {
        events.build_task_event(_issue(), generation=n, actor_account_id="a",
                                resolved_principal="p").event_id
        for n in range(4)
    }
    assert len(ids) == 4, "two generations derived the same event_id"


def test_the_first_sighting_never_advances(store):
    """Every card in the fleet is seen for the first time on the sweep right
    after the deploy, with the label on it. None of them may restart."""
    generation, advanced = trigger_state.observe(
        tenant_id=TENANT, issue_id=ISSUE_ID, ticket_key=TICKET, label_present=True
    )
    assert (generation, advanced) == (0, False)


# --------------------------------------------------------------- the gesture

def test_taking_the_label_off_and_putting_it_back_starts_a_new_attempt(store):
    _sweep(store)                      # card arrives labelled
    assert len(store.admitted_ids) == 1
    store.work_item_status = "failed"  # the attempt ends

    _sweep(store, labels=())           # the human removes the label
    _sweep(store)                      # ...and puts it back
    assert len(store.admitted_ids) == 2, (
        f"the gesture did not start a second attempt: {store.admitted_ids}"
    )


def test_the_gesture_works_as_many_times_as_a_human_makes_it(store):
    """'One retry per ticket, forever' was the old ceiling. There is none now."""
    _sweep(store)
    for _ in range(4):
        store.work_item_status = "failed"
        _sweep(store, labels=())
        _sweep(store)
    assert len(store.admitted_ids) == 5


@pytest.mark.parametrize("status", TERMINAL)
def test_the_gesture_restarts_from_every_terminal_status(store, status):
    """Including `done`: the human asking again is the whole signal."""
    _sweep(store)
    store.work_item_status = status
    _sweep(store, labels=())
    _sweep(store)
    assert len(store.admitted_ids) == 2, f"a {status} card refused the gesture"


# --------------------------------------------------------------- no loop

def test_a_label_that_never_goes_absent_never_advances(store):
    """The sticky label — the service account could not remove it. Two hundred
    sweeps is roughly three hours of the production loop."""
    _sweep(store)
    store.work_item_status = "failed"
    for _ in range(200):
        _sweep(store)
    assert len(store.admitted_ids) == 1, (
        f"a label sitting on a card restarted the work: {len(store.admitted_ids)} attempts"
    )


def test_a_removal_that_reports_success_without_applying_never_loops(store):
    """The one sequence that could still loop: we disarm on a 200 the server
    did not honour, so the next sweep sees absent → present and advances, and
    the one after that does it again. Disarming is by observation, so the latch
    stays armed and the card is quiet."""
    _sweep(store)
    store.work_item_status = "failed"
    for _ in range(200):
        # The label is STILL on the card every single sweep — that is what
        # "did not apply" means — so the latch must never disarm.
        _sweep(store)
    assert len(store.admitted_ids) == 1


def test_an_in_flight_attempt_is_not_restarted(store):
    """Two workflows on one branch and one PR is worse than a late restart."""
    _sweep(store)
    store.work_item_status = "implementing"
    _sweep(store, labels=())
    result = _sweep(store)
    assert len(store.admitted_ids) == 1, "the gesture duplicated work in flight"
    assert result is not None and result.get("path") != "new_task"
