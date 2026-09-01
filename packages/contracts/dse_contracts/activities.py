"""Activity contract between WS-B (orchestrator, the caller), WS-C (sandbox,
which implements the execution Activities) and WS-E (validation/PR, which
implements the gate/finalize Activities). The types below are what crosses the
Activity boundary — the real `@activity.defn`s (decorated with
`temporalio.activity`) live in the owning service, but take/return these types
so that WS-B can write the workflow against a stable interface before any
implementation exists.

Convention: every real activity is registered in the single Worker in
`services/orchestrator/worker.py` (owner: WS-B), which imports each
workstream's Activities module. A defensive import (try/except ImportError) is
expected while the workstreams build in parallel.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .plan_artifact import PlanArtifact


# ---------------------------------------------------------------------------
# Activity names (used in `@activity.defn(name=...)` and in
# `workflow.execute_activity(name, ...)` — they must match on both sides).
# ---------------------------------------------------------------------------
ACTIVITY_PROVISION_SANDBOX = "provision_sandbox"
ACTIVITY_RUN_CODER_TURN = "run_coder_turn"
ACTIVITY_CHECKPOINT_SANDBOX = "checkpoint_sandbox"
ACTIVITY_REBUILD_SANDBOX = "rebuild_sandbox"
ACTIVITY_TEARDOWN_SANDBOX = "teardown_sandbox"
ACTIVITY_RUN_L1_PIPELINE = "run_l1_pipeline"
#: O conserto determinístico que o REPOSITÓRIO declara (`commands.lint_fix`),
#: rodado quando o gate `lint` reprova e antes de gastar um turno de modelo.
ACTIVITY_LINT_AUTOFIX = "lint_autofix"
ACTIVITY_FINALIZE_PR = "finalize_pr"
ACTIVITY_VERIFY_MERGE_STATE = "verify_merge_state"  # plan 08 §F (F1)
ACTIVITY_POST_TRACKING_COMMENT = "post_tracking_comment"
ACTIVITY_CONSUME_CI_STATUS = "consume_ci_status"
ACTIVITY_EMIT_AUDIT = "emit_audit_event"

# --- Phase 2 (stage-scoped session split + L2, ADR-13/FR-08/FR-13) ---
# Owners: WS-C implements the sessions (planner/tester/reviewer L2 — the L2
# session is built in WS-C by the de-duplication decision of master plan §7;
# WS-E orchestrates the fix-retry loop around it); WS-B calls them by name.
ACTIVITY_RUN_PLANNER_TURN = "run_planner_turn"
# Fase A2 (2026-08-19) — bootstrap do manifesto: o probe roda antes do Planner
# (uma chamada de API contra 4 estágios pagos), e o bootstrap abre a PR de
# arquivo único quando o manifesto está confirmadamente ausente.
ACTIVITY_PROBE_REPO_MANIFEST = "probe_repo_manifest"
ACTIVITY_BOOTSTRAP_REPO_MANIFEST = "bootstrap_repo_manifest"
# rc.105 — manifesto presente mas incompleto: emenda que ACRESCENTA a chave
# faltante. Não encerra a tarefa (o gate roda; só o preview degrada).
ACTIVITY_AMEND_REPO_MANIFEST = "amend_repo_manifest"
ACTIVITY_RUN_TESTER_TURN = "run_tester_turn"
ACTIVITY_RUN_L2_REVIEW = "run_l2_review"

# --- Phase 3 (evidence pipeline, ADR-26/ADR-27/§10.12-13) ---
# Owners: WS-E implements it (evidence/preview/artifacts); WS-B calls by name
# after the PR is finalized. Defined BEFORE the implementation (Phase 3 entry
# gate, addendum 02 §2.3) — the matching input/output models live in THIS file
# from day zero, so the boundary bug class of Phases 1-2 (14 occurrences) has
# nowhere to be born.
ACTIVITY_RUN_DEMO_EVIDENCE = "run_demo_evidence"
ACTIVITY_PUBLISH_ARTIFACT = "publish_artifact"
ACTIVITY_TRIGGER_PREVIEW = "trigger_preview"
# Preview degradado → um agente decide se uma MUDANÇA DE CÓDIGO conserta
# (decisão de operador, 2026-08-12: o laço fecha sem humano; política —
# tetos, no-op, gasto — continua determinística no workflow).
#: rc.130 — the evidence a human-requested CI fix carries (names, urls, log
#: tails). The preview triage that lived here (0/8 dispatches ever produced
#: a `created` preview) is gone with the automatic loop it fed.
ACTIVITY_FETCH_CI_FAILURE_EVIDENCE = "fetch_ci_failure_evidence"
# rc.103 — o LLM decide o caminho fundo do link de preview (a plataforma
# valida e compõe); roda antes do trigger, uma vez por rodada de evidência.
ACTIVITY_RESOLVE_PREVIEW_DEEP_LINK = "resolve_preview_deep_link"
ACTIVITY_RUN_VISUAL_DIFF = "run_visual_diff"

# --- Phase 4 (loop hardening & learning) ---
# Defined BEFORE the implementation (entry gate §4 of addendum 03, same
# discipline as Phase 3). Owners: merge-base = WS-E; skill promotion = WS-C
# (the eval->approval->canary->rollback pipeline); WS-B calls them by name.
ACTIVITY_UPDATE_BASE_BRANCH = "update_base_branch"   # merge-base, never rebase (WSE-E6-T16)
ACTIVITY_EVAL_SKILL_CANDIDATE = "eval_skill_candidate"  # replay against eval set (WSC-E4-T3)
ACTIVITY_PROMOTE_SKILL = "promote_skill"             # governed state transition (WSC-E4-T3)


# ---------------------------------------------------------------------------
# Owner: WS-C (services/sandbox-runtime)
# ---------------------------------------------------------------------------
class SandboxHandle(BaseModel):
    sandbox_id: str
    work_item_id: str
    tenant_id: str
    branch: str
    container_id: str | None = None  # id of the Docker container behind the handle
    # Additive: new runtimes return the SHAs that delimit the session. The
    # defaults keep handles already recorded in Temporal histories decodable.
    base_sha: str | None = None
    head_sha: str | None = None


class CheckpointRef(BaseModel):
    work_item_id: str
    git_ref: str  # commit sha on the task branch
    phase: str  # name of the phase boundary at which the checkpoint was taken
    base_sha: str | None = None


class CoderTurnResult(BaseModel):
    sandbox_id: str
    diff_summary: str
    files_changed: list[str]
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    base_sha: str | None = None
    head_sha: str | None = None
    # model_call_ledger.id written for this turn; None = not metered into the
    # ledger. The coder's spend never passes through the gateway client, so it
    # was absent from the ledger entirely and the console's cost rollup — which
    # is computed only from that table — understated real spend 56x. The
    # projector uses this to tell a metered turn from a legacy one, so the same
    # money is never counted twice.
    ledger_id: int | None = None


# ---------------------------------------------------------------------------
# Canonical inputs of the sandbox/validation boundaries. Historically some of
# these models lived only in the owning service; that let the workflow fakes
# accept dicts that differed from the real wire. They now live in the contracts
# package, with additive defaults for already-persisted histories. The owning
# service may resolve empty fields server-side from the work_item_id.
# ---------------------------------------------------------------------------
class ProvisionSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    branch: str | None = None
    base_branch: str = "main"
    repo: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    image: str | None = None
    base_sha: str | None = None


class CheckpointSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    sandbox_id: str | None = None
    branch: str | None = None
    phase: str = "manual"
    base_sha: str | None = None


class RebuildSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    checkpoint_ref: CheckpointRef
    branch: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    image: str | None = None
    base_sha: str | None = None
    #: Where to re-clone from when the checkpoint did not survive. The checkpoint
    #: volume is an emptyDir unless a PVC is configured, so on a rebuild it
    #: usually has NOT survived — and without these the bootstrap fell through to
    #: initialising an empty git repo, handing the Coder a workspace with none of
    #: the customer's code. Optional so an older history replays unchanged.
    repo: str | None = None
    base_branch: str | None = None


class TeardownSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    stage: str = "coder"


class RunCoderTurnInput(BaseModel):
    work_item_id: str
    tenant_id: str
    instruction: str
    sandbox_id: str | None = None
    branch: str | None = None
    stage: str = "coder"
    task_class: str = "default"
    data_class: str = "internal"
    model_override: str | None = None
    runtime_override: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    # Plan anchor (found on a real run: the CLI creates spontaneous files —
    # BUG_FIX_REPORT.md). Advisory since 2026-07-22 (see the L1
    # plan_compliance): it is NOT an equality gate on the diff. After the turn,
    # the deterministic prune deletes only NEW DISPOSABLE files (report/log/
    # scratch) — a legitimate new source file outside this list SURVIVES
    # (P1; see _prune_disposable_artifacts).
    expected_files: list[str] = Field(default_factory=list)


class RunL1PipelineInput(BaseModel):
    """Input tolerant of the old payload.

    ``sandbox``/``plan`` were absent in old histories; the owner resolves those
    gaps server-side using ``work_item_id``. New callers always send base/head
    SHA and L1 computes the diff from the immutable SHA.
    """

    work_item_id: str = ""
    sandbox_id: str | None = None
    sandbox: SandboxHandle | None = None
    plan: PlanArtifact | None = None
    tenant_id: str = ""
    base_branch: str = "main"
    base_sha: str = ""
    head_sha: str = ""
    target_dir: str = "."
    repo_dir: str = "/workspace/repo"

    @model_validator(mode="after")
    def _derive_work_item_id(self) -> "RunL1PipelineInput":
        if not self.work_item_id and self.sandbox is not None:
            self.work_item_id = self.sandbox.work_item_id
        if not self.sandbox_id and self.sandbox is not None:
            self.sandbox_id = self.sandbox.sandbox_id
        if not self.base_sha and self.sandbox is not None and self.sandbox.base_sha:
            self.base_sha = self.sandbox.base_sha
        if not self.head_sha and self.sandbox is not None and self.sandbox.head_sha:
            self.head_sha = self.sandbox.head_sha
        return self


class FinalizePrInput(BaseModel):
    """Additive finalize; empty fields are resolved by the owner from the SoR."""

    work_item_id: str
    tenant_id: str = ""
    repo: str = ""
    branch: str = ""
    base_branch: str = "main"
    base_sha: str = ""
    head_sha: str = ""
    summary: str = ""
    risk_class: str = "low"
    evidence_url: str = ""
    issue_ref: dict[str, Any] | None = None
    sandbox_id: str | None = None
    sandbox: SandboxHandle | None = None
    repo_dir: str = "/workspace/repo"
    strict_mode: bool | None = None
    surface_ref: dict[str, Any] | None = None
    #: Tudo que o item mudou, para o corpo da PR apontar as edições de TESTE.
    #: Desde 2026-08-10 o DSE altera qualquer teste e nada mais o contém —
    #: a supervisão é o diff da PR, e um aviso nomeando os arquivos é o que
    #: dá lugar a ela. Aditivo: histórias antigas decodificam com [].
    files_changed: list[str] = Field(default_factory=list)


class ConsumeCiStatusInput(BaseModel):
    """New fields carry defaults so historical payloads self-heal."""

    work_item_id: str
    tenant_id: str = ""
    repo: str = ""
    pr_number: int
    ref: str = ""
    base_sha: str = ""
    head_sha: str = ""
    surface_ref: dict[str, Any] | None = None


class PersistWorkItemStateInput(BaseModel):
    """Idempotent projection of the workflow into Postgres.

    Only ``work_item_id`` is required, so that old Activity payloads keep
    decoding. Absent fields are preserved by the server; plan/hash/
    expected_files are derived server-side.
    """

    work_item_id: str
    status: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    plan: dict[str, Any] | None = None
    risk_class: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    ci_status: str | None = None
    last_error: str | None = None
    clear_ci_status: bool = False
    validation_attempts: dict[str, int] | None = None


# ---------------------------------------------------------------------------
# Owner: WS-E (services/validation)
# ---------------------------------------------------------------------------
class GateStatus(str, Enum):
    """Structured result shared by L1/L2/L3.

    Only PASS authorizes moving forward. ``passed`` still exists on the models
    for compatibility with old producers/consumers.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class L1Finding(BaseModel):
    check: str  # "lint" | "typecheck" | "test" | "build" | "sast" | "secret_scan" | "diff_budget" | "forbidden_paths"
    passed: bool = False
    status: GateStatus | None = None
    detail: str = ""
    #: A one-line reason written BY THE PLATFORM, holding no value the check
    #: read out of the repository or out of a subprocess. `detail` is the
    #: opposite: it carries scanner output, compiler output, matched source
    #: lines — whatever the gate actually saw.
    #:
    #: The split exists because the two go to different places. `detail` may
    #: only reach `validation_runs`, which retention can clean. `summary` is
    #: the only thing allowed into `audit_log`, which is append-only (0028),
    #: exempt from retention by design, and copied verbatim into the console
    #: read model — a value written there can be rotated, never scrubbed.
    #:
    #: How long this check took, in seconds. Without it 583 of the L1 gate's 638
    #: seconds were unattributable — the pipeline knew which stage was running
    #: (the heartbeat carries that) but nothing durable recorded how long any of
    #: them took, so every proposal to make the gate faster was a guess about
    #: which stage to attack.
    duration_seconds: float = 0.0

    #: This is an ALLOWLIST, and it replaced a denylist of check names that was
    #: wrong in both directions: it dropped `sast`'s ERROR reason (the very
    #: incident this field exists for) while still letting `lint`'s exit-127
    #: branch put raw sandbox stderr — from a command the customer's own
    #: manifest chose — into the permanent ledger. Sensitivity is a property of
    #: a BRANCH, not of a check name, so the branch declares it. A check that
    #: sets nothing here says nothing to the ledger: fail-closed.
    summary: str = ""
    #: Suites que JÁ falhavam no `base_sha` — vermelho que o item encontrou, não
    #: que trouxe. Nomes (caminho/classe) porque `detail`/`validation_runs`
    #: aguentam nomes; quem for para o `audit_log` publica a CONTAGEM.
    #: Aditivo e opcional: um worker antigo decodifica igual.
    inherited_failures: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_status(self) -> "L1Finding":
        if self.status is None:
            self.status = GateStatus.PASS if self.passed else GateStatus.FAIL
        elif self.passed != (self.status == GateStatus.PASS):
            raise ValueError("passed must be true only when status=PASS")
        return self


class L1Result(BaseModel):
    work_item_id: str
    passed: bool = False
    status: GateStatus | None = None
    findings: list[L1Finding]
    base_sha: str | None = None
    head_sha: str | None = None

    @model_validator(mode="after")
    def _normalize_status(self) -> "L1Result":
        if self.status is None:
            if self.passed:
                self.status = GateStatus.PASS
            else:
                statuses = {f.status for f in self.findings}
                for candidate in (
                    GateStatus.ERROR,
                    GateStatus.NOT_CONFIGURED,
                    GateStatus.FAIL,
                    GateStatus.SKIPPED,
                ):
                    if candidate in statuses:
                        self.status = candidate
                        break
                else:
                    self.status = GateStatus.FAIL
        elif self.passed != (self.status == GateStatus.PASS):
            raise ValueError("passed must be true only when status=PASS")
        return self


class PrRef(BaseModel):
    # Phase 2 (addendum 01 §4, approved by the architect): optional `pr_number` +
    # `compare_url` for the strict mode (WSE-E3-T8) in which the system only
    # pushes the branch and posts a compare link — the PR is opened by a human.
    # Additive change: every Phase 1 caller keeps building with pr_number
    # filled in; exactly one of the two must be present.
    work_item_id: str
    pr_number: int | None = None
    url: str
    compare_url: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None


class VerifyMergeInput(BaseModel):
    """Plan 08 §F (F1) — asks for the REAL state of the PR at the source
    (GitHub) to confirm a merge signal against the truth, not only against the
    envelope (which a forged webhook could fake — pr_number/repo/sha are not
    secrets)."""

    work_item_id: str
    tenant_id: str
    repo: str
    pr_number: int
    expected_head_sha: str | None = None


class MergeVerification(BaseModel):
    """Result of the API check. `verified` is only True when the PR exists, is
    in fact `merged`, and (when provided) the expected head_sha matches.
    Fail-safe: any doubt => verified=False (the workflow does NOT conclude as
    done)."""

    exists: bool = False
    merged: bool = False
    merged_by: str | None = None
    merge_commit_sha: str | None = None
    head_sha: str | None = None
    verified: bool = False
    reason: str = ""


class L2Verdict(BaseModel):
    """Structured verdict of the fresh-context Reviewer session (Phase 2,
    WSC-E3-T5 builds the session / WSE-E2 orchestrates the loop). P3: the L2
    session receives ONLY the plan artifact + final diff — never the Coder's
    history."""

    work_item_id: str
    passed: bool = False
    status: GateStatus | None = None
    objections: list[str] = []  # empty when passed; specific (file/line) when not
    cost_usd: float = 0.0
    base_sha: str | None = None
    head_sha: str | None = None

    @model_validator(mode="after")
    def _normalize_status(self) -> "L2Verdict":
        if self.status is None:
            self.status = GateStatus.PASS if self.passed else GateStatus.FAIL
        elif self.passed != (self.status == GateStatus.PASS):
            raise ValueError("passed must be true only when status=PASS")
        return self


class FailingCheck(BaseModel):
    """One red check, as the human (and a fix instruction) needs to see it.

    rc.130. The ledger always knew the names (`wse_ci_status`, the
    `ci_status_consumed` audit row) — the workflow only ever received the
    string "red". Measured on wi_f1f27266: eight paid fix rounds whose whole
    instruction was "ci red: fix the pipeline", every one of them changing no
    file, until the retry cap escalated with a card that named nothing.
    """

    name: str
    conclusion: str = ""
    url: str | None = None


class CiStatusResult(BaseModel):
    work_item_id: str
    pr_number: int
    # "pending" | "green" | "red" | "no_ci". `no_ci` means neither a check run
    # nor a commit status exists for the ref — the repo has no CI, which is a
    # TERMINAL observation, not a wait. It is deliberately distinct from
    # `pending`: collapsing the two is what made every PR wait forever.
    status: str
    head_sha: str | None = None
    # Additive: a historical payload without the key decodes to [] — this model
    # has no extra="forbid", and producer and consumer share one image.
    failing_checks: list[FailingCheck] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Session models promoted to the foundation (addendum 02 §2.3 — Phase 3, entry
# gate). In Phases 1-2 these models lived in `sandbox_runtime.activities` and
# the caller payload (WS-B) drifted away from the declared fields without any
# test catching it (lenient fakes on both sides) — 14 boundary bugs in total.
# From here on: ONE source of truth, with boundary regression tests in
# packages/contracts/tests/test_activity_boundaries.py that validate the EXACT
# payloads the workflow sends. `sandbox_runtime` imports from here and
# re-exports for compatibility.
# ---------------------------------------------------------------------------
class RunPlannerTurnInput(BaseModel):
    """Input of the Planner session (WSC-E3-T3). Tolerant by design of the
    aliases the WS-B workflow sends (`instructions` list / `base_branch`) —
    a Phase 2 integration reconciliation, now part of the contract."""

    model_config = {"populate_by_name": True}

    work_item_id: str
    tenant_id: str
    instruction: str = ""
    instructions: list[str] = Field(default_factory=list)
    repo: str = "app"
    branch: str | None = None
    base_branch: str | None = None
    base_sha: str | None = None
    task_class: str = "default"
    data_class: str = "internal"
    diff_budget_lines: int = 400
    related_tickets: list[str] = Field(default_factory=list)
    model_override: str | None = None  # tolerated; model policy lives in the gateway

    @model_validator(mode="after")
    def _reconcile(self) -> "RunPlannerTurnInput":
        if not self.instruction and self.instructions:
            self.instruction = " ".join(s for s in self.instructions if s)
        if self.branch is None and self.base_branch is not None:
            self.branch = self.base_branch
        return self


class RunTesterTurnInput(BaseModel):
    """Input of the Tester session (WSC-E3-T4). Tolerant of the WS-B aliases
    (`plan` dict + `sandbox_id`); derives `instruction` from the test_plan."""

    model_config = {"populate_by_name": True}

    work_item_id: str
    tenant_id: str
    instruction: str = ""
    plan: dict[str, Any] | None = None
    sandbox_id: str | None = None
    repo: str = "app"
    branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    task_class: str = "default"
    data_class: str = "internal"
    model_override: str | None = None
    runtime_override: str | None = None

    @model_validator(mode="after")
    def _reconcile(self) -> "RunTesterTurnInput":
        if not self.instruction and self.plan:
            self.instruction = str(self.plan.get("test_plan") or "write/adjust tests for the change")
        return self


class TesterTurnResult(BaseModel):
    #: The Tester authored tests but did NOT run the suite — L1's `test` gate
    #: is the verdict. `tests_passed` is then not an opinion about the code and
    #: the workflow must not treat it as one.
    #:
    #: Why the field rather than just setting `tests_passed=True`: that would be
    #: a false green, indistinguishable from a suite that really passed. This
    #: says "no verdict was taken here", which is the truth.
    suite_deferred: bool = False

    """Return of the Tester session. Superset compatible with `CoderTurnResult`
    (the workflow decodes the return as CoderTurnResult — `files_changed`
    mirrors `test_files`)."""

    sandbox_id: str
    test_files: list[str]
    tests_ran: bool
    tests_passed: bool
    returncode: int
    status: GateStatus | None = None
    cost_usd: float = 0.0
    diff_summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    base_sha: str | None = None
    head_sha: str | None = None
    # Tail of the suite's stdout+stderr when it failed. The runtime already
    # captured this and wrote it to a log line, where nothing could read it: the
    # workflow retried the Coder with the SAME instruction because it had
    # nothing else to say. Four rounds of an identical request, then the retry
    # cap. Empty on success, and defaulted so an older worker still decodes.
    failure_output: str = ""

    @model_validator(mode="after")
    def _mirror_test_files(self) -> "TesterTurnResult":
        if not self.files_changed:
            self.files_changed = list(self.test_files)
        if not self.diff_summary:
            self.diff_summary = f"tester: {len(self.test_files)} test file(s)"
        if self.status is None:
            if not self.tests_ran:
                self.status = GateStatus.NOT_CONFIGURED
            elif self.tests_passed and (self.suite_deferred or self.returncode == 0):
                # `returncode` stays truthful in the payload — it is what the
                # suite actually did — but it is not the VERDICT when the
                # verdict was deferred to L1. Reading it as one here is what
                # made a deferred run still come back FAIL.
                self.status = GateStatus.PASS
            else:
                self.status = GateStatus.FAIL
        return self


class RunL2ReviewInput(BaseModel):
    """Input of the L2 Reviewer session (WSC-E3-T5). STRUCTURAL P3, hardened on
    promotion to the contract: `extra="forbid"` — any attempt to pass a field
    beyond the declared ones (e.g. Coder history/instructions) fails at the
    Activity DECODE, not only in a test. The fields are exactly
    {work_item_id, tenant_id, plan, diff, task_class, data_class}; the one who
    adapts is always the CALLER (WS-B sends `diff`, never `diff_summary`)."""

    model_config = {"extra": "forbid"}

    work_item_id: str
    tenant_id: str
    plan: PlanArtifact
    diff: str
    task_class: str = "default"
    data_class: str = "internal"
    base_sha: str | None = None
    head_sha: str | None = None


# ---------------------------------------------------------------------------
# Phase 3 — evidence pipeline (owner: WS-E; caller: WS-B). Defined in the
# contract BEFORE any implementation exists (entry gate).
# ---------------------------------------------------------------------------
class RunDemoEvidenceInput(BaseModel):
    """Runs the task's `@demo` test(s) (convention `demos/<work_item_id>/`,
    ADR-27) with video recording. The test is authored by the Tester
    (WSC-E3-T4b); the EXECUTION here is deterministic — real Playwright, no
    LLM."""

    work_item_id: str
    tenant_id: str
    sandbox: SandboxHandle | None = None
    demo_dir: str = ""  # derived default: demos/<work_item_id>/
    base_url: str | None = None  # preview URL (TriggerPreview) when there is one; local fixture otherwise
    timeout_s: int = 120
    head_sha: str | None = None


class DemoEvidenceResult(BaseModel):
    work_item_id: str
    passed: bool
    status: GateStatus | None = None
    video_artifact_key: str | None = None  # key in the artifact store (published via PublishArtifact)
    trace_artifact_key: str | None = None
    duration_s: float = 0.0
    detail: str = ""
    head_sha: str | None = None

    @model_validator(mode="after")
    def _normalize_status(self) -> "DemoEvidenceResult":
        if self.status is None:
            self.status = GateStatus.PASS if self.passed else GateStatus.FAIL
        elif self.passed != (self.status == GateStatus.PASS):
            raise ValueError("passed must be true only when status=PASS")
        return self


class PublishArtifactInput(BaseModel):
    work_item_id: str
    tenant_id: str
    kind: str  # "demo_video" | "playwright_trace" | "visual_diff" | "test_report"
    local_path: str
    content_type: str = "application/octet-stream"
    ttl_seconds: int = 7 * 24 * 3600  # short-TTL presigned URL by policy


class ArtifactRef(BaseModel):
    work_item_id: str
    tenant_id: str
    kind: str
    store_key: str  # S3 key in Garage, tenant-prefixed (NFR-03)
    presigned_url: str
    expires_at: str  # ISO-8601 — evidence links EXPIRE by policy (Phase 3 exit)


class TriggerPreviewInput(BaseModel):
    """Triggers (or skips) the per-PR preview environment. The decision is
    DETERMINISTIC by paths-filter (FR-20) — never by model:

    - `preview_enabled` (plan 08 §D) — operator-set gate (`repo_bindings.
      deploys_preview`): a repo not marked as "generates preview" skips with
      `skipped_disabled`. Default True keeps the previous behavior for callers
      that do not pass the gate (single-repo/no config).
    - `ui_path_globs` + `deployable_globs` — the preview is warranted if the
      change touches UI (front) OR a deployable service (back). Docs/test-only
      change → `skipped_backend_only` (counts as success, NEVER blocks)."""

    work_item_id: str
    tenant_id: str
    repo: str
    #: rc.131 — `None` for the smoke (`preview check ui|deployable`): a preview
    #: proved OUTSIDE an item, with no PR to write to.
    pr_number: int | None = None
    files_changed: list[str] = Field(default_factory=list)
    head_sha: str | None = None
    preview_enabled: bool = True
    #: rc.131 — explicit branch/kind/TTL for the smoke. An item's preview keeps
    #: the conventions (`dse/<work_item_id>`, paths-filter, config default).
    branch: str | None = None
    kind: str | None = None  # "ui" | "deployable" — never a synthetic files_changed
    ttl_seconds: int | None = None
    #: rc.103 — o caminho fundo decidido pelo LLM (validado pela plataforma) e
    #: a nota de 1 linha. Opcionais e aditivos: payload antigo decodifica igual.
    deep_path: str | None = None
    deep_note: str | None = None
    #: "How to test" (mesmo turno do deep link): {steps, login}; {} = sem guia.
    test_guide: dict = Field(default_factory=dict)
    # `**/*.html` is load-bearing: a plain static page is the most common shape
    # of a UI change, and without it a PR that only edits index.html was
    # classified backend-only and silently skipped the preview.
    # `**/*.component.ts`: o Angular escreve UI em `.ts`, e `.ts` está nos
    # globs DEPLOYABLE — sem esta entrada, um change só-de-componente
    # classificava um FE Angular como serviço de backend e ganhava a receita
    # errada (medido no wi_cc72b204: `npm: not found` numa imagem JDK). O
    # sufixo `.component.ts` é convenção inequívoca do framework, então um
    # backend Node em TypeScript (`src/server.ts`) segue `deployable`.
    # `src/app/**` (2026-08-14, wi_e15f4991): a SEGUNDA encarnação do mesmo
    # bug — diff Angular só-de-estado (reducers/selectors/types .ts) não tem
    # `.component.ts` nem template, caía no `**/*.ts` do deployable e ganhava
    # imagem sem npm. `src/app/` é a convenção do Angular CLI; back Java vive
    # em `src/main/` e segue deployable.
    ui_path_globs: list[str] = Field(default_factory=lambda: ["ui/**", "frontend/**", "src/app/**", "**/*.html", "**/*.css", "**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.svelte", "**/*.component.ts"])
    # plan 08 §D — deployable service (back): source/manifest/container that
    # change the artifact served in the preview. Docs/test-only do not match
    # and skip.
    deployable_globs: list[str] = Field(default_factory=lambda: [
        "**/Dockerfile", "Dockerfile", "**/*.py", "**/*.go", "**/*.rb",
        "**/*.java", "**/*.ts", "**/*.js", "k8s/**", "deploy/**", "charts/**",
        "**/requirements*.txt", "pyproject.toml", "go.mod", "package.json",
        # Arquivo de build do Java. A lista já tinha `**/*.java` mas nenhum
        # POM: uma PR que só mexe em dependência ou plugin (metade do trabalho
        # de onboarding de um repo Maven) não era previewável, e o sintoma era
        # "preview não saiu" sem nada explicando.
        "pom.xml", "**/pom.xml",
    ])


class PreviewRef(BaseModel):
    work_item_id: str
    pr_number: int | None = None
    status: str  # "created" | "skipped_backend_only" | "skipped_disabled" | "degraded"
    namespace: str | None = None
    url: str | None = None
    detail: str = ""
    # plan 08 §D — "ui" | "deployable" | "" — which filter triggered the
    # preview (evidence in the PR/console for why this PR did or did not get
    # a preview).
    kind: str = ""
    deep_path: str | None = None
    deep_note: str | None = None
    #: "How to test" — {steps: [...], login: "..."} gerado no mesmo turno do
    #: deep link; {} = sem guia. Aditivo: payload antigo decodifica igual.
    test_guide: dict = Field(default_factory=dict)


class FetchCiFailureEvidenceInput(BaseModel):
    """What a human-requested CI fix needs to hear. Measured on wi_f1f27266:
    eight paid rounds whose whole instruction was "ci red: fix the pipeline"
    changed no file — the check names and their messages never crossed."""

    work_item_id: str
    tenant_id: str
    repo: str
    ref: str  # head sha (or branch) the checks ran on
    pr_number: int | None = None


class CiCheckEvidence(BaseModel):
    name: str
    conclusion: str = ""
    url: str | None = None
    #: The END of the job's log (bounded), or the check's annotations, or
    #: nothing — `source` says which, so the instruction never implies more
    #: than was fetched.
    log_tail: str = ""
    source: str = "none"  # "job_log" | "annotations" | "none"


class CiFailureEvidence(BaseModel):
    work_item_id: str
    checks: list[CiCheckEvidence] = Field(default_factory=list)


class RunVisualDiffInput(BaseModel):
    work_item_id: str
    tenant_id: str
    base_screenshot_key: str | None = None  # artifact store; None = first run (baseline)
    candidate_screenshot_path: str = ""
    threshold_pct: float = 0.1


class VisualDiffResult(BaseModel):
    work_item_id: str
    passed: bool
    changed_pct: float = 0.0
    diff_artifact_key: str | None = None
    baseline_created: bool = False
    # Additive (requested by WS-E during the Phase 3 integration): the baseline
    # key in the artifact store, for the caller to persist and resend as
    # `base_screenshot_key` on the following runs — before this the key came
    # back overloaded in `diff_artifact_key` when baseline_created=True.
    baseline_artifact_key: str | None = None


# ---------------------------------------------------------------------------
# Phase 4 — merge-base / base-drift (owner WS-E, WSE-E6-T16) and skill
# promotion (owner WS-C, WSC-E4-T3). Contracts defined at the entry gate,
# before the build. VALIDATION NOTE (addendum 03): merge-base is NEW
# construction — Phase 1 never implemented drift handling, despite what the
# plan text says.
# ---------------------------------------------------------------------------
class UpdateBaseBranchInput(BaseModel):
    """Updates the task branch with the base drift WITHOUT rewriting history.
    P1: the strategy is deterministic (code, not model). `first_human_review`
    marks the only moment when rebase is allowed (before the 1st review): after
    it, ONLY merge-base-into-branch — rebase+force-push orphans the review
    threads anchored on the rewritten commits (verified GitHub behavior,
    failure mode 11)."""

    work_item_id: str
    tenant_id: str
    repo: str
    branch: str
    base_branch: str
    first_human_review_done: bool = True  # safe default: assume a review already happened → never rebase


class UpdateBaseBranchResult(BaseModel):
    work_item_id: str
    strategy: str          # "merge_base" | "rebase_prefirst_review" | "noop_no_drift"
    conflict: bool = False  # unresolvable conflict → workflow escalates to a human (never force-resolves)
    orphaned_threads: int = 0  # Phase 4 exit assertion: MUST be 0
    detail: str = ""


class EvalSkillCandidateInput(BaseModel):
    """Replay of a skill candidate against the historical eval set (positives
    and negatives) — WSC-E4-T3. Deterministic; produces a score, not a decision
    (approval is human)."""

    tenant_id: str
    skill_key: str
    candidate_version: int


class EvalSkillCandidateResult(BaseModel):
    skill_key: str
    candidate_version: int
    passed: bool
    score: float = 0.0
    positive_hits: int = 0
    negative_regressions: int = 0  # >0 blocks promotion by construction
    detail: str = ""


class PromoteSkillInput(BaseModel):
    """GOVERNED state transition of the promotion pipeline (WSC-E4-T3):
    candidate → (eval) → approved → canary → active, and the rollback
    active/canary → rolled_back. P1/P3: no transition to `approved`/`active`
    without a resolved human `approver` — promotion without approval is
    impossible by construction (the Activity refuses `to_status in
    {approved,active}` with an empty approver)."""

    tenant_id: str
    skill_key: str
    version: int
    to_status: str      # 'approved' | 'canary' | 'active' | 'rolled_back'
    approver: str | None = None  # resolved principal; required for approved/active
    reason: str = ""


class PromoteSkillResult(BaseModel):
    skill_key: str
    version: int
    from_status: str
    to_status: str
    ok: bool
    detail: str = ""
