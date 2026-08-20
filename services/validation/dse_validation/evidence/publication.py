"""WSE-E5-T14 — CONSOLIDATED and DEBOUNCED evidence publication (ADR-26).

A single tracking comment per PR (the foundation's own `MutableCommentWriter`,
edited in place — never comment-per-update) concentrates ALL evidence links:
@demo video, Playwright trace, preview environment, visual diff. The body is
RENDERED FROM DATABASE STATE (wse_artifacts / wse_previews / wse_ci_status /
wse_pr_tracking) — crash-consistent: any re-render converges to the same content.

DEBOUNCE (ADR-26): regenerating evidence is expensive (Playwright + preview +
diff). `should_refresh_evidence()` is the 100% DETERMINISTIC decision (P1) that
WS-B's workflow consults BEFORE re-running the evidence pipeline:
  - explicit human request            => refresh (always; audited);
  - no previous publication           => refresh (first evidence);
  - same commit already published     => do NOT refresh (no-op);
  - new commit touching only files that do not change behavior (docs/markdown,
    configurable default)             => do NOT refresh;
  - new commit with a behavior change => refresh.
The workflow side (the Activity call) belongs to WS-B and is being built in
parallel — the decision contract exposed here is `wse_should_refresh_evidence`
(input/output documented in activities.py).
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from dse_contracts.paths import file_matches_glob

from dse_validation import db
from dse_validation.evidence.garage import resolve_artifact_url

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.evidence.publication")

# files that do NOT change behavior (do not invalidate evidence) — ADR-26.
_DEFAULT_NON_BEHAVIOR_GLOBS = ["*.md", "docs/**", "**/*.md", "LICENSE", ".github/CODEOWNERS"]


def non_behavior_globs() -> list[str]:
    raw = os.environ.get("DSE_EVIDENCE_NON_BEHAVIOR_GLOBS", "")
    if raw.strip():
        return [g.strip() for g in raw.split(",") if g.strip()]
    return list(_DEFAULT_NON_BEHAVIOR_GLOBS)


def _matches_any(path: str, globs: list[str]) -> bool:
    """Alias do primitivo único (`dse_contracts.paths.file_matches_glob`)."""
    return any(file_matches_glob(path, g) for g in globs)


@dataclass(frozen=True)
class RefreshDecision:
    refresh: bool
    reason: str


def should_refresh_evidence(
    *,
    work_item_id: str,
    commit_sha: str,
    files_changed: list[str] | None = None,
    human_requested: bool = False,
) -> RefreshDecision:
    """Deterministic debounce decision (ADR-26). See the module docstring."""
    if human_requested:
        return RefreshDecision(True, "explicit human request")
    previous = db.get_evidence_publication(work_item_id)
    if previous is None:
        return RefreshDecision(True, "first evidence publication")
    if previous["last_commit_sha"] == commit_sha:
        return RefreshDecision(False, "same commit as the last publication (debounce ADR-26)")
    if files_changed is not None and files_changed and all(
        _matches_any(f, non_behavior_globs()) for f in files_changed
    ):
        return RefreshDecision(
            False, "new commit only touches non-behavior files (docs) — debounce ADR-26"
        )
    return RefreshDecision(True, "new commit with a behavior change")


# ---------------------------------------------------------------------------
# Evidence block render + consolidated tracking comment
# ---------------------------------------------------------------------------
_EVIDENCE_HEADER = "### Fintex DSE — task evidence"

_KIND_LABEL = {
    "demo_video": "🎬 @demo video",
    "playwright_trace": "🧭 Playwright trace",
    "visual_diff": "🎨 Visual diff",
    "visual_baseline": "🖼️ Visual baseline",
    "test_report": "📄 Test report",
}


def render_evidence_section(work_item_id: str, *, accessor: str = "system:validation",
                            pr_number: int | None = None) -> str:
    """Builds the evidence section from DATABASE STATE. Each presigned link is
    RESOLVED on the spot (fresh) and the access is logged
    (`via='tracking_comment'`) — input to the evidence-consumption metric."""
    lines: list[str] = [_EVIDENCE_HEADER, ""]

    artifacts = db.list_artifacts(work_item_id)
    for row in artifacts:
        if row["quarantined_at"] is not None:
            lines.append(f"- {_KIND_LABEL.get(row['kind'], row['kind'])}: **quarantined** (access revoked)")
            continue
        try:
            url = resolve_artifact_url(
                work_item_id=work_item_id, store_key=row["store_key"],
                accessor=accessor, pr_number=pr_number, via="tracking_comment",
            )
            expires = row["expires_at"].isoformat() if row["expires_at"] else "?"
            lines.append(
                f"- {_KIND_LABEL.get(row['kind'], row['kind'])}: [{row['store_key']}]({url}) "
                f"(expires {expires})"
            )
        except PermissionError as exc:
            lines.append(f"- {_KIND_LABEL.get(row['kind'], row['kind'])}: unavailable ({exc})")

    preview = db.get_preview(work_item_id)
    if preview is not None:
        if preview["status"] == "created":
            # rc.103 — o link cai NA mudança: url + deep_path compostos só na
            # apresentação (a url crua segue sendo o baseURL do demo).
            alvo = f"{preview['url']}{preview.get('deep_path') or ''}"
            nota = f" — {preview['deep_note']}" if preview.get("deep_note") else ""
            lines.append(f"- 🌐 Preview: {alvo}{nota} (namespace `{preview['namespace']}`, "
                         f"expires {preview['expires_at'].isoformat() if preview['expires_at'] else '?'})")
        elif preview["status"] == "skipped_backend_only":
            lines.append("- 🌐 Preview: skipped (backend-only PR, FR-20)")
        elif preview["status"] == "degraded":
            lines.append(f"- 🌐 Preview: degraded — {preview['detail'][:200]} (does not block the PR)")
        elif preview["status"] == "reaped":
            lines.append("- 🌐 Preview: expired (TTL)")

    ci = db.get_ci_status(work_item_id)
    if ci is not None:
        emoji = {"green": "🟢", "red": "🔴", "pending": "🟡"}.get(ci["status"], "⚪")
        lines.append(f"- {emoji} CI (L3): **{ci['status']}** (PR #{ci['pr_number']})")

    if len(lines) == 2:
        lines.append("_(no evidence published yet)_")
    return "\n".join(lines)


def publish_evidence_bundle(
    *,
    work_item_id: str,
    tenant_id: str,
    commit_sha: str,
    comment_writer,
    surface_ref: dict,
    pr_number: int | None = None,
    files_changed: list[str] | None = None,
    human_requested: bool = False,
    actor: str = "system:validation",
) -> dict:
    """Consolidated publication + debounce. Returns
    {published: bool, reason: str, fingerprint: str|None}."""
    decision = should_refresh_evidence(
        work_item_id=work_item_id, commit_sha=commit_sha,
        files_changed=files_changed, human_requested=human_requested,
    )
    if not decision.refresh:
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="evidence_refresh_debounced", tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"commit_sha": commit_sha, "reason": decision.reason},
            )
        return {"published": False, "reason": decision.reason, "fingerprint": None}

    body = render_evidence_section(work_item_id, accessor=actor, pr_number=pr_number)
    fingerprint = hashlib.sha256(body.encode()).hexdigest()[:16]
    comment_writer.upsert(work_item_id, surface_ref, body)
    db.upsert_evidence_publication(work_item_id, tenant_id, commit_sha, fingerprint)
    if audit_emit is not None:
        audit_emit(
            actor=actor, action="evidence_published", tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"commit_sha": commit_sha, "reason": decision.reason,
                     "fingerprint": fingerprint, "pr_number": pr_number},
        )
    return {"published": True, "reason": decision.reason, "fingerprint": fingerprint}
