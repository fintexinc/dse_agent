"""The panel decides which Jira projects are swept — not a Secret and a restart.

Binding a board in the console writes one row in `repo_bindings`; that is the
gesture an operator makes and the only one they can make from a browser. But the
poller took its project list from `JIRA_POLL_PROJECTS` and read it ONCE, at
process start (`poller_main` passed it to the constructor). So half of "connect
this board" happened — the binding decided which repository a card would land
in — while the other half, whether anyone reads that project at all, needed an
edit to the out-of-repo Secret `dse-poc-secrets` plus a rollout restart.

Measured on 2026-09-08: `repo_bindings` carried BFA -> the two BMO repositories
since 2026-09-04, the resolution cascade answered for BFA correctly, and
`config.get_poll_projects()` in the live Pod still returned `['BD']`. A card
labelled `dse` on that board was never read by anything. Nothing failed; the
work simply never arrived — the same shape as the timezone outage that
`test_poller_time_bound.py` pins.

So the sweep set becomes the union of what the database says (bindings, re-read
every round) and what the environment says (the fallback, for a project with no
binding). A database that is unreachable degrades to the environment: the sweep
is a fallback path itself and must never be the thing that stops.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adapter_jira.poller import JiraPoller

from .helpers import FakeConn

NOW = datetime(2026, 9, 8, 22, 0, tzinfo=timezone.utc)


class RecordingClient:
    """Records which projects were asked for, and returns no issues."""

    def __init__(self):
        self.searched: list[str] = []

    def search_updated(self, project: str, bound: str | None):
        self.searched.append(project)
        return []


def _responder(bound_projects: list[str]):
    """Rows a real database would return: the panel's project bindings."""

    def respond(sql: str, params):
        if "FROM repo_bindings" in sql:
            return [(p,) for p in bound_projects]
        return []

    return respond


def _poller(configured: list[str]) -> JiraPoller:
    return JiraPoller(
        RecordingClient(),
        tenant_id="test_tenant_jira_adapter",
        projects=configured,
        trigger_label="dse",
        approved_status="Plan approved",
        rejected_status="Plan rejected",
    )


@pytest.fixture
def db(monkeypatch):
    """One FakeConn whose rows the test can change between sweeps, exactly as a
    panel write would change them under a running poller."""
    conn = FakeConn(_responder([]))
    monkeypatch.setattr("adapter_jira.poller.get_connection", lambda: conn)
    return conn


def test_a_project_bound_in_the_panel_is_swept(db):
    """The whole point: bind BFA in the console, and the poller reads BFA."""
    db.responder = _responder(["BFA"])
    poller = _poller(["BD"])
    poller.poll_once(now=NOW)
    assert "BFA" in poller._client.searched, (
        "a project bound in the panel was never searched: " f"{poller._client.searched}"
    )


def test_a_binding_added_while_the_poller_runs_needs_no_restart(db):
    """The operator's actual complaint: adding it on the site should be enough.

    The list is re-read every sweep, so a binding written between two rounds is
    picked up by the next one — no Secret edit, no rollout restart.
    """
    poller = _poller(["BD"])
    poller.poll_once(now=NOW)
    assert poller._client.searched == ["BD"]

    db.responder = _responder(["BFA"])  # the panel writes the binding
    poller.poll_once(now=NOW)
    assert "BFA" in poller._client.searched[1:], (
        "the poller only learns about a new project by being restarted: "
        f"{poller._client.searched}"
    )


def test_the_environment_still_carries_a_project_with_no_binding(db):
    """`JIRA_POLL_PROJECTS` stays authoritative for whatever has no binding —
    this change adds a source, it does not replace one."""
    db.responder = _responder(["BFA"])
    poller = _poller(["BD"])
    poller.poll_once(now=NOW)
    assert set(poller._client.searched) == {"BD", "BFA"}


def test_a_project_in_both_sources_is_swept_once(db):
    """Two sources, one sweep: a duplicated project would double every JQL
    call and re-reconcile the same issues for no reason."""
    db.responder = _responder(["BD", "BFA"])
    poller = _poller(["BD"])
    poller.poll_once(now=NOW)
    assert sorted(poller._client.searched) == ["BD", "BFA"]


def test_an_unreachable_database_falls_back_to_the_environment(monkeypatch):
    """Reading the bindings must not become a NEW way for the sweep to die.

    Scoped deliberately to the resolution step: a Postgres outage already stops
    this poller one line later, at the cursor read (`_get_cursor` opens its own
    connection and does not catch) — a pre-existing defect that killed the
    production process three times, and one this change neither causes nor
    fixes. What is pinned here is that the source of the project list degrades
    to the environment instead of raising.
    """

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr("adapter_jira.poller.get_connection", _boom)
    poller = _poller(["BD"])
    assert poller._projects_to_sweep() == ["BD"]
