"""WSC-E3-T5: fresh-context Reviewer session (builds the session; WS-E
orchestrates the loop).

Proves (P3 — no producer approves its own work, and the Reviewer never sees the
Coder's history):
  - the `run_l2_review` Activity is a Temporal Activity named after the contract
    and returns `L2Verdict`;
  - BY CONSTRUCTION the Reviewer's context holds ONLY plan + diff — there is no
    field/parameter that could carry the Coder's transcript/thoughts/tool-calls;
  - the verdict reflects adherence to the plan (specific objections when it does
    not).
"""
from __future__ import annotations

import asyncio

import pytest
from temporalio.testing import ActivityEnvironment

from dse_contracts import ACTIVITY_RUN_L2_REVIEW, L2Verdict, PlanArtifact
from sandbox_runtime.activities import (
    RunL2ReviewInput,
    _run_l2_review_impl,
    run_l2_review,
)
from sandbox_runtime.sessions import FreshReviewerSession, ReviewerContext

_PLAN = PlanArtifact(
    work_item_id="wi-rev",
    steps=["implement handler"],
    expected_files=["src/handler.py"],
    diff_budget_lines=100,
    test_plan="testar handler",
    risk_class="low",
)

_CLEAN_DIFF = (
    "diff --git a/src/handler.py b/src/handler.py\n"
    "--- a/src/handler.py\n+++ b/src/handler.py\n"
    "@@ -0,0 +1,2 @@\n+def handler():\n+    return 'ok'\n"
)
_OUT_OF_SCOPE_DIFF = (
    "diff --git a/src/secret_stealer.py b/src/secret_stealer.py\n"
    "--- /dev/null\n+++ b/src/secret_stealer.py\n"
    "@@ -0,0 +1,1 @@\n+import os\n"
)


def test_activity_name_matches_contract():
    assert run_l2_review.__temporal_activity_definition.name == ACTIVITY_RUN_L2_REVIEW


def test_reviewer_context_by_construction_has_only_plan_and_diff():
    """The structural proof of P3: the fields of the Reviewer context and of the
    Activity input are EXACTLY {plan, diff} (+ ids/classes) — no channel for the
    Coder's history."""
    ctx_fields = set(ReviewerContext.__dataclass_fields__.keys())
    assert ctx_fields == {"work_item_id", "plan", "diff"}
    for banned in ("coder_history", "transcript", "turns", "thoughts", "tool_calls", "coder_log", "session_history"):
        assert banned not in ctx_fields

    # Remediation (spec §4): the L2 evidence is pinned to the SHA — the input
    # gained base_sha/head_sha (immutable metadata, NOT the Coder's history). The
    # P3 guard is the {plan, diff} content allowlist + the BANNED field list
    # below; commit SHAs open no channel for transcript/thoughts/tool-calls.
    input_fields = set(RunL2ReviewInput.model_fields.keys())
    assert input_fields == {
        "work_item_id", "tenant_id", "plan", "diff", "task_class", "data_class",
        "base_sha", "head_sha",
    }
    for banned in ("coder_history", "transcript", "turns", "coder_log",
                   "thoughts", "tool_calls", "session_history"):
        assert banned not in input_fields

    # The fresh session only exposes read_plan/read_diff — no repo/history.
    session = FreshReviewerSession(ReviewerContext(work_item_id="x", plan=_PLAN, diff=_CLEAN_DIFF))
    public = {m for m in dir(session) if not m.startswith("_")}
    assert "read_plan" in public and "read_diff" in public
    assert not ({"read_coder_history", "read_transcript", "repo_map", "search_code"} & public)


def test_render_shows_estimate_as_info_never_budget():
    """rc.89: o contexto do L2 carregava `diff_budget_lines: 400` sob o título
    "Plan the diff must adhere to" — um teto FALSO (constante nunca dimensionada,
    gate L1 já desativado) apresentado como obrigação. Agora: a estimativa do
    Planner entra como INFORMAÇÃO ("not a limit") quando existe, e NADA entra
    quando não existe — o L2 não lê número nenhum sobre tamanho."""
    com_estimativa = PlanArtifact(
        work_item_id="wi-est", steps=["s"], expected_files=["src/x.py"],
        estimated_lines=380, test_plan="t", risk_class="low",
    )
    render = ReviewerContext(work_item_id="wi-est", plan=com_estimativa,
                             diff=_CLEAN_DIFF).render()
    assert "planner_estimated_lines" in render and "380" in render
    assert "not a limit" in render, "a estimativa tem de se declarar informativa"
    assert "diff_budget_lines" not in render, (
        "o teto morto de 400 voltou ao contexto do L2 como obrigação"
    )

    sem_estimativa = PlanArtifact(
        work_item_id="wi-sem", steps=["s"], expected_files=["src/x.py"],
        test_plan="t", risk_class="low",
    )
    render2 = ReviewerContext(work_item_id="wi-sem", plan=sem_estimativa,
                              diff=_CLEAN_DIFF).render()
    assert "diff_budget_lines" not in render2
    assert "planner_estimated_lines" not in render2, (
        "sem estimativa, nenhuma linha sobre tamanho — não se inventa número"
    )


def test_reviewer_passes_when_diff_adheres_to_plan():
    verdict = asyncio.run(
        _run_l2_review_impl(RunL2ReviewInput(work_item_id="wi-rev", tenant_id="t", plan=_PLAN, diff=_CLEAN_DIFF))
    )
    assert isinstance(verdict, L2Verdict)
    assert verdict.passed is True
    assert verdict.objections == []


def test_reviewer_objects_when_diff_leaves_blast_radius():
    verdict = asyncio.run(
        _run_l2_review_impl(
            RunL2ReviewInput(work_item_id="wi-rev", tenant_id="t", plan=_PLAN, diff=_OUT_OF_SCOPE_DIFF)
        )
    )
    assert verdict.passed is False
    assert any("blast radius" in o for o in verdict.objections)
    assert any("secret_stealer.py" in o for o in verdict.objections)


def test_reviewer_accepts_custom_model_verdict_with_file_line_objections():
    """The (fresh) review substrate may return file/line-specific objections —
    the WS-E loop consumes those."""

    def model_verdict(ctx: ReviewerContext):
        return (False, ["src/handler.py:2 — hardcoded 'ok' return violates the AGENTS.md error convention"], 0.02)

    verdict = asyncio.run(
        _run_l2_review_impl(
            RunL2ReviewInput(work_item_id="wi-rev", tenant_id="t", plan=_PLAN, diff=_CLEAN_DIFF),
            verdict_fn=model_verdict,
        )
    )
    assert verdict.passed is False
    assert verdict.objections[0].startswith("src/handler.py:2")
    assert verdict.cost_usd == pytest.approx(0.02)


def test_reviewer_runs_through_real_temporal_activity_environment():
    env = ActivityEnvironment()
    verdict = asyncio.run(
        env.run(run_l2_review, RunL2ReviewInput(work_item_id="wi-rev", tenant_id="t", plan=_PLAN, diff=_CLEAN_DIFF))
    )
    assert isinstance(verdict, L2Verdict)
    assert verdict.passed is True
