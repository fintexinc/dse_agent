"""WS-E's Temporal Activities, registered under the names from
`dse_contracts.activities` (ACTIVITY_RUN_L1_PIPELINE, ACTIVITY_FINALIZE_PR,
ACTIVITY_CONSUME_CI_STATUS) so that WS-B's single Worker
(`services/orchestrator/worker.py`) imports and registers them.

Every `@activity.defn` here is just a thin wrapper: it assembles the real objects
(executor from the `SandboxHandle`, `GitHubClient` from the env vars) and calls
the corresponding module's testable core function. This workstream's tests call
the core functions directly with injected fakes — they never need the Temporal
runtime or real Docker to validate the LOGIC (but the review_signal test does run
against the real Temporal, see README).

Defensive import: if `temporalio` is not installed in the environment importing
this module, the rest of `dse_validation` remains usable (the pure-logic tests do
not depend on the `@activity.defn` decorator).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from dse_contracts import (
    ACTIVITY_CONSUME_CI_STATUS,
    ACTIVITY_FINALIZE_PR,
    ACTIVITY_RUN_L1_PIPELINE,
    ACTIVITY_VERIFY_MERGE_STATE,
    CiStatusResult,
    L1Result,
    L2Verdict,
    MergeVerification,
    PlanArtifact,
    PrRef,
    VerifyMergeInput,
)
from dse_contracts.activities import (
    ACTIVITY_PUBLISH_ARTIFACT,
    ACTIVITY_RUN_DEMO_EVIDENCE,
    ACTIVITY_RUN_VISUAL_DIFF,
    ACTIVITY_TRIAGE_PREVIEW_FAILURE,
    ACTIVITY_TRIGGER_PREVIEW,
    ACTIVITY_UPDATE_BASE_BRANCH,
    ArtifactRef,
    DemoEvidenceResult,
    PreviewRef,
    PreviewTriageVerdict,
    PublishArtifactInput,
    RunDemoEvidenceInput,
    RunVisualDiffInput,
    TriagePreviewFailureInput,
    TriggerPreviewInput,
    UpdateBaseBranchInput,
    UpdateBaseBranchResult,
    VisualDiffResult,
)
from pydantic import BaseModel, Field

from dse_validation.config import GitHubConfig
from dse_validation.github.ci_status import consume_ci_status_core
from dse_validation.github.client import build_github_client
from dse_validation.github.pr_finalizer import adopt_pr_core, finalize_pr_core
from dse_validation.l1.pipeline import run_l1_pipeline_core
from dse_validation.l2 import fix_loop as _fix_loop
from dse_validation.l2.l2_review import run_l2_review
from dse_validation.l2.session import L2ReviewInput, L2ReviewSession, build_l2_session
from dse_validation.sandbox_exec import executor_for_handle

# Activity names WS-E owns in Phase 2. `ACTIVITY_RUN_L2_REVIEW` (dse_contracts)
# is the L2 SESSION, owned by WS-C — WS-E does NOT register it; WS-E registers the
# ORCHESTRATION around it (verdict/cost recording, fix-retry loop decision, PR
# adoption in strict mode). Distinct names so they do not collide in the single
# Worker.
WSE_ACTIVITY_RUN_L2_REVIEW = "wse_run_l2_review"  # orchestrates the session + records evidence
WSE_ACTIVITY_RECORD_FIX_LOOP = "wse_record_fix_loop"
WSE_ACTIVITY_ADOPT_PR = "wse_adopt_pr"

# Phase 3 — the 4 CONTRACT names (dse_contracts) belong to WS-E (owner declared in
# the contract itself): run_demo_evidence, publish_artifact, trigger_preview,
# run_visual_diff. The helpers below carry the wse_ prefix (non-contractual).
WSE_ACTIVITY_QUARANTINE_ARTIFACTS = "wse_quarantine_artifacts"
WSE_ACTIVITY_REAP_PREVIEWS = "wse_reap_previews"
WSE_ACTIVITY_SHOULD_REFRESH_EVIDENCE = "wse_should_refresh_evidence"
WSE_ACTIVITY_PUBLISH_EVIDENCE = "wse_publish_evidence"

# Phase 4 — ACTIVITY_UPDATE_BASE_BRANCH (dse_contracts) belongs to WS-E
# (merge-base, WSE-E6-T16). The review-feedback episode (WSE-E6-T18) is a helper
# (wse_ prefix, non-contractual — it only records the episode; promotion is WS-C's).
WSE_ACTIVITY_RECORD_REVIEW_EPISODE = "wse_record_review_episode"

try:
    from temporalio import activity

    _HAS_TEMPORAL = True
except ImportError:  # pragma: no cover
    _HAS_TEMPORAL = False


# ---------------------------------------------------------------------------
# Input models — Temporal Activities take a single pydantic argument (makes future
# versioning easier without breaking the positional signature).
#
# RunL1PipelineInput: uses the CANONICAL one from dse_contracts (found during the
# real run on 2026-07-22: a local shadow fell behind, missing work_item_id/base_sha
# — AttributeError in production while the contract tests passed against the
# canonical model). Never redefine contract models locally.
# ---------------------------------------------------------------------------
# As entradas que a Activity decodifica vêm do CONTRATO, nunca de cópia local.
# Cada cópia que existiu aqui ficou para trás e matou um item em produção com
# AttributeError e a suíte verde: RunL1PipelineInput em 2026-07-22
# (work_item_id/base_sha) e FinalizePrInput em 2026-08-11 (files_changed, uma
# PR perdida depois de passar L1 e L2). `test_activity_inputs_are_the_contract`
# é o que impede a terceira.
from dse_contracts.activities import (  # noqa: E402
    ConsumeCiStatusInput,
    FinalizePrInput,
    RunL1PipelineInput,
)


class RunL2ReviewInput(BaseModel):
    """WSE-E2-T4. P3: only plan+diff cross over — no Coder history."""

    work_item_id: str
    tenant_id: str
    plan: PlanArtifact
    diff: str
    iteration: int = 0
    l1_passed: bool = True  # cheapest-first guard (P5); the workflow passes L1Result.passed


class RecordFixLoopInput(BaseModel):
    """WSE-E2-T5 — mirrors the loop's durable counter maintained by the workflow
    (WS-B owns the state; this activity persists evidence + audits)."""

    work_item_id: str
    tenant_id: str
    action: str  # "retry_coder" | "escalate_operator"
    iterations: int
    coder_cost_usd: float = 0.0
    l2_cost_usd: float = 0.0
    reason: str = ""
    objections: list[str] = Field(default_factory=list)


class AdoptPrInput(BaseModel):
    """WSE-E3-T8 — a human opened the PR from the compare link; adopt it (same WI)."""

    work_item_id: str
    tenant_id: str
    repo: str
    branch: str
    pr_number: int | None = None
    pr_url: str | None = None


class QuarantineArtifactsInput(BaseModel):
    """WSE-E5-T12 — WS-F acceptance: a quarantined work item's artifact is moved to
    the quarantine prefix and access is invalidated before the TTL."""

    work_item_id: str
    tenant_id: str
    actor: str = "system:validation"


class ShouldRefreshEvidenceInput(BaseModel):
    """WSE-E5-T14 / ADR-26 — debounce decision contract consumed by WS-B's workflow
    (being built in parallel): regenerate evidence ONLY on an explicit human
    request or a commit that changes behavior. Returns:
    {"refresh": bool, "reason": str} — a 100% deterministic decision (P1)."""

    work_item_id: str
    tenant_id: str
    commit_sha: str
    files_changed: list[str] = Field(default_factory=list)
    human_requested: bool = False


class PublishEvidenceInput(BaseModel):
    """WSE-E5-T14 — consolidated publication (video/preview/diff/trace in a single
    tracking comment) with debounce built in."""

    work_item_id: str
    tenant_id: str
    commit_sha: str
    surface_ref: dict
    pr_number: int | None = None
    files_changed: list[str] = Field(default_factory=list)
    human_requested: bool = False


class RecordReviewEpisodeInput(BaseModel):
    """WSE-E6-T18 — records 1 skill-learning episode from ACCEPTED review feedback.
    NO skill is created/activated (only the episode; promotion is WS-C's)."""

    work_item_id: str
    tenant_id: str
    reviewer: str
    comment_body: str
    pr_number: int | None = None
    path: str | None = None
    diff_hunk: str | None = None
    accepted: bool = True


logger = logging.getLogger("dse_validation.activities")


class _L1Cancelled(Exception):
    """Raised inside the L1 thread at the first stage boundary after the Activity
    was cancelled — see `_run_l1_pipeline_with_heartbeat`."""


class _L1Progress:
    """Which L1 stage the worker thread is on, readable from the event loop.

    The pipeline thread writes and the Activity's loop reads. Both sides touch a
    SINGLE attribute holding the `(stage, started_at)` pair: rebinding one
    attribute is atomic under the GIL, so the loop can never publish a new stage
    name carrying the previous stage's clock. No lock, because the loop must
    never block on the thread it is heartbeating for.
    """

    def __init__(self, work_item_id: str) -> None:
        self._work_item_id = work_item_id
        self._started_at = time.monotonic()
        self._stage: tuple[str, float] = ("starting", self._started_at)
        self._cancelled = False

    def enter(self, stage: str) -> None:
        """`on_step` for the pipeline core — called from the worker thread."""
        if self._cancelled:
            raise _L1Cancelled(f"L1 activity cancelled before stage {stage}")
        self._stage = (stage, time.monotonic())

    def cancel(self) -> None:
        self._cancelled = True

    def current(self) -> tuple[str, float]:
        stage, since = self._stage
        return stage, max(0.0, time.monotonic() - since)

    def details(self, *, state: str, sequence: int) -> dict:
        """The heartbeat payload. Deliberately small and free of repository
        content: it is operational data, and on a timeout it is the ONLY thing
        the server hands back (`lastHeartbeatDetails`). Keeping the same shape as
        `sandbox_runtime.activity_heartbeat` so both workers read alike.
        `elapsed_seconds` is the age of `operation`; `total_elapsed_seconds` is
        the whole L1 — the pair is what sizes the per-command timeouts."""
        stage, stage_elapsed = self.current()
        return {
            "schema_version": 1,
            "component": "validation",
            "stage": "l1",
            "work_item_id": self._work_item_id,
            "operation": stage,
            "state": state,
            "sequence": sequence,
            "elapsed_seconds": round(stage_elapsed, 3),
            "total_elapsed_seconds": round(max(0.0, time.monotonic() - self._started_at), 3),
        }


def _run_l1_pipeline(
    inp: RunL1PipelineInput, on_step: Callable[[str], None] | None = None
) -> L1Result:
    # Boundary bug fixed during the real run (2026-07-22): the core moved to
    # base_sha/head_sha (sha-bound-validation-inputs-v1) and this wrapper kept
    # passing base_branch — the tests call the CORE directly and never saw the
    # boundary (test_l1_wrapper_matches_core_signature now pins this).
    executor = executor_for_handle(inp.sandbox, repo_dir=inp.repo_dir)
    # cfg=None → the core loads the repo's TRUSTED MANIFEST (.dse/validation.json
    # read from the immutable base_sha). Passing a default L1Config() here failed
    # EVERYTHING (manifest NOT_CONFIGURED) — found during the real run; the real L1
    # is the one from the manifest committed in the target repo.
    return run_l1_pipeline_core(
        executor=executor,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        plan=inp.plan,
        base_sha=inp.base_sha,
        head_sha=inp.head_sha,
        target_dir=inp.target_dir,
        on_step=on_step,
    )


def _finalize_pr(inp: FinalizePrInput) -> PrRef:
    from dse_validation.config import StrictModeConfig
    from dse_validation.db import PostgresCommentStateStore
    from dse_validation.github.comment_backend import GitHubCommentBackend

    try:
        from dse_contracts.mutable_comment import MutableCommentWriter
    except ImportError:  # pragma: no cover
        MutableCommentWriter = None

    github_client = build_github_client(GitHubConfig())
    executor = executor_for_handle(inp.sandbox, repo_dir=inp.repo_dir) if inp.sandbox else None
    if executor is None:
        raise ValueError("finalize_pr requires a valid SandboxHandle to run `git push`")

    strict = inp.strict_mode
    if strict is None:
        strict = StrictModeConfig().is_strict_for(inp.tenant_id, inp.repo)

    comment_writer = None
    if strict and inp.surface_ref is not None and MutableCommentWriter is not None:
        comment_writer = MutableCommentWriter(
            GitHubCommentBackend(github_client), PostgresCommentStateStore(), surface="github_pr"
        )

    return finalize_pr_core(
        executor=executor,
        github_client=github_client,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        branch=inp.branch,
        base_branch=inp.base_branch,
        summary=inp.summary,
        risk_class=inp.risk_class,
        evidence_url=inp.evidence_url,
        issue_ref=inp.issue_ref,
        strict_mode=strict,
        comment_writer=comment_writer,
        surface_ref=inp.surface_ref,
        files_changed=list(inp.files_changed or []),
    )


def _verify_merge_state(inp: VerifyMergeInput, github_client=None) -> MergeVerification:
    """plan 08 §F (F1) — confirms via the GitHub API that the PR is REALLY merged
    (and, if given, with the expected head_sha). Fail-safe: any error/doubt =>
    verified=False (the workflow never concludes as done based on that). The
    `github_client` is injectable for tests; in production it comes from env vars."""
    client = github_client or build_github_client(GitHubConfig())
    try:
        pr = client.get_pull_request(inp.repo, inp.pr_number)
    except Exception as exc:  # network/credential: fail-safe (not verified)
        return MergeVerification(verified=False, reason=f"api_error:{type(exc).__name__}")
    if pr is None:
        return MergeVerification(exists=False, verified=False, reason="pr_not_found")
    merged = bool(pr.get("merged"))
    head_sha = pr.get("head_sha")
    merged_by = pr.get("merged_by")
    if not merged:
        return MergeVerification(
            exists=True, merged=False, head_sha=head_sha, merged_by=merged_by,
            verified=False, reason=f"not_merged(state={pr.get('state')})",
        )
    if inp.expected_head_sha and head_sha and inp.expected_head_sha != head_sha:
        return MergeVerification(
            exists=True, merged=True, head_sha=head_sha, merged_by=merged_by,
            merge_commit_sha=pr.get("merge_commit_sha"),
            verified=False, reason="head_sha_mismatch",
        )
    return MergeVerification(
        exists=True, merged=True, head_sha=head_sha, merged_by=merged_by,
        merge_commit_sha=pr.get("merge_commit_sha"), verified=True, reason="ok",
    )


def _run_l2_review(inp: RunL2ReviewInput, session: L2ReviewSession | None = None) -> L2Verdict:
    # P5 cheapest-first: L2 only runs after L1 is green. The workflow passes
    # `l1_passed`; if false, fail cleanly at the boundary (P6) instead of spending L2.
    if not inp.l1_passed:
        raise ValueError(
            f"L2 cannot run before L1 is green (cheapest-first/P5) for {inp.work_item_id}"
        )
    session = session or build_l2_session()
    review_input = L2ReviewInput(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        plan=inp.plan,
        diff=inp.diff,  # P3: plan+diff only; L2ReviewInput has no Coder-history field
        iteration=inp.iteration,
    )
    return run_l2_review(
        session,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        inp=review_input,
        iteration=inp.iteration,
    )


def _record_fix_loop(inp: RecordFixLoopInput) -> dict:
    state = _fix_loop.FixLoopState(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        iterations=max(0, inp.iterations - 1),  # state BEFORE this iteration
    )
    if inp.action == "retry_coder":
        new_state = _fix_loop.register_retry(
            state, coder_cost_usd=inp.coder_cost_usd, l2_cost_usd=inp.l2_cost_usd
        )
    elif inp.action == "escalate_operator":
        new_state = _fix_loop.escalate_to_operator(
            state.model_copy(update={"iterations": inp.iterations}),
            reason=inp.reason,
            objections=inp.objections,
        )
    else:  # pragma: no cover - guard
        raise ValueError(f"unknown fix-loop action: {inp.action}")
    return new_state.model_dump()


def _adopt_pr(inp: AdoptPrInput) -> PrRef | None:
    github_client = build_github_client(GitHubConfig())
    return adopt_pr_core(
        github_client=github_client,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        branch=inp.branch,
        pr_number=inp.pr_number,
        pr_url=inp.pr_url,
    )


def _resolve_ci_input_gaps(inp: ConsumeCiStatusInput) -> ConsumeCiStatusInput:
    """Fills in missing tenant_id/repo/ref (old payload in Temporal's history — see
    the model's docstring) from work_items + wse_pr_tracking. Deterministic; a
    no-op when the payload already arrived complete."""
    if inp.tenant_id and inp.repo and inp.ref:
        return inp
    from dse_validation.db import get_connection
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT wi.tenant_id, t.repo, t.branch
            FROM work_items wi
            LEFT JOIN wse_pr_tracking t ON t.work_item_id = wi.id
            WHERE wi.id = %s
            ORDER BY t.created_at DESC NULLS LAST LIMIT 1
            """,
            (inp.work_item_id,),
        )
        row = cur.fetchone()
    if row is None:
        return inp
    tenant_id, repo, branch = row
    return inp.model_copy(update={
        "tenant_id": inp.tenant_id or (tenant_id or ""),
        "repo": inp.repo or (repo or ""),
        "ref": inp.ref or (branch or ""),
    })


def _consume_ci_status(inp: ConsumeCiStatusInput) -> CiStatusResult:
    inp = _resolve_ci_input_gaps(inp)
    github_client = build_github_client(GitHubConfig())
    if inp.surface_ref is None:
        # Phase 1/2 behavior unchanged (poll + aggregation + persistence)
        return consume_ci_status_core(
            github_client=github_client,
            work_item_id=inp.work_item_id,
            tenant_id=inp.tenant_id,
            repo=inp.repo,
            pr_number=inp.pr_number,
            ref=inp.ref,
        )
    # Phase 3 (WSE-E4-T9b): full L3 — reflection in the tracking comment +
    # targeted re-runs on a fix commit + CI-repair episodes.
    from dse_contracts.mutable_comment import MutableCommentWriter

    from dse_validation.db import PostgresCommentStateStore
    from dse_validation.github.comment_backend import GitHubCommentBackend
    from dse_validation.github.l3 import consume_ci_status_l3

    writer = MutableCommentWriter(
        GitHubCommentBackend(github_client), PostgresCommentStateStore(), surface="github_pr_ci"
    )
    return consume_ci_status_l3(
        github_client=github_client,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        pr_number=inp.pr_number,
        ref=inp.ref,
        comment_writer=writer,
        surface_ref=inp.surface_ref,
    )


# ---------------------------------------------------------------------------
# Phase 4 — merge-base (WSE-E6-T16) and review-feedback episode (WSE-E6-T18)
# ---------------------------------------------------------------------------
def _update_base_branch(inp: UpdateBaseBranchInput) -> UpdateBaseBranchResult:
    """Activity wrapper: resolves the git workspace and the anchored review threads
    (via the tracked PR + GitHub client) and calls the deterministic core. Like
    LocalFakeSandbox for L1, the TESTS call `update_base_branch_core` directly with
    a real local bare repo — this wrapper is the integration seam with the real
    WS-C (sandbox workspace) + GitHub App.

    O workspace é PROVISIONADO sob demanda quando não existe (o caso de
    produção no driver k8s — medido no wi_a8b760de, 2026-08-12: nenhum dos
    caminhos de `locations()` existe no pod do orchestrator e a activity
    retentou FileNotFoundError por horas). Provisionado aqui = descartado aqui:
    o /tmp do pod é um emptyDir de 256Mi partilhado por todas as chamadas."""
    import shutil as _shutil

    from dse_validation.github.client import build_github_client
    from dse_validation.merge_base import ensure_workspace, update_base_branch_core

    ws = ensure_workspace(
        work_item_id=inp.work_item_id, repo=inp.repo, branch=inp.branch,
    )

    # review threads anchored to commits — resolved via the tracked PR.
    anchored: list[str] = []
    # fix (observed live in the review loop): `db` was never imported in this
    # module — NameError on every real update_base_branch. Local import, in the
    # style of the others in this file.
    from dse_validation import db as _db
    tracked = _db.get_tracked_pr(inp.work_item_id)
    pr_number = tracked.get("pr_number") if tracked else None
    if pr_number is not None:
        github_client = build_github_client(GitHubConfig())
        for t in github_client.list_review_threads(inp.repo, int(pr_number)):
            sha = t.get("original_commit_id") or t.get("commit_id")
            if sha:
                anchored.append(sha)

    try:
        return update_base_branch_core(
            work_item_id=inp.work_item_id,
            tenant_id=inp.tenant_id,
            repo=inp.repo,
            branch=inp.branch,
            base_branch=inp.base_branch,
            workspace_dir=ws.workspace_dir,
            first_human_review_done=inp.first_human_review_done,
            anchored_review_shas=anchored,
            remote_url=ws.remote_url,
        )
    finally:
        # Só o que ESTA chamada criou — nunca o workspace do sandbox, e nunca
        # o diretório-pai (em dev ele guarda o origin.git dos testes).
        if ws.provisioned:
            _shutil.rmtree(ws.workspace_dir, ignore_errors=True)


def _triage_preview_failure(inp: TriagePreviewFailureInput) -> PreviewTriageVerdict:
    """Preview degradado → o agente decide se mudança de código conserta
    (conteúdo); o workflow decide o que fazer com o veredito (política)."""
    from dse_validation.preview.triage import triage_preview_failure_core

    return triage_preview_failure_core(inp)


def _resolve_preview_deep_link(payload: dict) -> dict:
    """rc.103 — o LLM decide o caminho fundo do preview; o portão determinístico
    e o fail-open vivem em `preview.deep_link` (o modelo nunca compõe URL)."""
    from dse_validation.config import GitHubConfig
    from dse_validation.github.client import build_github_client
    from dse_validation.preview.deep_link import build_completer, resolve_deep_link

    return resolve_deep_link(
        build_github_client(GitHubConfig()),
        repo=payload["repo"],
        pr_number=int(payload["pr_number"]),
        instruction=str(payload.get("instruction") or ""),
        files_changed=list(payload.get("files_changed") or []),
        kind=str(payload.get("kind") or "unknown"),
        complete=build_completer(
            str(payload.get("tenant_id") or "unknown"),
            str(payload.get("work_item_id") or "unknown"),
        ),
    )


def _record_review_episode(inp: RecordReviewEpisodeInput) -> dict | None:
    from dse_validation.review_learning import record_review_feedback_episode

    return record_review_feedback_episode(
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        pr_number=inp.pr_number,
        reviewer=inp.reviewer,
        comment_body=inp.comment_body,
        path=inp.path,
        diff_hunk=inp.diff_hunk,
        accepted=inp.accepted,
    )


# ---------------------------------------------------------------------------
# Phase 3 — cores of the evidence Activities (contract)
# ---------------------------------------------------------------------------
def _publish_artifact(inp: PublishArtifactInput) -> ArtifactRef:
    from dse_validation.evidence.garage import publish_artifact_core

    return publish_artifact_core(inp)


def _run_demo_evidence(inp: RunDemoEvidenceInput) -> DemoEvidenceResult:
    from dse_validation.evidence.demo import run_demo_evidence_core

    return run_demo_evidence_core(inp)


def _trigger_preview(inp: TriggerPreviewInput) -> PreviewRef:
    from dse_validation.preview.argocd import trigger_preview_core

    return trigger_preview_core(inp)


def _run_visual_diff(inp: RunVisualDiffInput) -> VisualDiffResult:
    from dse_validation.evidence.visual_diff import run_visual_diff_core

    return run_visual_diff_core(inp)


def _quarantine_artifacts(inp: QuarantineArtifactsInput) -> list[str]:
    from dse_validation.evidence.garage import quarantine_artifacts_for_work_item

    return quarantine_artifacts_for_work_item(inp.work_item_id, actor=inp.actor)


def _reap_previews() -> list[str]:
    from dse_validation.preview.argocd import reap_expired_previews

    return reap_expired_previews()


def _should_refresh_evidence(inp: ShouldRefreshEvidenceInput) -> dict:
    from dse_validation.evidence.publication import should_refresh_evidence

    decision = should_refresh_evidence(
        work_item_id=inp.work_item_id,
        commit_sha=inp.commit_sha,
        files_changed=inp.files_changed or None,
        human_requested=inp.human_requested,
    )
    return {"refresh": decision.refresh, "reason": decision.reason}


def _publish_evidence(inp: PublishEvidenceInput) -> dict:
    from dse_contracts.mutable_comment import MutableCommentWriter

    from dse_validation.db import PostgresCommentStateStore
    from dse_validation.evidence.publication import publish_evidence_bundle
    from dse_validation.github.comment_backend import GitHubCommentBackend

    github_client = build_github_client(GitHubConfig())
    writer = MutableCommentWriter(
        GitHubCommentBackend(github_client), PostgresCommentStateStore(), surface="github_pr_evidence"
    )
    return publish_evidence_bundle(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        commit_sha=inp.commit_sha,
        comment_writer=writer,
        surface_ref=inp.surface_ref,
        pr_number=inp.pr_number,
        files_changed=inp.files_changed or None,
        human_requested=inp.human_requested,
    )


# RULE (found during the real run 2026-07-23, wi_150d): every wrapper below is
# `async def` in a worker with ONE event loop shared with the sandbox-runtime
# activities. A sync impl called DIRECTLY on the loop (L1 = npm/subprocess,
# L2 = chat_completion, visual diff = Playwright…) blocks the loop for minutes and
# the heartbeats of ALL activities stop → the server cancels coder/tester
# mid-work and the retry pays for the model again. Hence: ALWAYS
# `await asyncio.to_thread(_impl, inp)` — never `_impl(inp)` directly.
if _HAS_TEMPORAL:

    # L1 is the longest synchronous body in this worker: it runs the target
    # repository's OWN suite inside the sandbox. `await asyncio.to_thread(...)`
    # keeps the loop free but emits nothing, so the server saw no heartbeat and
    # killed the Activity at `activity_heartbeat_seconds` — 600 s on the
    # dispatcher path, 60 s for any caller that goes through `apply_to_input`
    # without DSE_ACTIVITY_HEARTBEAT_SECONDS in the environment — with
    # ACTIVITY_TASK_TIMED_OUT. The `test` finding was then neither PASS nor
    # FAIL, it simply never existed. The loop beats while the thread works, which
    # is the shape sandbox-runtime already uses for the agent turns
    # (`sandbox_runtime.activity_heartbeat.run_sync_with_heartbeat`).
    #
    # That helper is NOT imported here on purpose, for two reasons: its heartbeat
    # details are frozen at call time (they could not name the stage that is
    # running), and `dse-validation` does not depend on `sandbox-runtime` — the
    # import resolves today only because both packages happen to live in the same
    # image, and would break the moment the images are split.
    _HEARTBEAT_INTERVAL_CAP_SECONDS = 20.0
    _MIN_HEARTBEAT_INTERVAL_SECONDS = 0.01

    def _l1_heartbeat_interval() -> float:
        """Beat at a third of the heartbeat timeout, capped at 20 s.

        Chosen against the timeout, not in the absolute: a third leaves two
        missed beats of slack before the server gives up, and it follows
        automatically when the timeout is retuned. The cap is what keeps the
        payload FRESH — the details are a black box recorder, and at 600 s of
        timeout a third would be 200 s, long enough to report `typecheck` for a
        pipeline that moved on to `test` three minutes ago. Beating more often
        is free: the SDK coalesces heartbeats and only ever ships the last
        details (at most one call per `max_heartbeat_throttle_interval`, 60 s by
        default), and heartbeats are not workflow-history events.
        """
        timeout = activity.info().heartbeat_timeout
        if timeout is None or timeout.total_seconds() <= 0:
            return _HEARTBEAT_INTERVAL_CAP_SECONDS
        return max(
            _MIN_HEARTBEAT_INTERVAL_SECONDS,
            min(_HEARTBEAT_INTERVAL_CAP_SECONDS, timeout.total_seconds() / 3.0),
        )

    def _drain_abandoned(task: asyncio.Task) -> None:
        """A cancelled Activity leaves the L1 task running. Retrieve its outcome
        so asyncio does not log `exception was never retrieved` when it is
        finally collected."""
        if not task.cancelled():
            task.exception()

    async def _run_l1_pipeline_with_heartbeat(inp: RunL1PipelineInput) -> L1Result:
        progress = _L1Progress(inp.work_item_id)
        interval = _l1_heartbeat_interval()
        sequence = 0
        activity.heartbeat(progress.details(state="started", sequence=sequence))
        call = asyncio.create_task(asyncio.to_thread(_run_l1_pipeline, inp, progress.enter))
        call.add_done_callback(_drain_abandoned)
        try:
            while True:
                done, _pending = await asyncio.wait({call}, timeout=interval)
                if call in done:
                    break
                sequence += 1
                activity.heartbeat(progress.details(state="running", sequence=sequence))
            result = await call
        except asyncio.CancelledError:
            # The thread survives the cancellation — `asyncio.to_thread` has no
            # way to interrupt it, and neither has `subprocess.run` inside the
            # executor. Asking the pipeline to stop at its next stage boundary
            # bounds the waste to the command already in flight instead of the
            # whole remaining pipeline, and skips the validation_runs/audit rows
            # of a run nobody will read.
            stage, stage_elapsed = progress.current()
            progress.cancel()
            logger.warning(
                "L1 activity cancelled for %s during stage=%s after %.1fs; the "
                "in-flight sandbox command keeps running until it returns",
                inp.work_item_id, stage, stage_elapsed,
            )
            raise
        activity.heartbeat(progress.details(state="completed", sequence=sequence + 1))
        return result

    @activity.defn(name=ACTIVITY_RUN_L1_PIPELINE)
    async def run_l1_pipeline(inp: RunL1PipelineInput) -> L1Result:
        return await _run_l1_pipeline_with_heartbeat(inp)

    @activity.defn(name=ACTIVITY_FINALIZE_PR)
    async def finalize_pr(inp: FinalizePrInput) -> PrRef:
        return await asyncio.to_thread(_finalize_pr, inp)

    @activity.defn(name=ACTIVITY_VERIFY_MERGE_STATE)
    async def verify_merge_state(inp: VerifyMergeInput) -> MergeVerification:
        return await asyncio.to_thread(_verify_merge_state, inp)

    @activity.defn(name=ACTIVITY_CONSUME_CI_STATUS)
    async def consume_ci_status(inp: ConsumeCiStatusInput) -> CiStatusResult:
        return await asyncio.to_thread(_consume_ci_status, inp)

    @activity.defn(name=WSE_ACTIVITY_RUN_L2_REVIEW)
    async def wse_run_l2_review(inp: RunL2ReviewInput) -> L2Verdict:
        # rc.104 — DESARMAR ANTES DE LIGAR. Esta activity roda sob
        # `RetryPolicy(maximum_attempts=0)` (`_run_model_activity`), cuja
        # política é: falha permanente é levantada `non_retryable` NA FONTE;
        # o resto retenta sob o teto de relógio de ~2h. Uma resposta de modelo
        # que não parseia é permanente por construção — `temperature=0`, mesmo
        # prompt, mesma saída — então sem esta tradução ela retentaria por duas
        # horas, faturando cada tentativa fora do orçamento do item.
        #
        # O custo que JÁ foi faturado viaja no erro e vai no primeiro detail,
        # que é o que o workflow lê para cobrar.
        from temporalio.exceptions import ApplicationError

        from dse_validation.model_json import ModelJsonError

        try:
            return await asyncio.to_thread(_run_l2_review, inp)
        except ModelJsonError as exc:
            raise ApplicationError(
                f"l2_answer_never_parsed: {exc}",
                {"cost_usd": exc.cost_usd},
                type="ModelAnswerUnparseable",
                non_retryable=True,
            ) from exc

    @activity.defn(name=WSE_ACTIVITY_RECORD_FIX_LOOP)
    async def wse_record_fix_loop(inp: RecordFixLoopInput) -> dict:
        return await asyncio.to_thread(_record_fix_loop, inp)

    @activity.defn(name=WSE_ACTIVITY_ADOPT_PR)
    async def wse_adopt_pr(inp: AdoptPrInput) -> PrRef | None:
        return await asyncio.to_thread(_adopt_pr, inp)

    # --- Phase 3: CONTRACT evidence Activities (owner: WS-E) ---
    @activity.defn(name=ACTIVITY_PUBLISH_ARTIFACT)
    async def publish_artifact(inp: PublishArtifactInput) -> ArtifactRef:
        return await asyncio.to_thread(_publish_artifact, inp)

    @activity.defn(name=ACTIVITY_RUN_DEMO_EVIDENCE)
    async def run_demo_evidence(inp: RunDemoEvidenceInput) -> DemoEvidenceResult:
        return await asyncio.to_thread(_run_demo_evidence, inp)

    @activity.defn(name=ACTIVITY_TRIGGER_PREVIEW)
    async def trigger_preview(inp: TriggerPreviewInput) -> PreviewRef:
        # Batimento enxuto (padrão do update_base_branch): a espera interna
        # chega a 900s e o call site agora declara heartbeat_timeout=120s —
        # sem bater, a activity morre no meio da espera; batendo, o prazo
        # start_to_close pode ter folga sem perder a detecção de worker morto.
        call = asyncio.create_task(asyncio.to_thread(_trigger_preview, inp))
        call.add_done_callback(_drain_abandoned)
        while True:
            done, _pending = await asyncio.wait({call}, timeout=15.0)
            if call in done:
                return await call
            activity.heartbeat({"work_item_id": inp.work_item_id, "state": "waiting"})

    @activity.defn(name=ACTIVITY_RUN_VISUAL_DIFF)
    async def run_visual_diff(inp: RunVisualDiffInput) -> VisualDiffResult:
        return await asyncio.to_thread(_run_visual_diff, inp)

    # --- Phase 3: helpers (non-contractual, wse_ prefix) ---
    @activity.defn(name=WSE_ACTIVITY_QUARANTINE_ARTIFACTS)
    async def wse_quarantine_artifacts(inp: QuarantineArtifactsInput) -> list[str]:
        return await asyncio.to_thread(_quarantine_artifacts, inp)

    @activity.defn(name=WSE_ACTIVITY_REAP_PREVIEWS)
    async def wse_reap_previews() -> list[str]:
        return await asyncio.to_thread(_reap_previews)

    @activity.defn(name=WSE_ACTIVITY_SHOULD_REFRESH_EVIDENCE)
    async def wse_should_refresh_evidence(inp: ShouldRefreshEvidenceInput) -> dict:
        return await asyncio.to_thread(_should_refresh_evidence, inp)

    @activity.defn(name=WSE_ACTIVITY_PUBLISH_EVIDENCE)
    async def wse_publish_evidence(inp: PublishEvidenceInput) -> dict:
        return await asyncio.to_thread(_publish_evidence, inp)

    # --- Phase 4: merge-base (contract) + review-feedback episode (helper) ---
    @activity.defn(name=ACTIVITY_UPDATE_BASE_BRANCH)
    async def update_base_branch(inp: UpdateBaseBranchInput) -> UpdateBaseBranchResult:
        # Batimento enxuto no padrão do L1: o call site declara
        # heartbeat_timeout=600s e ninguém batia — o provisionamento on-demand
        # (fetch de repo real, até 300s) somado ao merge/push pode passar do
        # prazo, e a activity morreria MUDA no meio do trabalho.
        call = asyncio.create_task(asyncio.to_thread(_update_base_branch, inp))
        call.add_done_callback(_drain_abandoned)
        while True:
            done, _pending = await asyncio.wait({call}, timeout=15.0)
            if call in done:
                return await call
            activity.heartbeat({"work_item_id": inp.work_item_id, "state": "running"})

    @activity.defn(name=WSE_ACTIVITY_RECORD_REVIEW_EPISODE)
    async def wse_record_review_episode(inp: RecordReviewEpisodeInput) -> dict | None:
        return await asyncio.to_thread(_record_review_episode, inp)

    @activity.defn(name=ACTIVITY_TRIAGE_PREVIEW_FAILURE)
    async def triage_preview_failure(inp: TriagePreviewFailureInput) -> PreviewTriageVerdict:
        return await asyncio.to_thread(_triage_preview_failure, inp)

    @activity.defn(name="resolve_preview_deep_link")
    async def resolve_preview_deep_link(payload: dict) -> dict:
        return await asyncio.to_thread(_resolve_preview_deep_link, payload)

    ALL_ACTIVITIES = [
        run_l1_pipeline,
        finalize_pr,
        verify_merge_state,
        consume_ci_status,
        wse_run_l2_review,
        wse_record_fix_loop,
        wse_adopt_pr,
        # Phase 3
        publish_artifact,
        run_demo_evidence,
        trigger_preview,
        run_visual_diff,
        wse_quarantine_artifacts,
        wse_reap_previews,
        wse_should_refresh_evidence,
        wse_publish_evidence,
        # Phase 4
        update_base_branch,
        wse_record_review_episode,
        # Preview autofix (2026-08-12)
        triage_preview_failure,
        # Deep link do preview (rc.103) — fora desta lista o worker não
        # registra (a lição e28f955, pinada em teste).
        resolve_preview_deep_link,
    ]
else:  # pragma: no cover
    ALL_ACTIVITIES = []

# Alias expected by the single worker's defensive loader (services/orchestrator/
# src/dse_orchestrator/worker.py:_load_cross_workstream_activities), which looks
# for `ACTIVITIES` (not `ALL_ACTIVITIES`) in this module.
ACTIVITIES = ALL_ACTIVITIES
