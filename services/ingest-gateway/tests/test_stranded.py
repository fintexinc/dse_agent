"""Work items whose workflow no longer exists (detection) and the one action
offered for them (escalation to a human).

The incident: four items sitting in `implementing` (x2), `queued` and `new` for
two days, no audit event for ~40 hours, and no workflow in Temporal — not open,
and not closed either, since 24h retention had already purged the history.
Nothing detected it, and nothing would have.

Three properties carry this module and are pinned below.

Detection is READ-ONLY: it never resumes a workflow (a history that is gone
cannot be re-run without risking a repeated coder turn or a reopened PR) and
never writes an audit row, because a timer writing to an append-only ledger
about a non-event is how one stuck item once produced ~2,900 rows.

Detection is also NARROW: silence is only evidence of a lost workflow where
nobody is legitimately waiting on a human. An earlier version of this suite
asserted the opposite for `review_ready`/`merge_pending`/`pr_ready` and would
have escalated every open PR under review — the exact production harm the
statuses below are sorted to prevent.

And escalation is IDEMPOTENT in a way that does not depend on the status
sticking: the guard is the ledger, not `work_items.status`, because any writer of
that column can un-escalate an item without writing an audit row — and then the
timer re-arms with nothing in the ledger to say so.

Postgres is faked. A fake that implements a guard on its own proves only that the
fake has it, so the escalation fake READS the statement it was given and applies
the ledger guard only when the real SQL still carries the predicate — delete it
from `stranded.py` and the idempotency tests below fail.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dse_contracts.work_item import WorkItemStatus

from ingest_gateway import stranded as stranded_mod
from ingest_gateway.stranded import (
    STRANDED_ESCALATION_ACTION,
    STRANDED_ESCALATION_REASON,
    STRANDED_HUMAN_WAIT_STATUSES,
    STRANDED_TERMINAL_STATUSES,
    escalate_stranded,
    stranded_work_items,
)

NOW = datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc)
IDLE_40H = 40 * 3600


@pytest.fixture(autouse=True)
def _cleanup_test_rows():
    """Overrides the conftest fixture of the same name: nothing in this module
    touches Postgres, and that fixture opens a real connection at teardown."""
    yield


@pytest.fixture
def audit_rows(monkeypatch) -> list[dict]:
    """Captures every ledger write attempted by this module. An empty list is a
    real assertion here, not the absence of one."""
    rows: list[dict] = []

    def _emit(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(stranded_mod, "audit_emit", _emit)
    return rows


# --- detection ---------------------------------------------------------------


class _RecordingCursor:
    def __init__(self, recorder: "_RecordingConn") -> None:
        self._recorder = recorder

    def execute(self, sql, params=None):
        self._recorder.statements.append((sql, params))

    def fetchall(self):
        return self._recorder.rows

    def fetchone(self):
        return self._recorder.rows[0] if self._recorder.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingConn:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _db_row(
    work_item_id: str = "wi_stuck",
    *,
    status: str = "implementing",
    source: str = "slack",
    last_event_at: datetime | None = None,
    idle_seconds: int = IDLE_40H,
) -> tuple:
    return (
        work_item_id,
        status,
        source,
        {"channel": "C1", "thread_ts": "1784000000.000100"},
        last_event_at,
        idle_seconds,
    )


def _detection_params(conn: _RecordingConn) -> tuple:
    """The parameter tuple of the detection SELECT, in statement order:
    (escalation action, tenant, excluded statuses, idle seconds, limit)."""
    (_sql, params) = conn.statements[0]
    return params


def test_detection_returns_enough_for_a_caller_to_act():
    last_event = NOW - timedelta(seconds=IDLE_40H)
    conn = _RecordingConn([_db_row(last_event_at=last_event)])

    items = stranded_work_items(conn, tenant_id="t1", idle_for_seconds=3600, limit=10)

    assert items == [
        {
            "work_item_id": "wi_stuck",
            "status": "implementing",
            "source": "slack",
            "source_ref": {"channel": "C1", "thread_ts": "1784000000.000100"},
            "last_event_at": last_event,
            "idle_seconds": IDLE_40H,
        }
    ]


def test_an_item_with_no_audit_row_at_all_still_reports_idle_time():
    """The `new` item from the incident had nothing after admission. A caller
    must get a number to put in the escalation row, not None."""
    conn = _RecordingConn([_db_row("wi_never_touched", status="new", last_event_at=None)])

    (item,) = stranded_work_items(conn, tenant_id="t1", idle_for_seconds=3600, limit=10)

    assert item["last_event_at"] is None
    assert item["idle_seconds"] == IDLE_40H


def test_detection_writes_nothing(audit_rows):
    """The whole point of splitting detection from action: a sweep that finds
    nothing new must leave no trace, on a timer, in an append-only ledger."""
    conn = _RecordingConn([_db_row()])

    stranded_work_items(conn, tenant_id="t1", idle_for_seconds=3600, limit=10)

    assert audit_rows == []
    assert conn.commits == 0
    assert all(sql.strip().upper().startswith("SELECT") for sql, _ in conn.statements)


def test_terminal_statuses_are_excluded_by_the_query():
    """`done/failed/blocked/escalated` are over: there is no next step to owe."""
    conn = _RecordingConn([])

    stranded_work_items(conn, tenant_id="t1", idle_for_seconds=3600, limit=10)

    excluded = set(_detection_params(conn)[2])
    assert {"done", "failed", "blocked", "escalated"} <= excluded
    assert set(STRANDED_TERMINAL_STATUSES) | set(STRANDED_HUMAN_WAIT_STATUSES) == excluded


# Every status where a LIVE workflow parks on `workflow.wait_condition` waiting
# for a person, with the reason the park writes nothing to the ledger while it
# lasts. Verified against services/orchestrator/src/dse_orchestrator/workflows.py.
_HUMAN_PARKS = {
    "needs_clarification": "the intake gate asked a question and waits for the answer",
    "awaiting_repo_selection": "the requester still has to pick a repository",
    "awaiting_plan_approval": "a named approver has to approve the plan",
    "review_ready": "_set_status(review_ready, 'awaiting_human_review') then an "
                    "untimed wait_condition on the reviewer's verdict",
    "merge_pending": "_set_status(merge_pending, 'approved_awaiting_merge') then "
                     "wait_condition(self._merged) until a human merges the PR",
    "pr_ready": "pre-patch alias of pr_open/review_ready/merge_pending, and two of "
                "those three are unbounded human parks — executions from before "
                "fine-pr-ci-states-v1 are still in flight",
}

# Statuses that are NOT excluded, and why silence there is a real symptom: the
# workflow itself owes the next step, so nobody is waiting on a human.
_OWES_A_NEXT_STEP = {
    "new": "admitted and nothing has picked it up — one of the four incident items",
    "ready": "clarification complete; the dispatcher owes a start",
    "queued": "an incident item: dispatched and never started",
    "implementing": "two incident items sat here for two days",
    "validating": "an L1 run is in progress or was lost with the workflow",
    "pr_open": "the evidence pipeline runs, then the review loop starts — transient",
    "ci_pending": "a CI poll every ci_poll_interval_seconds writes ci_status_observed",
    "review_feedback": "a fix cycle is running on the same branch",
}


@pytest.mark.parametrize("status,why", sorted(_HUMAN_PARKS.items()))
def test_statuses_where_a_live_workflow_waits_on_a_human_are_excluded(status, why):
    """Silence here is the SPECIFIED behaviour, not a lost workflow: the workflow
    writes one audit row, parks on a wait_condition, and stays quiet for as long
    as the person takes. A weekend-long code review is the most normal state this
    machine has, and the first version of this sweep would have escalated every
    one of them — moving live work with an open PR to a terminal status.
    """
    conn = _RecordingConn([])

    stranded_work_items(conn, tenant_id="t1", idle_for_seconds=3600, limit=10)

    excluded = set(_detection_params(conn)[2])
    assert status in excluded, f"{status}: {why}"
    assert status in STRANDED_HUMAN_WAIT_STATUSES


@pytest.mark.parametrize("status,why", sorted(_OWES_A_NEXT_STEP.items()))
def test_statuses_that_owe_a_next_step_stay_detectable(status, why):
    """The other side of the same line. `pr_open`/`ci_pending`/`review_feedback`
    look like waiting too, but what they wait for is the engine: an evidence
    pipeline, a CI poll that writes an audit row every cycle, a fix turn. Silence
    there means the producer is gone, which is exactly what this sweep is for.
    """
    conn = _RecordingConn([])

    stranded_work_items(conn, tenant_id="t1", idle_for_seconds=3600, limit=10)

    excluded = set(_detection_params(conn)[2])
    assert status not in excluded, f"{status}: {why}"


def test_every_status_in_the_lifecycle_is_classified_one_way_or_the_other():
    """A status in both tuples, or in neither, is an undecided question — and the
    default answer is "escalate it", which is the harmful one. Comparing against
    the real enum is what makes a NEW status a test failure here rather than a
    surprise escalation in production.

    `awaiting_repo_selection` is not a `WorkItemStatus` member (work_items.status
    has no CHECK constraint and the adapters write/render this one — see
    `reconcile.RECOVERABLE_STATUSES` and adapter-slack), so it is added
    explicitly rather than dropped.
    """
    classified = set(_HUMAN_PARKS) | set(_OWES_A_NEXT_STEP) | set(STRANDED_TERMINAL_STATUSES)

    assert not (set(STRANDED_TERMINAL_STATUSES) & set(STRANDED_HUMAN_WAIT_STATUSES))
    assert set(STRANDED_HUMAN_WAIT_STATUSES) == set(_HUMAN_PARKS)
    assert classified == {s.value for s in WorkItemStatus} | {"awaiting_repo_selection"}


def test_detection_suppresses_an_item_this_sweep_already_reported():
    """Idempotency cannot rest on the status. `escalated` is a value in a mutable
    column with no CHECK constraint and more than one writer; a bare status
    projection that moves the item back to `implementing` writes NO audit row, and
    the item is then silent, detectable, and escalated again — ~2,900 rows. The
    orchestrator's writer now refuses that write, but this side does not depend on
    it: the query asks the ledger, which cannot be rewritten."""
    conn = _RecordingConn([])

    stranded_work_items(conn, tenant_id="t1", idle_for_seconds=3600, limit=10)

    sql, params = conn.statements[0]
    assert params[0] == STRANDED_ESCALATION_ACTION
    assert "FILTER (WHERE a.action = %s) AS last_escalation_at" in sql
    # ... and it un-suppresses as soon as anything else writes about the item, so
    # a genuine second stranding is still reported (once).
    assert "ev.last_escalation_at < ev.last_event_at" in sql
    assert "ev.last_escalation_at IS NULL" in sql


def test_threshold_and_limit_reach_the_query():
    conn = _RecordingConn([])

    stranded_work_items(conn, tenant_id="t1", idle_for_seconds=IDLE_40H, limit=7)

    params = _detection_params(conn)
    assert params[1] == "t1"
    assert params[3] == float(IDLE_40H)
    assert params[4] == 7


@pytest.mark.parametrize("idle_for_seconds", [0, -1])
def test_a_non_positive_threshold_is_refused(idle_for_seconds):
    """With a zero threshold the query returns every in-flight item in the
    tenant, and the caller escalates what it is handed — one config typo would
    hand a whole tenant's live queue to a human as failed work."""
    with pytest.raises(ValueError):
        stranded_work_items(
            _RecordingConn([]), tenant_id="t1", idle_for_seconds=idle_for_seconds, limit=10
        )


def test_a_non_positive_limit_is_refused():
    with pytest.raises(ValueError):
        stranded_work_items(_RecordingConn([]), tenant_id="t1", idle_for_seconds=3600, limit=0)


# --- escalation --------------------------------------------------------------


# The ledger guard as the real UPDATE has to spell it: "no escalation row of ours
# yet, or something else wrote after it". The fake below applies the guard only if
# BOTH fragments are in the statement it is handed, so a suite that claims to pin
# the guard cannot go on passing once the predicate leaves the SQL — which is
# exactly what a fake implementing the guard by itself allowed.
_LEDGER_GUARD_SQL = (
    "l.last_escalation_at IS NULL",
    "l.last_escalation_at < l.last_event_at",
)


class _FakeWorkItems:
    """Applies the two guards the real UPDATE carries in its WHERE clauses.

    (1) the status guard: the change happens only while the item is neither
    terminal nor waiting on a human, and the statement reports the status it had
    before. (2) the ledger guard: it does not happen at all when this sweep's own
    escalation row is already the newest thing in the item's audit_log — and this
    one is applied ONLY when the statement contains `_LEDGER_GUARD_SQL`, so the
    tests below are pinned to the real predicate rather than to this class.

    `state_version` and `last_transition_at` are tracked so the canonical-writer
    invariant can be asserted rather than assumed.
    """

    def __init__(
        self,
        statuses: dict[str, str],
        *,
        tenant_id: str = "t1",
        ledger: dict[str, list[str]] | None = None,
    ) -> None:
        self.statuses = statuses
        self.tenant_id = tenant_id
        # work_item_id -> actions in ledger order (newest last)
        self.ledger = ledger if ledger is not None else {}
        self.state_versions = {wi: 0 for wi in statuses}
        self.transitions = {wi: 0 for wi in statuses}
        self.updates = 0
        self.commits = 0
        self.rollbacks = 0
        self.statements: list[str] = []
        self._row: tuple | None = None

    # connection protocol
    def cursor(self):
        return self

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    # cursor protocol
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        (work_item_id, tenant_id, excluded, escalation_action,
         ledger_tenant_id, ledger_work_item_id) = params
        assert ledger_tenant_id == tenant_id and ledger_work_item_id == work_item_id
        status = self.statuses.get(work_item_id)
        if status is None or tenant_id != self.tenant_id or status in excluded:
            self._row = None
            return
        if all(fragment in sql for fragment in _LEDGER_GUARD_SQL):
            actions = self.ledger.get(work_item_id, [])
            if actions and actions[-1] == escalation_action:
                # `last_escalation_at` is the newest ts: nothing happened since we
                # reported this item, so there is nothing new to report.
                self._row = None
                return
        self.statuses[work_item_id] = "escalated"
        self.state_versions[work_item_id] += 1
        self.transitions[work_item_id] += 1
        self.ledger.setdefault(work_item_id, []).append(escalation_action)
        self.updates += 1
        self._row = (status,)

    def fetchone(self):
        return self._row


def test_escalation_moves_the_item_and_names_the_cause(audit_rows):
    conn = _FakeWorkItems({"wi_stuck": "implementing"})

    escalated = escalate_stranded(
        conn,
        work_item_id="wi_stuck",
        tenant_id="t1",
        idle_seconds=IDLE_40H,
        actor="system:ingest-gateway-stranded-sweep",
    )

    assert escalated is True
    assert conn.statuses["wi_stuck"] == "escalated"
    assert conn.commits == 1
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["action"] == STRANDED_ESCALATION_ACTION
    assert row["actor"] == "system:ingest-gateway-stranded-sweep"
    assert row["work_item_id"] == "wi_stuck"
    assert row["tenant_id"] == "t1"
    assert row["details"] == {
        "reason": STRANDED_ESCALATION_REASON,
        "status_before": "implementing",
        "idle_seconds": IDLE_40H,
    }
    # Same transaction as the status change: an escalation whose audit row was
    # lost is an item a human owns with nothing saying why.
    assert row["conn"] is conn


def test_the_status_change_advances_state_version_like_the_canonical_writer(audit_rows):
    """This is the only UPDATE of `work_items` outside
    `local_activities.update_work_item_status`, which bumps `state_version` and
    stamps `last_transition_at` on exactly the writes that change the status.
    A status change that leaves `state_version` behind makes every cached read of
    it look current."""
    conn = _FakeWorkItems({"wi_stuck": "implementing"})

    escalate_stranded(
        conn, work_item_id="wi_stuck", tenant_id="t1", idle_seconds=IDLE_40H, actor="system:x"
    )

    assert conn.state_versions["wi_stuck"] == 1
    assert conn.transitions["wi_stuck"] == 1
    sql = conn.statements[0]
    assert "state_version = wi.state_version + 1" in sql
    assert "last_transition_at = now()" in sql


def test_a_skipped_item_does_not_advance_state_version(audit_rows):
    """The bump belongs to the transition, not to the attempt."""
    conn = _FakeWorkItems({"wi_done": "done"})

    escalate_stranded(
        conn, work_item_id="wi_done", tenant_id="t1", idle_seconds=IDLE_40H, actor="system:x"
    )

    assert conn.state_versions["wi_done"] == 0


def test_escalating_twice_is_a_no_op(audit_rows):
    """This runs on a timer. A second pass over the same item must not write a
    second row — the ledger is append-only and one looping item once buried
    thirteen hours of console timeline under ~2,900 rows."""
    conn = _FakeWorkItems({"wi_stuck": "queued"})
    kwargs = dict(work_item_id="wi_stuck", tenant_id="t1", idle_seconds=IDLE_40H, actor="system:x")

    assert escalate_stranded(conn, **kwargs) is True
    assert escalate_stranded(conn, **kwargs) is False

    assert len(audit_rows) == 1
    assert conn.updates == 1
    assert conn.commits == 1


def test_an_item_un_escalated_with_no_audit_row_is_not_escalated_again(audit_rows):
    """The failure the ledger guard exists for: something moves the item out of
    `escalated` and writes nothing to the ledger (a redelivered Activity, a
    replaying workflow, an operator's UPDATE). If suppression depended on the
    status, the timer would escalate it again on the next cycle, and again after
    the next such write — with no ledger evidence that it was a loop."""
    conn = _FakeWorkItems({"wi_stuck": "implementing"})
    kwargs = dict(work_item_id="wi_stuck", tenant_id="t1", idle_seconds=IDLE_40H, actor="system:x")

    assert escalate_stranded(conn, **kwargs) is True
    conn.statuses["wi_stuck"] = "implementing"  # the un-guarded COALESCE write

    assert escalate_stranded(conn, **kwargs) is False
    assert len(audit_rows) == 1


def test_an_item_that_came_back_to_life_and_stranded_again_is_reported_once_more(audit_rows):
    """Suppression must not be permanent: the guard is "our row is the newest",
    not "our row exists". Real activity (any audit row from anyone else) means the
    item genuinely restarted, and a second stranding is a second incident."""
    conn = _FakeWorkItems({"wi_stuck": "implementing"})
    kwargs = dict(work_item_id="wi_stuck", tenant_id="t1", idle_seconds=IDLE_40H, actor="system:x")

    assert escalate_stranded(conn, **kwargs) is True
    conn.statuses["wi_stuck"] = "implementing"
    conn.ledger["wi_stuck"].append("sandbox_provisioned")  # somebody really worked on it

    assert escalate_stranded(conn, **kwargs) is True
    assert len(audit_rows) == 2


@pytest.mark.parametrize("status", list(STRANDED_TERMINAL_STATUSES))
def test_a_terminal_item_is_never_re_escalated(status, audit_rows):
    conn = _FakeWorkItems({"wi_done": status})

    assert escalate_stranded(
        conn, work_item_id="wi_done", tenant_id="t1", idle_seconds=IDLE_40H, actor="system:x"
    ) is False
    assert conn.statuses["wi_done"] == status
    assert audit_rows == []


@pytest.mark.parametrize("status", list(STRANDED_HUMAN_WAIT_STATUSES))
def test_an_item_that_moved_to_a_human_wait_is_left_alone(status, audit_rows):
    """The window between detection and action is real: the item may have asked a
    question, opened a PR for review, or been approved and be waiting on a merge
    while the sweep was iterating. The guard must not yank it into a terminal
    status behind the reviewer's back."""
    conn = _FakeWorkItems({"wi_moved": status})

    assert escalate_stranded(
        conn, work_item_id="wi_moved", tenant_id="t1", idle_seconds=IDLE_40H, actor="system:x"
    ) is False
    assert conn.statuses["wi_moved"] == status
    assert audit_rows == []


def test_another_tenants_item_is_not_escalated(audit_rows):
    conn = _FakeWorkItems({"wi_stuck": "implementing"}, tenant_id="t1")

    assert escalate_stranded(
        conn, work_item_id="wi_stuck", tenant_id="t2", idle_seconds=IDLE_40H, actor="system:x"
    ) is False
    assert conn.statuses["wi_stuck"] == "implementing"
    assert audit_rows == []


def test_a_no_op_does_not_hold_the_transaction_open():
    """Most items on most cycles are skipped; leaving each skip idle-in-transaction
    would pin a snapshot for the whole sweep and keep vacuum away from
    work_items."""
    conn = _FakeWorkItems({"wi_done": "done"})

    escalate_stranded(
        conn, work_item_id="wi_done", tenant_id="t1", idle_seconds=IDLE_40H, actor="system:x"
    )

    assert conn.rollbacks == 1
    assert conn.commits == 0


def test_a_failed_audit_write_rolls_the_status_change_back(monkeypatch):
    """Status and ledger row commit together or not at all. A committed
    escalation with no audit row would also be invisible to the next cycle's
    reasoning about what has already been reported — and that reasoning now READS
    the ledger, so a missing row would re-arm the timer."""
    conn = _FakeWorkItems({"wi_stuck": "implementing"})

    def _boom(**kwargs):
        raise RuntimeError("audit_log unavailable")

    monkeypatch.setattr(stranded_mod, "audit_emit", _boom)

    with pytest.raises(RuntimeError):
        escalate_stranded(
            conn, work_item_id="wi_stuck", tenant_id="t1", idle_seconds=IDLE_40H, actor="system:x"
        )

    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_the_console_projector_maps_this_actions_literal():
    """The action name is a cross-service string contract and neither side can
    import the other: `console-projector` has the literal typed out in
    `AUDIT_EVENT_MAP`, this package owns the constant. Renaming it here without
    touching that map would silently downgrade the escalation to a `note` on the
    console timeline (its fallback for unknown actions), so the map is read from
    source and checked against the constant.
    """
    mappers = (
        Path(__file__).resolve().parents[2]
        / "console-projector" / "console_projector" / "mappers.py"
    )
    assignment = next(
        node
        for node in ast.parse(mappers.read_text()).body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "AUDIT_EVENT_MAP"
    )
    audit_event_map = ast.literal_eval(assignment.value)

    assert audit_event_map.get(STRANDED_ESCALATION_ACTION) == "error", (
        f"console-projector's AUDIT_EVENT_MAP has no 'error' entry for "
        f"{STRANDED_ESCALATION_ACTION!r}; unmapped actions become 'note' there"
    )


def test_escalation_target_is_the_terminal_escalated_status(audit_rows):
    """`escalated` means "handed to a human", never "will be retried" — the
    module offers no resume, so the status must not suggest one."""
    conn = _FakeWorkItems({"wi_stuck": "validating"})

    escalate_stranded(
        conn, work_item_id="wi_stuck", tenant_id="t1", idle_seconds=IDLE_40H, actor="system:x"
    )

    assert "escalated" in STRANDED_TERMINAL_STATUSES
    assert "status = 'escalated'" in conn.statements[0]
    assert "FOR UPDATE" in conn.statements[0]
