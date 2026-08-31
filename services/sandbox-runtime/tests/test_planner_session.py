"""WSC-E3-T3: read-only Planner session.

Proves (against real sandbox/git/Postgres, the real Temporal SDK harness):
  - the `run_planner_turn` Activity is a genuine Temporal Activity named after
    the contract and emits a structured `PlanArtifact`;
  - the context is hydrated with AGENTS.md + CODEOWNERS (read at the base ref
    through the GitHub App — NOT from the workspace, which does not exist yet
    when the Planner runs and never exists on the worker under k8s) + the
    tenant's approved skills (registry) + tickets + retrieval/index;
  - `risk_class` is DERIVED deterministically from the blast radius (P1), not
    from the proposer's word;
  - CONFORMANCE: any WRITE tool in the Planner FAILS (`ToolPermissionError`) —
    the session is read-only by construction.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from dse_contracts import ACTIVITY_RUN_PLANNER_TURN, PlanArtifact
from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    RunPlannerTurnInput,
    TeardownSandboxInput,
    _paths_for,
    _run_planner_turn_impl,
    provision_sandbox,
    run_planner_turn,
    teardown_sandbox,
)
from sandbox_runtime.retrieval import RetrievalService
from sandbox_runtime.toolsets import ToolPermissionError


def _cleanup_retrieval(dsn, tenant):
    import psycopg2

    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM retrieval_documents WHERE tenant_id=%s", (tenant,))
    conn.close()


_AGENTS_MD = "# AGENTS\nUse camelCase. Run the tests before the PR.\n"
_CODEOWNERS = "src/payments/ @payments-team\n"


class _DocsClient:
    """The base ref's view of the repo. Writing these files into the workspace —
    which is what this file used to do — could never prove anything: the Planner
    runs before `provision_sandbox` clones, and under `sandboxDriver: k8s` the
    host workspace is never created at all."""

    def __init__(self, files):
        self.files = files

    def get_file_text(self, repo, path, ref):
        return self.files.get(path)


def _seed_repo_docs(monkeypatch, files=None):
    """Serve AGENTS.md/CODEOWNERS the way production does — at the base ref,
    through the GitHub App."""
    import dse_validation.config as cfg
    import sandbox_runtime.activities as acts

    files = {"AGENTS.md": _AGENTS_MD, "CODEOWNERS": _CODEOWNERS} if files is None else files
    monkeypatch.setattr(acts, "_planner_github_client", lambda: _DocsClient(files), raising=True)
    monkeypatch.setattr(
        cfg, "GitHubConfig", lambda: type("_C", (), {"is_configured": True})(), raising=True
    )


def _seed_workspace(work_item_id, tenant, dsn, repo="app"):
    """Provisions the sandbox and seeds the retrieval index. The repo docs are
    NOT seeded here — see `_seed_repo_docs`."""
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
    workspace_dir, _ = _paths_for(work_item_id)
    svc = RetrievalService(dsn=dsn)
    svc.index_repo(
        tenant,
        repo,
        {
            "src/auth.py": "def login(user, password):\n    return verify(user, password)\n",
            "src/payments.py": "def charge(card, amount):\n    return gateway.charge(card, amount)\n",
        },
    )
    return workspace_dir, svc


def test_activity_name_matches_contract():
    assert run_planner_turn.__temporal_activity_definition.name == ACTIVITY_RUN_PLANNER_TURN


def test_planner_emits_structured_plan_with_hydrated_context(
    work_item_id, state_dir, pg_dsn, monkeypatch
):
    tenant = f"plan-{uuid.uuid4().hex[:8]}"
    # seed an approved skill for this tenant, to prove hydration
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_registry (tenant_id, skill_key, title, body, category, created_by, status) "
            "VALUES (%s,'no-plaintext-secrets','No cleartext secrets','Never hardcode secrets.','security',"
            "'principal:human:t','approved')",
            (tenant,),
        )
    conn.close()

    _seed_repo_docs(monkeypatch)
    workspace_dir, svc = _seed_workspace(work_item_id, tenant, pg_dsn)
    try:
        def proposer(ctx):
            # the 'LLM' proposer sees the hydrated context
            assert "AGENTS" in ctx.agents_md
            assert "@payments-team" in ctx.codeowners
            # Skills fora do serving (2026-08-31): o contexto chega sem elas.
            assert ctx.skills == []
            return {
                "steps": ["Add validation to the login"],
                "expected_files": ["src/auth.py"],
                "test_plan": "Test an invalid login.",
            }

        plan = asyncio.run(
            _run_planner_turn_impl(
                RunPlannerTurnInput(
                    work_item_id=work_item_id,
                    tenant_id=tenant,
                    instruction="improve the login validation",
                    repo="app",
                    related_tickets=["JIRA-1: harden login"],
                ),
                retrieval=svc,
                proposer=proposer,
            )
        )
        assert isinstance(plan, PlanArtifact)
        assert plan.work_item_id == work_item_id
        assert plan.steps == ["Add validation to the login"]
        assert plan.expected_files == ["src/auth.py"]
        # auth is sensitive in the fintech domain → the deterministic classifier
        # raises it to 'high' (glob **/*auth*), regardless of what the proposer said.
        assert plan.risk_class == "high"
    finally:
        _cleanup_retrieval(pg_dsn, tenant)
        conn = psycopg2.connect(pg_dsn)
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM skill_registry WHERE tenant_id=%s", (tenant,))
        conn.close()
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))


def test_planner_plumbs_estimated_lines_into_artifact_and_risk(work_item_id, state_dir, pg_dsn):
    """rc.89: a estimativa do proposer chega ao PlanArtifact E ao classificador —
    um plano estimado em 1200 linhas parqueia como high de verdade (o ramo >800
    era código morto com a constante 400). Fixture sem estimativa → None."""
    tenant = f"plan-{uuid.uuid4().hex[:8]}"
    workspace_dir, svc = _seed_workspace(work_item_id, tenant, pg_dsn)
    try:
        plan = asyncio.run(
            _run_planner_turn_impl(
                RunPlannerTurnInput(
                    work_item_id=work_item_id, tenant_id=tenant,
                    instruction="rewrite the module", repo="app",
                ),
                retrieval=svc,
                proposer=lambda ctx: {
                    "steps": ["rewrite"], "expected_files": ["src/big_module.py"],
                    "estimated_lines": 1200,
                },
            )
        )
        assert plan.estimated_lines == 1200
        assert plan.risk_class == "high", (
            f"1200 linhas estimadas têm de parquear como high — veio {plan.risk_class!r}"
        )
    finally:
        _cleanup_retrieval(pg_dsn, tenant)
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))


def test_planner_risk_class_is_deterministic_floor_not_llm(work_item_id, state_dir, pg_dsn):
    """Even if the proposer reports expected_files touching a migration, the
    risk_class is derived in code (high), not from what the 'LLM' claims."""
    tenant = f"plan-{uuid.uuid4().hex[:8]}"
    workspace_dir, svc = _seed_workspace(work_item_id, tenant, pg_dsn)
    try:
        plan = asyncio.run(
            _run_planner_turn_impl(
                RunPlannerTurnInput(
                    work_item_id=work_item_id, tenant_id=tenant, instruction="touch the schema", repo="app"
                ),
                retrieval=svc,
                proposer=lambda ctx: {"steps": ["change the schema"], "expected_files": ["migrations/0099_x.sql"]},
            )
        )
        assert plan.risk_class == "high"
    finally:
        _cleanup_retrieval(pg_dsn, tenant)
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))


def test_planner_write_tool_fails_conformance(work_item_id, state_dir, pg_dsn):
    """CONFORMANCE (WSC-E3-T3): a write step in the Planner's exploration script
    raises ToolPermissionError — the session is read-only."""
    tenant = f"plan-{uuid.uuid4().hex[:8]}"
    workspace_dir, svc = _seed_workspace(work_item_id, tenant, pg_dsn)
    try:
        with pytest.raises(ToolPermissionError):
            asyncio.run(
                _run_planner_turn_impl(
                    RunPlannerTurnInput(
                        work_item_id=work_item_id, tenant_id=tenant, instruction="tries to write", repo="app"
                    ),
                    retrieval=svc,
                    exploration_script=[
                        {"tool": "read_file", "path": "src/auth.py"},  # ok
                        {"tool": "write_file", "path": "src/evil.py", "content": "x=1"},  # must fail
                    ],
                )
            )
        # the file was NOT created (the write never reached the dispatch)
        assert not (Path(workspace_dir) / "src/evil.py").exists()
    finally:
        _cleanup_retrieval(pg_dsn, tenant)
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))


def test_planner_runs_through_real_temporal_activity_environment(work_item_id, state_dir, pg_dsn):
    tenant = f"plan-{uuid.uuid4().hex[:8]}"
    _seed_workspace(work_item_id, tenant, pg_dsn)
    env = ActivityEnvironment()
    try:
        plan = asyncio.run(
            env.run(
                run_planner_turn,
                RunPlannerTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="something simple", repo="app"),
            )
        )
        assert isinstance(plan, PlanArtifact)
        assert plan.risk_class in {"low", "medium", "high"}
    finally:
        _cleanup_retrieval(pg_dsn, tenant)
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
