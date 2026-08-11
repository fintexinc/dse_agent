"""The `dse-retry` label: restart a work item that ended terminally.

Before this, such a work item could only be retried by editing the database by
hand — re-adding the trigger label converges on the `created:{issue id}` event_id
that already belongs to the attempt that ended, so it does nothing at all.

The dangerous part is not starting the retry, it is starting it ONCE. The poller
sweeps every minute and the label does not remove itself, so the naive version
restarts the task forever. That exact shape — a timer re-triggering the same
action — is what turned one stuck item into ~2,900 audit rows, and the audit log
is append-only, so there is no cleaning it up afterwards.

Crucially, the guard cannot be "the label was removed": removing a Jira label
needs the *Edit Issues* permission, which the service account may not have. Keying
the event_id on the work item being retried was not enough either — attempt fails,
retry admitted, retry fails, the retry is now the prior item, so the next sweep
derived a NEW key and admitted another attempt, forever. So the whole file leans on
one property: the evidence lives in Postgres (`ingest_events.event_id`, UNIQUE,
committed with the work item), and `test_two_hundred_sweeps_...` proves it with
label removal failing on every single call.

No Postgres: `get_connection` is replaced by a fake that answers the handful of
queries this path runs, which lets the real `build_retry_event`,
`recorded_work_item_id` and `admit_work_item` run against it (their idempotency
keys are what the single-shot property rests on, so they must not be mocked away).
"""
from __future__ import annotations

import pytest

from adapter_jira import config, events
from adapter_jira.backend import FakeJiraClient
from adapter_jira.ingest import _RETRY_ELIGIBLE_STATUSES, ingest_retry_trigger
from adapter_jira.poller import JiraPoller

from .helpers import FakeConn

TENANT = "test_tenant_jira_adapter"
SITE = "https://acme.atlassian.net"
REPORTER = {"accountId": "acc_reporter", "displayName": "Rita"}
POLLER_ACTOR = "system:adapter-jira-poller"

IN_FLIGHT_STATUSES = [
    "new", "needs_clarification", "ready", "queued", "awaiting_plan_approval",
    "implementing", "validating", "pr_open", "ci_pending", "review_ready",
    "merge_pending", "pr_ready", "review_feedback",
]


def _issue(key="DSE-300", issue_id="10300", *, labels=None, status="To Do"):
    return {
        "id": issue_id,
        "key": key,
        "self": f"{SITE}/rest/api/3/issue/{issue_id}",
        "fields": {
            "summary": "Fix the importer",
            "description": "it crashes on empty rows",
            "labels": list(labels or []),
            "status": {"name": status},
            "project": {"key": key.split("-")[0]},
            "reporter": dict(REPORTER),
        },
    }


class FakeDb:
    """The rows this path reads, plus whatever it writes.

    `ingest_events` is a real dict rather than a canned answer so that a SECOND
    call in the same test dedupes for real, through the same `event_id` the
    production code derives. Same for `emitted`: the decline guard reads the
    ledger back, so "one row per (ticket, reason), ever" can only be proven if the
    fake remembers what was emitted.
    """

    def __init__(self, *, work_item: tuple | None = None, channel_killed: str | None = None):
        self.work_item = work_item  # (id, status, repo, base_branch)
        self.channel_killed = channel_killed
        self.ingest_events: dict[str, str] = {}
        self.work_items: list[tuple] = []
        self.inserted_audit: list[tuple] = []  # rows the REAL gateway writes
        self.emitted: list[dict] = []  # rows the adapter emits via audit_emit

    def emit(self, **kwargs) -> None:
        self.emitted.append(kwargs)

    def declined(self, reason: str | None = None) -> list[dict]:
        return [
            r
            for r in self.emitted
            if r["action"] == "jira_retry_declined"
            and (reason is None or r["details"].get("reason") == reason)
        ]

    def responder(self, sql: str, params):
        if sql.startswith("SELECT id, status, repo, base_branch FROM work_items"):
            return [self.work_item] if self.work_item else []
        if sql.startswith("SELECT work_item_id FROM ingest_events"):
            recorded = self.ingest_events.get(params[0])
            return [(recorded,)] if recorded else []
        if sql.startswith("SELECT 1 FROM audit_log"):
            # The work item predicate is spelled inline (index usability), so the
            # first four parameters are the stable part of the key.
            _tenant, action, ticket, reason = params[:4]
            work_item = params[4] if len(params) > 4 else None
            hit = any(
                r["action"] == action
                and r["work_item_id"] == work_item
                and r["details"].get("ticket_key") == ticket
                and r["details"].get("reason") == reason
                for r in self.emitted
            )
            return [(1,)] if hit else []
        if sql.startswith("SELECT kill_switch_enabled"):
            return []
        if sql.startswith("SELECT active, reason FROM channel_kill_switches"):
            return [(True, self.channel_killed)] if self.channel_killed else []
        if sql.startswith("INSERT INTO ingest_events"):
            self.ingest_events[params[1]] = params[0]
            return []
        if sql.startswith("INSERT INTO work_items"):
            self.work_items.append(params)
            return []
        if sql.startswith("INSERT INTO audit_log"):
            self.inserted_audit.append(params)
            return []
        raise AssertionError(f"unexpected statement: {sql}")


@pytest.fixture
def db(monkeypatch):
    """Wires `FakeDb` in and hands back a factory so a test can pick the row the
    work_items lookup should return."""

    def _install(*, work_item=None, channel_killed=None) -> FakeDb:
        store = FakeDb(work_item=work_item, channel_killed=channel_killed)
        conn = FakeConn(store.responder)
        monkeypatch.setattr("adapter_jira.ingest.get_connection", lambda: conn)
        monkeypatch.setattr("adapter_jira.ingest.audit_emit", store.emit)
        monkeypatch.setattr(
            "adapter_jira.ingest.resolve_repo",
            lambda conn, **kw: ("acme/resolved-by-cascade", "main", []),
        )
        store.conn = conn
        return store

    return _install


def _retry(issue) -> dict:
    return ingest_retry_trigger(
        issue,
        tenant_id=TENANT,
        actor_account_id=REPORTER["accountId"],
        resolved_principal="user:rita",
        display_name=REPORTER["displayName"],
    )


# --- which statuses qualify -------------------------------------------------


@pytest.mark.parametrize("status", _RETRY_ELIGIBLE_STATUSES)
def test_a_terminal_unsuccessful_work_item_is_retried(db, status):
    """`failed`, `blocked` and `escalated` are terminal and not a success, so
    nothing is running that a fresh attempt could race.

    `blocked` and `escalated` qualify deliberately: their own status comments send
    the human back to the ticket ("adjust CODEOWNERS", "re-apply the `dse` label to
    try again"), and re-applying `dse` provably does nothing. This label is the only
    lever they have, and the new attempt still goes through every gate."""
    store = db(work_item=(f"wi_{status}", status, "acme/api", "develop"))

    result = _retry(_issue())

    assert result["path"] == "retried"
    assert result["previous_work_item_id"] == f"wi_{status}"
    assert result["work_item_id"] != f"wi_{status}"  # a NEW attempt, not a resurrection
    assert len(store.work_items) == 1


def test_done_is_never_retried(db):
    """The work merged. A second attempt re-implements a change that shipped and
    opens a second PR for it."""
    store = db(work_item=("wi_done", "done", "acme/api", "main"))

    assert _retry(_issue())["path"] == "not_retryable"
    assert store.work_items == []


@pytest.mark.parametrize("status", IN_FLIGHT_STATUSES)
def test_an_in_flight_item_is_never_retried(db, status):
    """Two workflows would race for the same branch and PR."""
    store = db(work_item=(f"wi_{status}", status, "acme/api", "main"))

    result = _retry(_issue())

    assert result["path"] == "not_retryable"
    assert result["status"] == status
    assert store.work_items == []


def test_a_ticket_the_dse_never_took_on_is_not_retried(db):
    store = db(work_item=None)

    assert _retry(_issue())["path"] == "no_work_item"
    assert store.work_items == []


# --- single-shot ------------------------------------------------------------


def test_the_same_ticket_is_only_ever_admitted_once(db):
    """The hard guarantee, independent of the label ever being removed: the
    event_id is derived from the TICKET, so every later sweep recognises the retry
    it already admitted."""
    store = db(work_item=("wi_failed", "failed", "acme/api", "main"))

    first = _retry(_issue())
    assert first["path"] == "retried"

    for _ in range(20):
        again = _retry(_issue())
        assert again["path"] == "already_retried"
        assert again["work_item_id"] == first["work_item_id"]

    assert len(store.work_items) == 1
    assert len([r for r in store.emitted if r["action"] == "jira_retry_admitted"]) == 1


def test_a_retry_that_also_fails_does_not_buy_another_retry(db):
    """The BLOCKER. With the event_id keyed on the work item being retried, this
    loop was unbounded: the retry becomes the newest work item, it fails, the key
    changes, and the next sweep admits attempt number three — with nobody asking,
    once per minute, for as long as the label sits there."""
    store = db(work_item=("wi_failed", "failed", "acme/api", "main"))

    first = _retry(_issue())
    assert first["path"] == "retried"

    for _ in range(50):
        # The attempt just admitted is now the newest work item, and it failed too.
        store.work_item = (first["work_item_id"], "failed", "acme/api", "main")
        assert _retry(_issue())["path"] == "already_retried"

    assert len(store.work_items) == 1


def test_the_retry_event_id_is_deterministic_and_distinct():
    issue = _issue()
    build = lambda i: events.build_retry_event(  # noqa: E731 — three near-identical calls
        i, actor_account_id="acc", resolved_principal="user:rita"
    )

    assert build(issue).event_id == build(issue).event_id  # stable across sweeps
    # Stable even when the ticket's own state moves on: nothing about the retry key
    # may depend on what the poller happens to read this minute.
    assert build(issue).event_id == build(_issue(status="Done", labels=["x"])).event_id
    assert build(issue).event_id != build(_issue(key="DSE-301", issue_id="10301")).event_id

    # Must NOT collide with the original admission: that event_id already belongs
    # to the attempt that ended, and `admit_work_item` would dedupe the retry
    # into silence.
    original = events.build_task_event(issue, actor_account_id="acc", resolved_principal="user:rita")
    assert build(issue).event_id != original.event_id
    assert build(issue).kind == original.kind  # still a task_request: it starts a workflow


def test_the_retry_carries_the_ticket_text_the_agents_need():
    ev = events.build_retry_event(_issue(), actor_account_id="acc", resolved_principal="user:rita")
    assert "Fix the importer" in ev.content_snapshot
    assert "crashes on empty rows" in ev.content_snapshot
    assert ev.source_ref == {"ticket_key": "DSE-300"}


# --- the human always gets an answer, and only one -------------------------


@pytest.mark.parametrize(
    ("work_item", "reason"),
    [
        (None, "no_work_item"),
        (("wi_done", "done", "acme/api", "main"), "status_not_retryable"),
    ],
)
def test_a_label_that_will_not_be_acted_on_is_answered_exactly_once(db, work_item, reason):
    """A label the DSE ignores in silence forever was the LOW finding: no audit
    row, no comment, the label never removed, and the poller re-running the lookup
    every minute. One row answers it; a row per sweep would be the ~2,900-row
    failure with a friendlier name."""
    store = db(work_item=work_item)

    first = _retry(_issue())
    assert first["recorded"] is True

    for _ in range(60):
        assert _retry(_issue())["recorded"] is False

    (row,) = store.declined()
    assert row["details"]["reason"] == reason
    assert row["details"]["ticket_key"] == "DSE-300"
    assert row["actor"] == POLLER_ACTOR


def test_the_decline_row_names_the_status_and_what_would_have_qualified(db):
    store = db(work_item=("wi_impl", "implementing", "acme/api", "main"))

    _retry(_issue())

    (row,) = store.declined("status_not_retryable")
    assert row["work_item_id"] == "wi_impl"
    assert row["details"]["status"] == "implementing"
    assert row["details"]["retryable_statuses"] == list(_RETRY_ELIGIBLE_STATUSES)


def test_a_stale_label_after_a_retry_is_answered_once_and_then_ignored(db):
    """The label could not be removed (no *Edit Issues* permission). The human gets
    told the ticket's retry is spent — once — and then the path goes quiet."""
    store = db(work_item=("wi_failed", "failed", "acme/api", "main"))
    _retry(_issue())

    results = [_retry(_issue()) for _ in range(40)]

    assert {r["path"] for r in results} == {"already_retried"}
    assert [r["recorded"] for r in results].count(True) == 1
    (row,) = store.declined("retry_already_used")
    assert row["work_item_id"] == results[0]["work_item_id"]


def test_the_decline_reasons_are_a_finite_set(db):
    """Bounded from above: the guard is per (ticket, reason), so a ticket cannot
    accumulate more rows than there are reasons, no matter how long it lives."""
    store = db(work_item=None)
    _retry(_issue())  # no_work_item

    store.work_item = ("wi_impl", "implementing", "acme/api", "main")
    for _ in range(10):
        _retry(_issue())  # status_not_retryable

    store.work_item = ("wi_impl", "failed", "acme/api", "main")
    _retry(_issue())  # retried
    for _ in range(10):
        _retry(_issue())  # retry_already_used

    assert [r["details"]["reason"] for r in store.declined()] == [
        "no_work_item",
        "status_not_retryable",
        "retry_already_used",
    ]


# --- what the retry inherits ------------------------------------------------


def test_the_repo_of_the_failed_attempt_is_inherited(db):
    """On a ticket whose repo came from a human answering the ambiguity
    clarification, re-running the cascade would land back on ambiguous and ask
    the same question again."""
    store = db(work_item=("wi_failed", "failed", "acme/chosen-by-human", "release/7"))

    _retry(_issue())

    inserted = store.work_items[0]
    assert "acme/chosen-by-human" in inserted
    assert "release/7" in inserted


def test_with_no_inherited_repo_the_cascade_still_runs(db):
    store = db(work_item=("wi_failed", "failed", None, None))

    _retry(_issue())

    assert "acme/resolved-by-cascade" in store.work_items[0]


def test_the_audit_row_names_the_reason_and_the_failed_attempt(db):
    store = db(work_item=("wi_failed", "failed", "acme/api", "main"))

    result = _retry(_issue())

    (row,) = [r for r in store.emitted if r["action"] == "jira_retry_admitted"]
    assert row["work_item_id"] == result["work_item_id"]
    assert row["details"]["reason"] == "retry_label"
    assert row["details"]["previous_work_item_id"] == "wi_failed"
    assert row["details"]["previous_status"] == "failed"
    assert row["details"]["ticket_key"] == "DSE-300"


def test_the_retry_is_attributed_to_the_poller_not_to_the_reporter(db):
    """The poller reads current state, never the changelog, so it cannot know who
    added the label. Naming the REPORTER blames a person who may never have asked,
    in a table with no UPDATE (migrations/0028). The reporter stays the work item's
    requester (BD-39) — that is a different claim, and it is recorded as one."""
    store = db(work_item=("wi_failed", "failed", "acme/api", "main"))

    _retry(_issue())

    (row,) = [r for r in store.emitted if r["action"] == "jira_retry_admitted"]
    assert row["actor"] == POLLER_ACTOR
    assert row["details"]["requester"] == "user:rita"


def test_the_admission_audit_row_joins_the_admission_transaction(db):
    """The HIGH finding: without `conn=conn` this opened a second connection after
    `admit_work_item` had committed, so a failure there left a retry running with
    nothing in the ledger calling it one."""
    store = db(work_item=("wi_failed", "failed", "acme/api", "main"))

    _retry(_issue())

    (row,) = [r for r in store.emitted if r["action"] == "jira_retry_admitted"]
    assert row["conn"] is store.conn
    assert store.conn.commits >= 1  # and the caller commits it, rather than leaving it open


def test_the_kill_switch_still_wins_and_does_not_consume_the_label(db):
    """A paused channel is paused for retries too — otherwise the label is a way
    around the switch. It writes NOTHING: `admit_work_item` audits every blocked
    admission, which on a timer against a label that stays put is one row per
    sweep. A pause is temporary, so the request survives it."""
    store = db(work_item=("wi_failed", "failed", "acme/api", "main"), channel_killed="incident_2026_07")

    for _ in range(30):
        result = _retry(_issue())
        assert result["path"] == "blocked_kill_switch"
        assert result["recorded"] is False

    assert store.work_items == []
    assert store.emitted == []
    assert store.conn.writes() == []

    # …and once the operator resumes the channel, the retry that was waiting runs.
    store.channel_killed = None
    assert _retry(_issue())["path"] == "retried"


# --- the poller side --------------------------------------------------------


@pytest.fixture
def poller_db(monkeypatch):
    """The poller's own queries (cursor + pending replies) answered without a
    database, so `poll_once` can run end to end."""

    def responder(sql: str, params):
        if sql.startswith("SELECT last_polled_at FROM jira_poll_state"):
            return []
        if sql.startswith("INSERT INTO jira_poll_state"):
            return []
        if sql.startswith("SELECT id, source_ref, status"):  # pending-reply recovery
            return []
        raise AssertionError(f"unexpected statement: {sql}")

    conn = FakeConn(responder)
    monkeypatch.setattr("adapter_jira.poller.get_connection", lambda: conn)
    monkeypatch.setattr("adapter_jira.poller.ingest_task_trigger", lambda *a, **kw: {"ok": True})
    # Identity resolution is a Postgres lookup of its own and not what these
    # tests are about; the shape (a principal that is not the poller) is.
    monkeypatch.setattr(
        "adapter_jira.poller.resolve_principal",
        lambda platform, account_id, display=None: f"user:{(display or account_id).lower()}",
    )
    return conn


@pytest.fixture
def poller_audit(monkeypatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setattr("adapter_jira.poller.audit_emit", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture
def retries(monkeypatch) -> list[dict]:
    """Records every retry attempt the poller makes, and answers `retried` the
    first time per ticket — the ingest-level guard is proven above; here what
    matters is how often the poller asks."""
    calls: list[dict] = []
    seen: set[str] = set()

    def _ingest(issue, **kwargs):
        calls.append({"ticket_key": issue["key"], **kwargs})
        if issue["key"] in seen:
            return {"ok": True, "path": "already_retried", "recorded": False, "work_item_id": "wi_retry"}
        seen.add(issue["key"])
        return {
            "ok": True,
            "path": "retried",
            "recorded": True,
            "work_item_id": "wi_retry",
            "previous_work_item_id": "wi_failed",
        }

    monkeypatch.setattr("adapter_jira.poller.ingest_retry_trigger", _ingest)
    return calls


def _poller(client, **kwargs) -> JiraPoller:
    return JiraPoller(
        client,
        tenant_id=TENANT,
        projects=["DSE"],
        trigger_label="dse",
        retry_label="dse-retry",
        approved_status="Plan approved",
        rejected_status="Plan rejected",
        grace_seconds=0,
        reconcile_comments=False,
        **kwargs,
    )


def test_the_label_comes_off_when_a_retry_was_admitted(poller_db, retries):
    issue = _issue(labels=["dse", "dse-retry"])
    fake = FakeJiraClient(issues_by_project={"DSE": [issue]})
    poller = _poller(fake)

    poller.poll_once()
    assert fake.label_removals == [{"key": "DSE-300", "label": "dse-retry"}]

    # Ten more sweeps of the minute-by-minute loop: the label is gone, so the
    # retry path is not even entered again.
    for _ in range(10):
        poller.poll_once()
    assert len(retries) == 1


def test_a_ticket_without_the_retry_label_is_untouched(poller_db, retries):
    fake = FakeJiraClient(issues_by_project={"DSE": [_issue(labels=["dse"])]})
    _poller(fake).poll_once()

    assert retries == []
    assert fake.label_removals == []


def test_the_label_comes_off_on_the_sweep_that_answered_the_human(poller_db, monkeypatch):
    """A decision the human has been told about must not leave the label looking
    pending — and must not be re-answered on the next sweep either."""
    paths = iter(
        [{"ok": True, "path": "not_retryable", "status": "done", "recorded": True}]
        + [{"ok": True, "path": "not_retryable", "status": "done", "recorded": False}] * 5
    )
    monkeypatch.setattr("adapter_jira.poller.ingest_retry_trigger", lambda issue, **kw: next(paths))
    fake = FakeJiraClient(issues_by_project={"DSE": [_issue(labels=["dse-retry"])]})
    fake.remove_label = lambda key, label: fake.label_removals.append({"key": key, "label": label})
    poller = _poller(fake)

    for _ in range(6):
        poller.poll_once()

    assert fake.label_removals == [{"key": "DSE-300", "label": "dse-retry"}]


def test_a_paused_channel_keeps_the_label_pending(poller_db, monkeypatch):
    monkeypatch.setattr(
        "adapter_jira.poller.ingest_retry_trigger",
        lambda issue, **kw: {"ok": True, "path": "blocked_kill_switch", "recorded": False},
    )
    fake = FakeJiraClient(issues_by_project={"DSE": [_issue(labels=["dse-retry"])]})

    for _ in range(5):
        _poller(fake).poll_once()

    assert fake.label_removals == []  # the request survives the pause


def test_the_retry_is_requested_for_the_reporter_not_the_poller(poller_db, retries):
    """BD-39: a work item admitted as `system:adapter-jira-poller` cannot be
    unblocked by the ticket's own author — their answer does not authorize."""
    fake = FakeJiraClient(issues_by_project={"DSE": [_issue(labels=["dse-retry"])]})
    _poller(fake).poll_once()

    assert retries[0]["actor_account_id"] == "acc_reporter"
    assert retries[0]["resolved_principal"] != POLLER_ACTOR
    assert retries[0]["display_name"] == "Rita"


def test_a_failed_label_removal_is_audited_and_does_not_lose_the_retry(poller_db, retries, poller_audit):
    """The removal is one more HTTP call against a rate-limited API, and needs the
    *Edit Issues* permission besides. Losing it must not undo the retry that
    already ran, and it must not be silent — the ticket still carries a label the
    DSE has acted on."""
    fake = FakeJiraClient(issues_by_project={"DSE": [_issue(labels=["dse-retry"])]})

    def _boom(key, label):
        raise RuntimeError("403 Forbidden: Edit Issues permission required")

    fake.remove_label = _boom

    assert _poller(fake).poll_once() >= 1  # the retry itself still counted
    assert [r["action"] for r in poller_audit] == ["jira_retry_label_removal_failed"]
    assert poller_audit[0]["details"] == {
        "ticket_key": "DSE-300",
        "label": "dse-retry",
        "after": "retried",
    }


def test_the_retry_label_is_off_unless_configured(poller_db, retries):
    """A board that never configured the label must not have the DSE reacting to
    a word it did not choose."""
    fake = FakeJiraClient(issues_by_project={"DSE": [_issue(labels=["dse-retry"])]})
    poller = JiraPoller(
        fake,
        tenant_id=TENANT,
        projects=["DSE"],
        trigger_label="dse",
        approved_status="Plan approved",
        rejected_status="Plan rejected",
        grace_seconds=0,
        reconcile_comments=False,
    )

    poller.poll_once()
    assert retries == []


def test_the_label_name_is_configurable_like_the_trigger_one(monkeypatch):
    assert config.get_retry_label() == "dse-retry"
    monkeypatch.setenv("JIRA_RETRY_LABEL", "needs-another-go")
    assert config.get_retry_label() == "needs-another-go"


def test_a_custom_label_is_the_one_acted_on(poller_db, retries):
    fake = FakeJiraClient(issues_by_project={"DSE": [_issue(labels=["needs-another-go"])]})
    poller = JiraPoller(
        fake,
        tenant_id=TENANT,
        projects=["DSE"],
        trigger_label="dse",
        retry_label="needs-another-go",
        approved_status="Plan approved",
        rejected_status="Plan rejected",
        grace_seconds=0,
        reconcile_comments=False,
    )

    poller.poll_once()
    assert len(retries) == 1
    assert fake.label_removals == [{"key": "DSE-300", "label": "needs-another-go"}]


# --- end to end, the way it actually breaks ---------------------------------


def test_two_hundred_sweeps_with_label_removal_always_failing(db, poller_db, poller_audit):
    """The BLOCKER, measured. Poller + real ingest + a Jira that refuses every
    label removal (no *Edit Issues* permission — the very grant this feature newly
    needs), and each admitted attempt failing in turn, which is what fed the old
    unbounded chain.

    Two hundred sweeps is over three hours of the production loop. Everything that
    touches the world has to be a small constant, not a function of the sweep
    count."""
    store = db(work_item=("wi_failed", "failed", "acme/api", "main"))
    issue = _issue(labels=["dse-retry"])
    fake = FakeJiraClient(issues_by_project={"DSE": [issue]})
    removal_attempts: list[str] = []

    def _no_permission(key, label):
        removal_attempts.append(key)
        raise RuntimeError("403 Forbidden: Edit Issues permission required")

    fake.remove_label = _no_permission
    poller = _poller(fake)

    for _ in range(200):
        poller.poll_once()
        if store.work_items:
            # The attempt that was admitted has failed too, and is now the ticket's
            # newest work item. Keying the retry on it is what made this unbounded.
            store.work_item = (store.work_items[-1][0], "failed", "acme/api", "main")

    assert len(store.work_items) == 1, "a second attempt was admitted with nobody asking"
    assert len(store.ingest_events) == 1
    assert [r["action"] for r in store.emitted] == ["jira_retry_admitted", "jira_retry_declined"]
    # One per decision, and there are exactly two decisions here.
    assert len(removal_attempts) == 2
    assert [r["action"] for r in poller_audit] == ["jira_retry_label_removal_failed"] * 2
    assert [r["details"]["after"] for r in poller_audit] == ["retried", "already_retried"]


def test_two_hundred_sweeps_on_a_ticket_that_never_qualifies(db, poller_db, poller_audit):
    """Same loop, the other branch: a label on a `done` ticket. One answer, one
    removal attempt, and then complete silence — no rows, no HTTP."""
    store = db(work_item=("wi_done", "done", "acme/api", "main"))
    fake = FakeJiraClient(issues_by_project={"DSE": [_issue(labels=["dse-retry"])]})
    removal_attempts: list[str] = []

    def _no_permission(key, label):
        removal_attempts.append(key)
        raise RuntimeError("403 Forbidden: Edit Issues permission required")

    fake.remove_label = _no_permission
    poller = _poller(fake)

    for _ in range(200):
        poller.poll_once()

    assert store.work_items == []
    assert [r["action"] for r in store.emitted] == ["jira_retry_declined"]
    assert removal_attempts == ["DSE-300"]
    assert len(poller_audit) == 1
