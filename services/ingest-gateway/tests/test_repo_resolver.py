"""Repo resolution cascade (Report 07 C2 / Phase B).

Proves the precedence order and — crucially — that ambiguity/absence returns
(None, None) so the workflow ASKS (never guesses). Runs against the real
Postgres (the standard for these suites); uses an isolated synthetic tenant.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from ingest_gateway.repo_resolver import parse_explicit_repo, resolve_repo

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
SUPER_DSN = DSN.replace("dse_app:dse_app_dev_only", "dse:dse_dev_only")


@pytest.fixture()
def tenant():
    tid = f"crm-repo-{uuid.uuid4().hex[:8]}"
    yield tid
    conn = psycopg2.connect(SUPER_DSN)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM repo_bindings WHERE tenant_id = %s", (tid,))
        cur.execute("DELETE FROM repo_profiles WHERE tenant_id = %s", (tid,))
    conn.commit()
    conn.close()


def _bind(tenant, platform, btype, value, repo, branch="main"):
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repo_bindings (tenant_id, platform, binding_type, binding_value, repo, base_branch) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (tenant, platform, btype, value, repo, branch),
        )
    conn.commit()
    conn.close()


def _profile(tenant, repo, role="", language=""):
    """A repository the tenant HAS but nobody bound to a channel or a project."""
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repo_profiles (tenant_id, repo, role, language) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (tenant, repo, role, language),
        )
    conn.commit()
    conn.close()


def test_parse_explicit_repo():
    assert parse_explicit_repo("do X in repo=org/x branch=dev") == ("org/x", "dev")
    assert parse_explicit_repo("nothing here") == (None, None)


def test_rung1_explicit_override_beats_binding(tenant):
    _bind(tenant, "slack", "channel", "C1", "org/from-binding")
    conn = psycopg2.connect(DSN)
    try:
        repo, branch, _scope = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "use repo=org/explicit", "channel": "C1"},
        )
    finally:
        conn.close()
    assert repo == "org/explicit"  # explicit beats the channel binding


def test_rung2_channel_binding(tenant):
    _bind(tenant, "slack", "channel", "C_PAY", "org/payments", "develop")
    conn = psycopg2.connect(DSN)
    try:
        repo, branch, _scope = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "fix the balance", "channel": "C_PAY"},
        )
    finally:
        conn.close()
    assert (repo, branch) == ("org/payments", "develop")


def test_component_beats_project_jira(tenant):
    _bind(tenant, "jira", "project", "FINX", "org/mono")
    _bind(tenant, "jira", "component", "Payments", "org/payments")
    conn = psycopg2.connect(DSN)
    try:
        repo, _, _scope = resolve_repo(
            conn, tenant_id=tenant, platform="jira",
            signals={"text": "bug", "component": "Payments", "project": "FINX"},
        )
    finally:
        conn.close()
    assert repo == "org/payments"  # component (finer) beats project


def test_rung4_single_repo_default(tenant):
    # tenant with ONE distinct repo across any binding -> default with no signal
    _bind(tenant, "slack", "channel", "C_A", "org/only")
    conn = psycopg2.connect(DSN)
    try:
        repo, _, _scope = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "something", "channel": "C_UNKNOWN"},
        )
    finally:
        conn.close()
    assert repo == "org/only"


def test_ambiguous_returns_none_for_clarification(tenant):
    # two distinct repos, no signal matches -> does NOT guess
    _bind(tenant, "slack", "channel", "C_A", "org/a")
    _bind(tenant, "slack", "channel", "C_B", "org/b")
    conn = psycopg2.connect(DSN)
    try:
        repo, branch, _scope = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "something", "channel": "C_X"},
        )
    finally:
        conn.close()
    assert (repo, branch) == (None, None)


def test_empty_tenant_returns_none(tenant):
    conn = psycopg2.connect(DSN)
    try:
        repo, branch, _scope = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "no binding at all", "channel": "C_X"},
        )
    finally:
        conn.close()
    assert (repo, branch) == (None, None)


def test_rung4_does_not_call_a_two_repo_tenant_single_because_only_one_is_bound(tenant):
    """The defect this cascade is supposed to make impossible: guessing.

    `repo_bindings` has one row per BINDING, not per repository, so a tenant
    with two repositories and a single Jira binding looked like a tenant with
    one. Rung 4 returned that one repository for EVERY Slack request, and Rung
    5 — the rung whose whole job is to say "ambiguous, ask" — was never reached.
    In production that sent "show a coloured badge on the reports dashboard" to
    the BACKEND repo, and because the router was skipped there was not even a
    `repo_routing_decided` row to explain it. The item simply started
    implementing against the wrong repository.

    The frontend here is bound to nothing, exactly as it was in production.
    """
    _bind(tenant, "jira", "project", "BD", "org/backend")
    _profile(tenant, "org/frontend", role="frontend", language="typescript")

    conn = psycopg2.connect(DSN)
    try:
        repo, branch, _scope = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "show a coloured badge on the reports dashboard",
                     "channel": "C_UNBOUND"},
        )
    finally:
        conn.close()

    assert repo is None, (
        f"resolved deterministically to {repo!r} for a tenant that has TWO "
        "repositories — the router never gets a chance"
    )
    assert branch is None


def test_rung4_still_defaults_when_the_tenant_genuinely_has_one_repo(tenant):
    """The other half: widening the question must not stop Rung 4 working. A
    repo known only through `repo_profiles` counts, and carries no binding, so
    the branch falls back to Rung 1's 'main' convention."""
    _profile(tenant, "org/only", role="backend")

    conn = psycopg2.connect(DSN)
    try:
        repo, branch, _scope = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "anything", "channel": "C_UNKNOWN"},
        )
    finally:
        conn.close()

    assert repo == "org/only"
    assert branch == "main"


def test_rung4_counts_a_repo_present_in_both_tables_once(tenant):
    """The union must be a UNION. Counting `repo_bindings` and `repo_profiles`
    separately would make a single well-configured repository look like two and
    send every request to the human picker."""
    _bind(tenant, "slack", "channel", "C_A", "org/same")
    _profile(tenant, "org/same", role="backend")

    conn = psycopg2.connect(DSN)
    try:
        repo, _, _scope = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "anything", "channel": "C_UNKNOWN"},
        )
    finally:
        conn.close()

    assert repo == "org/same"
