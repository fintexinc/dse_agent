"""A routed repository keeps the branch its binding declares.

The resolution cascade answers `(repo, base_branch)` when an origin binds ONE
repository. When it binds several — a Jira board with a frontend and a backend,
which is the case this feature exists for — it answers with candidates instead,
and the router picks among them. That is where the branch was being dropped:

    workflows.py:2101   input.base_branch = input.base_branch or "main"
    local_activities    fan_out(..., "base_branch": input.base_branch)

Neither reads the binding of the repository that was actually chosen, so every
routed item started from the literal `main`.

Measured on 2026-09-08, BFA-1132: the board binds `bmo-fee-calculator-fe`
(`develop`) and `bmo-fee-calculator-be` (`main`); the router picked the frontend
and the work item was written with `base_branch = main` — a branch 1407 commits
behind `develop`. The bootstrap pull request it opened targeted that dead branch.

`main` stays the fallback for a repository with no binding (a repo known only
through `repo_profiles`); what changes is that a declared branch is no longer
thrown away.
"""
from __future__ import annotations

import asyncio
import json as _json
import uuid as _uuid

import httpx
import psycopg2
import pytest

#: The activity under test connects as `dse_app`; the fixture writes and
#: cleans up as the owner, because `dse_app` has no DELETE on `work_items`
#: (the ledger grants, migrations 0028/0030).
ADMIN_DSN = "postgresql://dse:dse_dev_only@localhost:5432/dse"
TENANT = "test_tenant_branch_routing"
FE = "acme/fee-fe"
BE = "acme/fee-be"


@pytest.fixture
def db():
    conn = psycopg2.connect(ADMIN_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM repo_bindings WHERE tenant_id = %s", (TENANT,))
            cur.execute(
                "INSERT INTO repo_bindings (tenant_id, platform, binding_type, binding_value, "
                "repo, base_branch) VALUES (%s,'jira','project','BFA',%s,'dse-agent'), "
                "(%s,'jira','project','BFA',%s,'release')",
                (TENANT, FE, TENANT, BE),
            )
        conn.commit()
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM work_items WHERE tenant_id = %s", (TENANT,))
            cur.execute("DELETE FROM repo_bindings WHERE tenant_id = %s", (TENANT,))
        conn.commit()
        conn.close()


def test_the_router_reports_the_branch_each_binding_declares(db, monkeypatch):
    """The workflow cannot read the database; the routing activity can, and it
    is the one place that already knows which repositories were candidates."""
    from dse_orchestrator import local_activities

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"content": _json.dumps({"repos": [FE], "reason": "frontend"})}}
                ]
            }

    # `httpx` is imported inside the function under test, so the patch lands
    # on the module itself (same shape as test_router_retries._route).
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    monkeypatch.setenv("DSE_MODEL_GATEWAY_URL", "http://gateway.invalid")
    monkeypatch.setenv("DSE_LITELLM_MASTER_KEY", "sk-test")

    out = local_activities._route_repos_sync(TENANT, "fix the fee table on the UI", None)

    assert out["repos"] == [FE]
    assert out.get("base_branches", {}).get(FE) == "dse-agent", (
        "the routed repository's declared branch was dropped; the item would "
        f"start from 'main': {out}"
    )


def test_a_sibling_starts_from_its_own_repositorys_branch(db):
    """Fan-out is where this bites twice: every sibling used to inherit the
    PRIMARY's branch, so a backend bound to `release` would be worked on the
    frontend's branch name."""
    from dse_orchestrator.local_activities import fan_out_sibling_work_items

    primary = f"wi_br_{_uuid.uuid4().hex[:12]}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, repo, base_branch, "
            "requester, idempotency_key, status) "
            "VALUES (%s,%s,'jira',%s,%s,'dse-agent','usr_test',%s,'new')",
            (primary, TENANT, _json.dumps({"ticket_key": "BFA-1"}), FE, f"idem_{primary}"),
        )
    db.commit()

    result = asyncio.run(
        fan_out_sibling_work_items(
            {
                "work_item_id": primary,
                "tenant_id": TENANT,
                "repos": [BE],
                "base_branch": "dse-agent",  # the PRIMARY's branch
            }
        )
    )
    sib = result["created"][0]
    with db.cursor() as cur:
        cur.execute("SELECT base_branch FROM work_items WHERE id = %s", (sib,))
        got = cur.fetchone()[0]
    assert got == "release", (
        f"the sibling inherited the primary's branch instead of its own binding: {got!r}"
    )


def test_a_repository_with_no_binding_still_falls_back_to_main(db):
    """The fallback is the point of `or 'main'` and must survive: a repository
    known only through `repo_profiles` has no binding to carry a branch."""
    from dse_orchestrator.local_activities import fan_out_sibling_work_items

    primary = f"wi_br_{_uuid.uuid4().hex[:12]}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, repo, base_branch, "
            "requester, idempotency_key, status) "
            "VALUES (%s,%s,'jira',%s,%s,'dse-agent','usr_test',%s,'new')",
            (primary, TENANT, _json.dumps({"ticket_key": "BFA-2"}), FE, f"idem_{primary}"),
        )
    db.commit()

    result = asyncio.run(
        fan_out_sibling_work_items(
            {
                "work_item_id": primary,
                "tenant_id": TENANT,
                "repos": ["acme/unbound"],
                "base_branch": "main",
            }
        )
    )
    sib = result["created"][0]
    with db.cursor() as cur:
        cur.execute("SELECT base_branch FROM work_items WHERE id = %s", (sib,))
        assert cur.fetchone()[0] == "main"
