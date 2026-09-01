"""What the CI said, for the one fix cycle a human asks for (rc.130).

Measured on wi_f1f27266 (2026-08-31): eight paid fix rounds (~US$ 19) whose
whole instruction was "ci red: fix the pipeline", every one changing no file.
The three red checks carried exact messages ("Contract or generated client is
stale. Run 'npm run gen:contract'…", "Configuration key AUTH_ISSUER does not
exist") — none of them crossed into the turn.

Best-effort by construction. The job log needs `actions:read` on the App
(optional); without it the check run's annotations (which the App already
reads); without those, name/conclusion/url only. `source` says which one was
fetched, so the instruction never implies more than it has. Bounded per check:
80 lines / 4 000 chars of the END of the log — the end is where a runner
prints its verdict.
"""
from __future__ import annotations

import logging

from dse_contracts import CiCheckEvidence, CiFailureEvidence

from .ci_status import _TERMINAL_FAILURE_CONCLUSIONS

logger = logging.getLogger("dse_validation.github.ci_evidence")

_MAX_LINES = 80
_MAX_CHARS = 4_000


def _bounded_tail(text: str) -> str:
    lines = (text or "").splitlines()[-_MAX_LINES:]
    tail = "\n".join(lines)
    return tail[-_MAX_CHARS:]


def fetch_ci_failure_evidence_core(
    *, github_client, work_item_id: str, repo: str, ref: str,
) -> CiFailureEvidence:
    """One `CiCheckEvidence` per red check run at `ref`, in the CI's order."""
    checks: list[CiCheckEvidence] = []
    for run in github_client.list_check_runs(repo, ref):
        if run.get("status") != "completed" or run.get("conclusion") not in _TERMINAL_FAILURE_CONCLUSIONS:
            continue
        name = str(run.get("name") or "?")
        url = run.get("html_url") or run.get("details_url")
        tail, source = "", "none"
        run_id = run.get("id")
        if run_id is not None:
            log = _safe(github_client.get_job_log_tail, repo, int(run_id), max_lines=_MAX_LINES)
            if log:
                tail, source = _bounded_tail(log), "job_log"
            else:
                notas = _safe(github_client.list_check_run_annotations, repo, int(run_id)) or []
                texto = "\n".join(
                    f"{a.get('path') or ''}: {a.get('message') or ''}".strip(": ")
                    for a in notas if a.get("message")
                )
                if texto:
                    tail, source = _bounded_tail(texto), "annotations"
        checks.append(CiCheckEvidence(
            name=name, conclusion=str(run.get("conclusion") or ""), url=url,
            log_tail=tail, source=source,
        ))
    return CiFailureEvidence(work_item_id=work_item_id, checks=checks)


def _safe(fn, *args, **kwargs):
    """A missing permission (403), a vanished job (404) or a transport error
    is a fact about what could be fetched — never a reason to block the fix."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — best-effort, ver docstring
        logger.info("ci evidence: %s unavailable (%s: %s)", getattr(fn, "__name__", fn), type(exc).__name__, str(exc)[:120])
        return None
