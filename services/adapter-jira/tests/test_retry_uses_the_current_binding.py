"""A retry starts from the branch the binding declares TODAY.

`ingest_retry_trigger` inherits the previous attempt's repository — right, since
the retry is the same piece of work — and used to inherit its `base_branch` with
it. That is the one field that can have gone stale: the reason an item is being
retried is often that the operator changed something, and pointing the board at
a different branch is exactly that kind of change.

Measured on BFA-1132 (2026-09-08): the item was written with `base_branch =
main` (the router's literal default, since fixed), the board was then bound to
`dse-agent` — the branch carrying `.dse/validation.json` — and a retry would
still have gone to `main`, spending the one retry a ticket ever gets
(`ingest_events.event_id` is UNIQUE on `retry:<issue id>`) on the wrong branch.

The repository stays inherited. Only the branch is re-read, and only when the
binding actually declares one.
"""
from __future__ import annotations

from adapter_jira import ingest

from .helpers import FakeConn

TENANT = "test_tenant_jira_adapter"
REPO = "fintexinc/bmo-fee-calculator-fe"


def _responder(*, binding_branch: str | None):
    """The rows the real queries would return for a retryable ticket."""

    def respond(sql: str, params):
        if "FROM repo_bindings" in sql and "base_branch" in sql:
            return [(binding_branch,)] if binding_branch else []
        return []

    return respond


def test_the_retry_re_reads_the_branch_from_the_binding():
    conn = FakeConn(_responder(binding_branch="dse-agent"))
    got = ingest.branch_for_retry(
        conn, tenant_id=TENANT, repo=REPO, prior_base_branch="main"
    )
    assert got == "dse-agent", (
        f"the retry would run against the previous attempt's branch: {got!r}"
    )


def test_a_repository_with_no_binding_keeps_the_previous_branch():
    """No binding means nothing newer to learn — inheriting is then correct."""
    conn = FakeConn(_responder(binding_branch=None))
    assert (
        ingest.branch_for_retry(conn, tenant_id=TENANT, repo=REPO, prior_base_branch="main")
        == "main"
    )


def test_a_database_error_keeps_the_previous_branch():
    """A retry must not be lost because a lookup failed."""

    def _boom(sql: str, params):
        raise RuntimeError("connection reset")

    conn = FakeConn(_boom)
    assert (
        ingest.branch_for_retry(conn, tenant_id=TENANT, repo=REPO, prior_base_branch="release")
        == "release"
    )
