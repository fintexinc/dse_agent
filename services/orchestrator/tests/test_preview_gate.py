"""Plan 08 §D — deploys_preview gate (preview_enabled_for_repo).

Tests the gate's deterministic, fail-safe semantics directly against Postgres
(no Temporal): binding marked → enabled; no binding on the tenant → enabled
(backward compat); bindings exist but repo not marked → disabled.
"""
from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest

from conftest import DSN
from dse_orchestrator.local_activities import preview_enabled_for_repo


def _run(payload: dict) -> dict:
    return asyncio.run(preview_enabled_for_repo(payload))


@pytest.fixture
def gate_tenant():
    """Per-test isolated tenant — clears the bindings before and after."""
    tid = f"gate-{uuid.uuid4().hex[:8]}"
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM repo_bindings WHERE tenant_id = %s", (tid,))
        conn.commit()
        yield tid
        with conn.cursor() as cur:
            cur.execute("DELETE FROM repo_bindings WHERE tenant_id = %s", (tid,))
        conn.commit()
    finally:
        conn.close()


def _insert_binding(tenant_id: str, repo: str, deploys_preview: bool):
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO repo_bindings (tenant_id, platform, binding_type, binding_value, repo, base_branch, deploys_preview) "
                "VALUES (%s,'slack','channel',%s,%s,'main',%s) "
                "ON CONFLICT (tenant_id, platform, binding_type, binding_value, repo) DO UPDATE SET deploys_preview = EXCLUDED.deploys_preview",
                (tenant_id, f"C-{repo}", repo, deploys_preview),
            )
        conn.commit()
    finally:
        conn.close()


def test_no_bindings_is_enabled_backward_compat(gate_tenant):
    r = _run({"tenant_id": gate_tenant, "repo": "acme/anything"})
    assert r["enabled"] is True
    assert r["reason"] == "no_bindings_backward_compat"


def test_binding_marked_is_enabled(gate_tenant):
    _insert_binding(gate_tenant, "acme/wallet", deploys_preview=True)
    r = _run({"tenant_id": gate_tenant, "repo": "acme/wallet"})
    assert r["enabled"] is True
    assert r["reason"] == "binding_marked"


def test_bindings_exist_but_repo_not_marked_is_disabled(gate_tenant):
    # the tenant has a binding (for another repo), but the target repo is not previewable
    _insert_binding(gate_tenant, "acme/other", deploys_preview=False)
    r = _run({"tenant_id": gate_tenant, "repo": "acme/not-marked"})
    assert r["enabled"] is False
    assert r["reason"] == "bindings_exist_repo_not_marked"
