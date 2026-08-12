"""FAKE Activities implementing the SAME signature/name/return type as the
cross-workstream Activities in `dse_contracts.activities` (owners: WS-C and
WS-E). They let us prove the workflow state machine (WSB-E2-T3) without waiting
for the other workstreams to finish their real implementations — exactly as the
task statement requires.

Postgres and Temporal themselves are NEVER mocked here — only the two Activity
boundaries that belong to other workstreams. The WS-B local Activities
(`update_work_item_status`, `check_clarification_completeness`,
`emit_audit_event`) used by the tests ARE the real ones, hitting the infra's
real Postgres (see `conftest.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError


def _maybe_fail_closed(state: "FakeControlPlane", name: str) -> None:
    """WSB-E5-T3b — simulates a fail-closed policy refusal on the model/egress
    path (egress-proxy down, expired virtual key, kill switch). Raises a
    NON-retryable ApplicationError with a marker the workflow's
    `_run_model_activity` recognizes — clean failure, no truncated output (P6).
    Marked as a boundary FAKE (the real WS-D/WS-C do this for real)."""
    spec = state.fail_closed_on.get(name)
    if not spec:
        return
    times = spec.get("times", 0)
    if times <= 0:
        return
    spec["times"] = times - 1
    marker = spec.get("marker", "egress_proxy_unreachable_fail_closed")
    # Phase 2 (plan 09): the default is the canonical TYPE from the contract's
    # vocabulary (the message no longer decides); "type" in the spec allows
    # simulating legacy raisers ("EgressFailClosed") for the fallback compat test.
    from dse_contracts.failure import FailureClass, failure_type

    err_type = spec.get("type", failure_type(FailureClass.policy_fail_closed))
    raise ApplicationError(marker, type=err_type, non_retryable=True)


def _maybe_transient_fail(state: "FakeControlPlane", name: str) -> None:
    """WSB-E5-T3b — simulates a TRANSIENT gateway oscillation (LiteLLM drops and
    comes back mid-task). RETRYABLE error: Temporal itself retries the Activity
    until it passes — durability absorbs the oscillation without losing progress
    or truncating output (P6/P8)."""
    spec = state.transient_fail_on.get(name)
    if not spec:
        return
    times = spec.get("times", 0)
    if times <= 0:
        return
    spec["times"] = times - 1
    raise ApplicationError("gateway_oscillation_transient", type="GatewayUnavailable",
                           non_retryable=False)

from dse_contracts.activities import (
    ACTIVITY_CHECKPOINT_SANDBOX,
    ACTIVITY_CONSUME_CI_STATUS,
    ACTIVITY_FINALIZE_PR,
    ACTIVITY_PROVISION_SANDBOX,
    ACTIVITY_REBUILD_SANDBOX,
    ACTIVITY_RUN_CODER_TURN,
    ACTIVITY_RUN_DEMO_EVIDENCE,
    ACTIVITY_RUN_L1_PIPELINE,
    ACTIVITY_RUN_L2_REVIEW,
    ACTIVITY_RUN_PLANNER_TURN,
    ACTIVITY_RUN_TESTER_TURN,
    ACTIVITY_RUN_VISUAL_DIFF,
    ACTIVITY_TEARDOWN_SANDBOX,
    ACTIVITY_TRIGGER_PREVIEW,
    ACTIVITY_UPDATE_BASE_BRANCH,
    ACTIVITY_VERIFY_MERGE_STATE,
    CheckpointRef,
    CheckpointSandboxInput,
    CiStatusResult,
    ConsumeCiStatusInput,
    CoderTurnResult,
    GateStatus,
    DemoEvidenceResult,
    FinalizePrInput,
    L1Finding,
    L1Result,
    L2Verdict,
    MergeVerification,
    PreviewRef,
    PrRef,
    ProvisionSandboxInput,
    RebuildSandboxInput,
    RunCoderTurnInput,
    RunDemoEvidenceInput,
    RunL1PipelineInput,
    RunVisualDiffInput,
    SandboxHandle,
    TeardownSandboxInput,
    TesterTurnResult,
    TriggerPreviewInput,
    UpdateBaseBranchInput,
    UpdateBaseBranchResult,
    VerifyMergeInput,
    VisualDiffResult,
)
from dse_contracts.plan_artifact import PlanArtifact
# All input models now come from the canonical contract. That way this suite
# detects wire drift without importing the owning services.
from dse_contracts.activities import (
    RunL2ReviewInput,
    RunPlannerTurnInput,
    RunTesterTurnInput,
)


@dataclass
class FakeControlPlane:
    """Mutable state injected into a specific test to drive the fakes' behavior
    (how many times L1 fails, the CI status sequence, whether checkpoint fails N
    times before working, etc.)."""

    l1_fail_times: int = 0
    #: detail of the failing `test` finding (spec-conflict tests put real
    #: "FAIL <path>" lines here, the shape quality_checks emits)
    l1_fail_detail: str = "simulated failure"
    #: Gates que voltam com `status=ERROR` (a ferramenta NÃO rodou) em vez de
    #: FAIL (rodou e reprovou). Existe porque a diferença entre os dois decide
    #: se o Coder é chamado — e ela não tinha como ser exercitada aqui.
    l1_error_checks: list[str] = field(default_factory=list)
    ci_sequence: list[str] = field(default_factory=lambda: ["green"])
    checkpoint_fail_times: int = 0
    provision_calls: int = 0
    coder_turn_calls: int = 0
    checkpoint_calls: int = 0
    rebuild_calls: int = 0
    teardown_calls: int = 0
    finalize_calls: int = 0
    # S7: maps work_item_id -> pr_number so finalize is idempotent per work item
    # (the call site no longer passes `existing_pr_number` — the PR is resolved
    # by work_item_id inside the real finalize).
    pr_by_wi: dict = field(default_factory=dict)
    l1_calls: int = 0
    pr_counter: int = 1000
    calls_log: list[str] = field(default_factory=list)
    # lets a test "hang" an activity until released (used by the chaos test to
    # simulate a long Activity in progress when the worker dies)
    coder_turn_hang_event: Any = None

    # --- Phase 2: Planner / Tester / Reviewer L2 (WS-C/WS-E boundaries) ---
    planner_calls: int = 0
    tester_calls: int = 0
    #: test files the fake Tester reports as ITS OWN (spec-conflict tests set
    #: this to tell tester-owned failures apart from pre-existing specs)
    tester_test_files: list[str] = field(default_factory=lambda: ["test_app.py"])
    #: what each Tester call received in `reauthor_specs` (beco 1, veredito
    #: `reauthor`): [] em turno normal, os caminhos ordenados no turno da ordem
    tester_reauthor_orders: list[list[str]] = field(default_factory=list)
    #: a instrução completa que cada turno de Coder recebeu (a porta 1 v3
    #: afirma que as asserções da spec em conflito chegam no fix_context)
    coder_instructions: list[str] = field(default_factory=list)
    #: o reauthor_context que cada turno de Tester recebeu (o comment do
    #: veredito humano tem que chegar ao prompt da ordem)
    tester_reauthor_contexts: list[str | None] = field(default_factory=list)
    #: suites que o gate `test` do fake reporta como já vermelhas no base_sha
    #: (baseline check, rc.43) — entram em L1Finding.inherited_failures
    l1_inherited_failures: list[str] = field(default_factory=list)
    #: findings POR CHAMADA do L1 (beco 1, gatilho por memória de spec): cada
    #: chamada consome a próxima lista; esgotadas → PASS. Tem precedência sobre
    #: l1_fail_times/l1_fail_detail — é o knob das sequências heterogêneas
    #: (test → lint+build → test) que o contador fixo não expressa.
    l1_findings_by_call: list[list[Any]] = field(default_factory=list)
    l2_calls: int = 0
    # risk declared by the Planner (default low -> the gate auto-approves)
    plan_risk_class: str = "low"
    plan_expected_files: list[str] = field(default_factory=lambda: ["app.py"])
    planner_cost_usd: float = 0.0
    tester_cost_usd: float = 0.0
    tester_tests_ran: bool = True
    tester_tests_passed: bool = True
    tester_returncode: int = 0
    #: `failure_output` do turno — o texto que o workflow classifica (carga/
    #: compilação morta = defeito do Tester; asserção falhando = do Coder).
    tester_failure_output: str = ""
    l2_cost_usd: float = 0.0
    coder_cost_usd: float = 0.01
    # L2: fails N times (objections) before approving
    l2_fail_times: int = 0
    l2_objections: list[str] = field(default_factory=lambda: ["app.py:12 has no test"])
    # captures the LAST payload the L2 fake received (proof of P3 isolation)
    last_l2_payload: dict | None = None
    # simulates a fail-closed refusal on the model path (WSB-E5-T3b): activity
    # name -> {"times": N, "marker": str}. NON-retryable error -> clean failure.
    fail_closed_on: dict = field(default_factory=dict)
    # simulates a TRANSIENT gateway oscillation (LiteLLM unstable mid-task):
    # activity name -> {"times": N}. RETRYABLE error -> Temporal retries until it
    # passes.
    transient_fail_on: dict = field(default_factory=dict)

    # --- Phase 3: evidence pipeline (WS-E boundary) ---
    # The fakes DECODE the payload with the REAL contract models
    # (TriggerPreviewInput/RunDemoEvidenceInput/RunVisualDiffInput) — no lenient
    # dicts (lesson from addendum 02: 14 boundary bugs in Phases 1-2).
    trigger_preview_calls: int = 0
    demo_evidence_calls: int = 0
    visual_diff_calls: int = 0
    # files_changed reported by the fake Coder (drives the preview paths-filter)
    coder_files_changed: list[str] = field(default_factory=lambda: ["app.py"])
    #: Per-turn override, one entry per Coder turn (the last entry repeats).
    #: Exists so a test can make a FIX turn move nothing while the first turn
    #: did — the shape that used to send the Tester and L1 to re-decide a
    #: byte-identical tree.
    coder_files_changed_by_turn: list[list[str]] | None = None
    #: Por turno: caminhos de teste que o post-turn REVERTEU (instrumento do
    #: Tester). O último item repete quando a sequência esgota, como o de cima.
    # "auto" = deterministic paths-filter (mirror of WS-E's FR-20);
    # "created"/"degraded" force the status; "raise" fails the whole Activity.
    preview_mode: str = "auto"
    # §F F1 — merge verification via the GitHub API (fake): "verified" (default,
    # PR actually merged), "not_merged" (forged -> refuted), "unavailable" (API
    # down -> degrades to the envelope).
    merge_verify_mode: str = "verified"
    demo_passed: bool = True
    demo_video_key: str | None = "evidence/demo.webm"
    demo_trace_key: str | None = "evidence/trace.zip"
    visual_diff_changed_pct: float = 0.0
    last_preview_payload: dict | None = None
    last_demo_payload: dict | None = None
    last_visual_diff_payload: dict | None = None

    # --- Phase 4: merge-base / base-drift (WS-E boundary, WSE-E6-T16) ---
    update_base_calls: int = 0
    # >0: a activity LEVANTA (retryable) nesse número de chamadas — 99 simula a
    # falha PERMANENTE medida no wi_a8b760de (workspace inexistente no pod):
    # sem cap ela retentava até o schedule_to_close e o workflow morria MUDO.
    update_base_fail_times: int = 0
    # controls the fake's return: is there drift? a conflict? orphaned threads?
    base_has_drift: bool = True
    base_conflict: bool = False
    base_orphaned_threads: int = 0  # the Phase 4 exit requires 0 on a real merge-base
    last_update_base_payload: dict | None = None
    last_l1_payload: dict | None = None
    last_finalize_payload: dict | None = None
    last_ci_payload: dict | None = None


def build_fake_activities(state: FakeControlPlane) -> list[Any]:
    async def run_planner_turn(payload: dict) -> PlanArtifact:
        state.planner_calls += 1
        state.calls_log.append("run_planner_turn")
        _maybe_fail_closed(state, ACTIVITY_RUN_PLANNER_TURN)
        RunPlannerTurnInput(**payload)  # REAL contract decode
        return PlanArtifact(
            work_item_id=payload["work_item_id"],
            steps=["step 1", "step 2"],
            expected_files=list(state.plan_expected_files),
            test_plan="covers the happy path",
            risk_class=state.plan_risk_class,
        )

    async def run_tester_turn(payload: dict) -> TesterTurnResult:
        state.tester_calls += 1
        reauthor_specs = list(payload.get("reauthor_specs") or [])
        state.tester_reauthor_orders.append(reauthor_specs)
        state.tester_reauthor_contexts.append(payload.get("reauthor_context"))
        state.calls_log.append("run_tester_turn")
        _maybe_fail_closed(state, ACTIVITY_RUN_TESTER_TURN)
        RunTesterTurnInput(**payload)  # REAL contract decode
        files_changed: list[str] = []
        if reauthor_specs:
            # Espelho do contrato da activity real (pod path): a ordem executa
            # a reescrita in-place e a EVIDÊNCIA vai ao ledger — o E2E da
            # cadeia parque → reauthor → reescrita auditada → L1 verde pina
            # exatamente este elo, que em produção falhou mudo (wi_6f00bf0a).
            import json as _json
            import os as _os

            import psycopg2 as _pg
            conn = _pg.connect(_os.environ.get(
                "DSE_DATABASE_URL",
                "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse",
            ))
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO audit_log (work_item_id, tenant_id, actor, action, details) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (payload["work_item_id"], payload["tenant_id"],
                         "system:sandbox-runtime", "tester_spec_reauthored",
                         _json.dumps({"files": reauthor_specs, "reason": "human_order",
                                      "cost_usd": 0.02})),
                    )
                conn.commit()
            finally:
                conn.close()
            files_changed = reauthor_specs
        return TesterTurnResult(
            sandbox_id=payload["sandbox_id"],
            test_files=list(state.tester_test_files),
            files_changed=files_changed,
            tests_ran=state.tester_tests_ran,
            tests_passed=state.tester_tests_passed,
            returncode=state.tester_returncode,
            failure_output=state.tester_failure_output,
            cost_usd=state.tester_cost_usd,
        )

    async def run_l2_review(payload: dict) -> L2Verdict:
        state.l2_calls += 1
        state.calls_log.append("run_l2_review")
        state.last_l2_payload = dict(payload)
        _maybe_fail_closed(state, ACTIVITY_RUN_L2_REVIEW)
        RunL2ReviewInput(**payload)  # REAL decode (extra=forbid — structural P3)
        if state.l2_fail_times > 0:
            state.l2_fail_times -= 1
            return L2Verdict(
                work_item_id=payload["work_item_id"], passed=False,
                objections=list(state.l2_objections), cost_usd=state.l2_cost_usd,
            )
        return L2Verdict(
            work_item_id=payload["work_item_id"], passed=True, objections=[],
            cost_usd=state.l2_cost_usd,
        )

    async def provision_sandbox(payload: dict) -> SandboxHandle:
        state.provision_calls += 1
        state.calls_log.append("provision_sandbox")
        _maybe_fail_closed(state, ACTIVITY_PROVISION_SANDBOX)
        inp = ProvisionSandboxInput(**payload)  # REAL decode
        return SandboxHandle(
            sandbox_id=f"sbx-{inp.work_item_id}",
            work_item_id=inp.work_item_id,
            tenant_id=inp.tenant_id,
            branch=f"dse/{inp.work_item_id}",
            container_id=f"ctr-{inp.work_item_id}",
        )

    def _files_for_turn(state) -> list[str]:
        seq = state.coder_files_changed_by_turn
        if not seq:
            return state.coder_files_changed
        return seq[min(state.coder_turn_calls - 1, len(seq) - 1)]

    async def run_coder_turn(payload: dict) -> CoderTurnResult:
        state.coder_turn_calls += 1
        state.coder_instructions.append(str(payload.get("instruction") or ""))
        state.calls_log.append("run_coder_turn")
        _maybe_transient_fail(state, ACTIVITY_RUN_CODER_TURN)
        _maybe_fail_closed(state, ACTIVITY_RUN_CODER_TURN)
        if state.coder_turn_hang_event is not None:
            await state.coder_turn_hang_event.wait()
        RunCoderTurnInput(**payload)  # REAL decode
        return CoderTurnResult(
            sandbox_id=payload["sandbox_id"],
            diff_summary="fake diff",
            files_changed=list(_files_for_turn(state)),
            cost_usd=state.coder_cost_usd,
            tokens_in=10,
            tokens_out=10,
        )

    async def checkpoint_sandbox(payload: dict) -> CheckpointRef:
        state.checkpoint_calls += 1
        state.calls_log.append("checkpoint_sandbox")
        inp = CheckpointSandboxInput(**payload)  # REAL decode
        if state.checkpoint_fail_times > 0:
            state.checkpoint_fail_times -= 1
            raise RuntimeError("simulated checkpoint failure")
        git_ref = "base0001" if inp.phase == "base" else f"head{state.checkpoint_calls:04d}"
        return CheckpointRef(
            work_item_id=inp.work_item_id, git_ref=git_ref, phase=inp.phase,
            base_sha="base0001",
        )

    async def rebuild_sandbox(payload: dict) -> SandboxHandle:
        state.rebuild_calls += 1
        state.calls_log.append("rebuild_sandbox")
        inp = RebuildSandboxInput(**payload)  # REAL decode (requires checkpoint_ref)
        return SandboxHandle(
            sandbox_id=f"sbx-{inp.work_item_id}-rebuilt",
            work_item_id=inp.work_item_id,
            tenant_id=inp.tenant_id,
            branch="dse/rebuilt",
        )

    async def teardown_sandbox(payload: dict) -> None:
        state.teardown_calls += 1
        state.calls_log.append("teardown_sandbox")
        TeardownSandboxInput(**payload)  # REAL decode (requires tenant_id)

    async def run_l1_pipeline(payload: dict) -> L1Result:
        state.l1_calls += 1
        state.calls_log.append("run_l1_pipeline")
        state.last_l1_payload = dict(payload)
        inp = RunL1PipelineInput(**payload)  # REAL contract decode (S7)
        wi = inp.sandbox.work_item_id
        if state.l1_findings_by_call:
            findings = state.l1_findings_by_call.pop(0)
            return L1Result(
                work_item_id=wi,
                passed=all(f.passed for f in findings),
                findings=list(findings),
            )
        if state.l1_fail_times > 0:
            state.l1_fail_times -= 1
            if state.l1_error_checks:
                return L1Result(
                    work_item_id=wi,
                    passed=False,
                    findings=[
                        L1Finding(check=c, passed=False, status=GateStatus.ERROR,
                                  detail=state.l1_fail_detail, summary=state.l1_fail_detail)
                        for c in state.l1_error_checks
                    ],
                )
            return L1Result(
                work_item_id=wi,
                passed=False,
                findings=[L1Finding(check="test", passed=False, detail=state.l1_fail_detail,
                                    inherited_failures=list(state.l1_inherited_failures))],
            )
        return L1Result(
            work_item_id=wi,
            passed=True,
            findings=[L1Finding(check="test", passed=True)],
        )

    async def finalize_pr(payload: dict) -> PrRef:
        state.finalize_calls += 1
        state.calls_log.append("finalize_pr")
        state.last_finalize_payload = dict(payload)
        inp = FinalizePrInput(**payload)  # REAL decode (requires summary + sandbox)
        wi = inp.work_item_id
        pr_number = state.pr_by_wi.get(wi)
        if pr_number is None:
            pr_number = state.pr_counter  # uses the current value BEFORE incrementing
            state.pr_counter += 1
            state.pr_by_wi[wi] = pr_number
        return PrRef(
            work_item_id=wi,
            pr_number=pr_number,
            url=f"https://github.com/x/y/pull/{pr_number}",
        )

    async def verify_merge_state(payload: dict) -> MergeVerification:
        # §F F1 — mirrors the real contract; the mode drives the verdict.
        state.calls_log.append("verify_merge_state")
        inp = VerifyMergeInput(**payload)  # REAL contract decode
        if state.merge_verify_mode == "not_merged":
            return MergeVerification(exists=True, merged=False, verified=False,
                                     reason="not_merged(state=open)")
        if state.merge_verify_mode == "unavailable":
            return MergeVerification(verified=False, reason="api_error:Timeout")
        return MergeVerification(exists=True, merged=True, merged_by="usr_test",
                                 merge_commit_sha="deadbeef", head_sha=inp.expected_head_sha,
                                 verified=True, reason="ok")

    # S3 (Phase 5): post_tracking_comment is now a REAL LOCAL Activity (in
    # LOCAL_ACTIVITIES) — it is no longer faked here, otherwise it collides (two
    # @activity.defn with the same name on the test worker). The real one is
    # best-effort: with no adapter up in tests, it returns ok=False without crashing.

    async def consume_ci_status(payload: dict) -> CiStatusResult:
        state.calls_log.append("consume_ci_status")
        state.last_ci_payload = dict(payload)
        inp = ConsumeCiStatusInput(**payload)  # REAL decode (requires tenant/repo/ref)
        status = state.ci_sequence.pop(0) if state.ci_sequence else "green"
        return CiStatusResult(
            work_item_id=inp.work_item_id, pr_number=inp.pr_number, status=status
        )

    # ------------------------------------------------------------------
    # Phase 3 — evidence pipeline (WS-E boundary). Each fake decodes the payload
    # with the REAL contract MODEL: if the workflow's call site drifts from the
    # contract, the test breaks HERE (not on the wire) — lesson from addendum 02.
    # ------------------------------------------------------------------
    def _preview_kind(files: list[str]) -> str:
        # mirror of WS-E's deterministic paths-filter (FR-20 + plan 08 §D) for
        # the fake — ui (front) takes precedence; otherwise a deployable service
        # (back: source/Dockerfile/manifest); otherwise none (docs/config).
        for f in files:
            if f.startswith(("ui/", "frontend/")) or f.endswith((".css", ".tsx", ".jsx")):
                return "ui"
        for f in files:
            if (f.endswith((".py", ".go", ".rb", ".java", ".ts", ".js", "Dockerfile"))
                    or f.startswith(("k8s/", "deploy/", "charts/"))
                    or f in ("pyproject.toml", "go.mod", "package.json")):
                return "deployable"
        return "none"

    async def trigger_preview(payload: dict) -> PreviewRef:
        state.trigger_preview_calls += 1
        state.calls_log.append("trigger_preview")
        state.last_preview_payload = dict(payload)
        inp = TriggerPreviewInput(**payload)  # REAL contract decode
        if state.preview_mode == "raise":
            raise ApplicationError("argocd unreachable (fake)",
                                   type="PreviewProvisionError", non_retryable=True)
        # Plan 08 §D — deploys_preview gate (operator-set). A disabled repo
        # skips CLEANLY (before any provisioning).
        if not inp.preview_enabled:
            return PreviewRef(work_item_id=inp.work_item_id, pr_number=inp.pr_number,
                              status="skipped_disabled", detail="repo with no preview (fake)")
        if state.preview_mode == "degraded":
            return PreviewRef(work_item_id=inp.work_item_id, pr_number=inp.pr_number,
                              status="degraded", detail="argocd sync failed (fake)")
        kind = _preview_kind(inp.files_changed)
        if state.preview_mode == "created" or kind != "none":
            return PreviewRef(
                work_item_id=inp.work_item_id, pr_number=inp.pr_number, status="created",
                namespace=f"preview-{inp.work_item_id}",
                url=f"http://preview-{inp.work_item_id}.local",
                kind=(kind if kind != "none" else "ui"),
            )
        return PreviewRef(work_item_id=inp.work_item_id, pr_number=inp.pr_number,
                          status="skipped_backend_only",
                          detail="paths-filter: no previewable change (§D)")

    async def run_demo_evidence(payload: dict) -> DemoEvidenceResult:
        state.demo_evidence_calls += 1
        state.calls_log.append("run_demo_evidence")
        state.last_demo_payload = dict(payload)
        inp = RunDemoEvidenceInput(**payload)  # REAL contract decode
        return DemoEvidenceResult(
            work_item_id=inp.work_item_id, passed=state.demo_passed,
            video_artifact_key=state.demo_video_key,
            trace_artifact_key=state.demo_trace_key,
            duration_s=1.2, detail="fake demo (publish internal to WS-E)",
        )

    async def run_visual_diff(payload: dict) -> VisualDiffResult:
        state.visual_diff_calls += 1
        state.calls_log.append("run_visual_diff")
        state.last_visual_diff_payload = dict(payload)
        inp = RunVisualDiffInput(**payload)  # REAL contract decode
        baseline_created = inp.base_screenshot_key is None
        return VisualDiffResult(
            work_item_id=inp.work_item_id,
            passed=state.visual_diff_changed_pct <= inp.threshold_pct,
            changed_pct=state.visual_diff_changed_pct,
            diff_artifact_key=f"evidence/{inp.work_item_id}/visual.png",
            baseline_created=baseline_created,
        )

    async def update_base_branch(payload: dict) -> UpdateBaseBranchResult:
        state.update_base_calls += 1
        state.calls_log.append("update_base_branch")
        state.last_update_base_payload = dict(payload)
        inp = UpdateBaseBranchInput(**payload)  # REAL contract decode (WSE-E6-T16)
        if state.update_base_fail_times > 0:
            state.update_base_fail_times -= 1
            raise RuntimeError("fake: merge-base workspace unavailable on this pod")
        if state.base_conflict:
            # unresolvable conflict: the workflow must escalate (never force a resolution)
            return UpdateBaseBranchResult(
                work_item_id=inp.work_item_id, strategy="merge_base",
                conflict=True, orphaned_threads=0, detail="merge conflict (fake)",
            )
        if not state.base_has_drift:
            return UpdateBaseBranchResult(
                work_item_id=inp.work_item_id, strategy="noop_no_drift",
                conflict=False, orphaned_threads=0, detail="no drift (fake)",
            )
        # P1: the strategy is determined by first_human_review_done. After the
        # 1st review (True) -> ALWAYS merge_base (never rebase). Zero orphans by
        # construction — unless the test forces the violation scenario.
        strategy = "merge_base" if inp.first_human_review_done else "rebase_prefirst_review"
        return UpdateBaseBranchResult(
            work_item_id=inp.work_item_id, strategy=strategy, conflict=False,
            orphaned_threads=state.base_orphaned_threads, detail="fake merge-base",
        )

    return [
        activity.defn(name=ACTIVITY_UPDATE_BASE_BRANCH)(update_base_branch),
        activity.defn(name=ACTIVITY_RUN_PLANNER_TURN)(run_planner_turn),
        activity.defn(name=ACTIVITY_RUN_TESTER_TURN)(run_tester_turn),
        activity.defn(name=ACTIVITY_RUN_L2_REVIEW)(run_l2_review),
        activity.defn(name=ACTIVITY_PROVISION_SANDBOX)(provision_sandbox),
        activity.defn(name=ACTIVITY_RUN_CODER_TURN)(run_coder_turn),
        activity.defn(name=ACTIVITY_CHECKPOINT_SANDBOX)(checkpoint_sandbox),
        activity.defn(name=ACTIVITY_REBUILD_SANDBOX)(rebuild_sandbox),
        activity.defn(name=ACTIVITY_TEARDOWN_SANDBOX)(teardown_sandbox),
        activity.defn(name=ACTIVITY_RUN_L1_PIPELINE)(run_l1_pipeline),
        activity.defn(name=ACTIVITY_FINALIZE_PR)(finalize_pr),
        activity.defn(name=ACTIVITY_VERIFY_MERGE_STATE)(verify_merge_state),
        # post_tracking_comment: real, comes from LOCAL_ACTIVITIES (S3) — do not register here.
        activity.defn(name=ACTIVITY_CONSUME_CI_STATUS)(consume_ci_status),
        activity.defn(name=ACTIVITY_TRIGGER_PREVIEW)(trigger_preview),
        activity.defn(name=ACTIVITY_RUN_DEMO_EVIDENCE)(run_demo_evidence),
        activity.defn(name=ACTIVITY_RUN_VISUAL_DIFF)(run_visual_diff),
    ]
