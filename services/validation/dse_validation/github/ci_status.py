"""WSE-E4-T9a — minimal consumption of PR status checks: reads GitHub's
check-runs for the branch's tip commit and aggregates them into a coarse signal
(`pending|green|red|no_ci`) for the WS-B workflow to decide what to do (P1: the
workflow decides, this function only delivers the result). No previews, no
selective re-run (Phase 3) — just reading + deterministic aggregation.

`no_ci` exists because "nothing has reported yet" and "nothing will ever report"
are not the same fact, and collapsing them into `pending` is what kept every
work item this system ever produced from finishing: the target repo has no
workflows, GitHub returns an empty check-run array forever, and the wait had no
way to end.
"""
from __future__ import annotations

from dse_contracts import CiStatusResult, FailingCheck

from dse_validation import db
from dse_validation.github.client import GitHubClient

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

_TERMINAL_FAILURE_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "stale"}

CI_NO_CI = "no_ci"


def aggregate_check_runs(check_runs: list[dict]) -> str:
    """green only if ALL check-runs finished with a non-failure conclusion;
    red if ANY of them finished in failure; pending while some are still
    running (decline-never-truncate: we never "guess" green before everything
    has completed); `no_ci` when the ref has no check run at all.

    Per-source: `no_ci` here means only "check runs reported nothing". The
    caller combines it with the legacy commit-status signal before concluding
    that the repo has no CI — see `aggregate_ci`."""
    if not check_runs:
        return CI_NO_CI
    saw_failure = False
    for run in check_runs:
        if run.get("status") != "completed":
            return "pending"
        if run.get("conclusion") in _TERMINAL_FAILURE_CONCLUSIONS:
            saw_failure = True
    return "red" if saw_failure else "green"


def aggregate_combined_status(combined: dict | None) -> str:
    """The legacy commit-status API, for repos whose CI reports through
    statuses rather than check runs.

    Keys off the `statuses` ARRAY, never off `state`: GitHub answers
    `{"state": "pending", "statuses": [], "total_count": 0}` for a commit
    nothing has reported on — the identical trap that produced this bug on the
    check-run side."""
    statuses = (combined or {}).get("statuses") or []
    if not statuses:
        return CI_NO_CI
    state = (combined or {}).get("state")
    if state == "success":
        return "green"
    if state in ("failure", "error"):
        return "red"
    return "pending"


def aggregate_ci(check_runs: list[dict], combined: dict | None) -> str:
    """Combines both sources. `no_ci` only when NEITHER reported anything;
    red wins over pending, pending wins over green — a signal that is still
    running can still turn red."""
    signals = [
        s for s in (aggregate_check_runs(check_runs), aggregate_combined_status(combined))
        if s != CI_NO_CI
    ]
    if not signals:
        return CI_NO_CI
    if "red" in signals:
        return "red"
    if "pending" in signals:
        return "pending"
    return "green"


def consume_ci_status_core(
    github_client: GitHubClient,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    pr_number: int,
    ref: str,
    actor: str = "system:validation",
) -> CiStatusResult:
    check_runs = github_client.list_check_runs(repo, ref)
    # Lazily: only ask about legacy commit statuses when check runs reported
    # nothing. On a healthy repo this keeps the hot path at one call per poll
    # instead of two, and it keeps an endpoint the App may not be permitted to
    # use off the path of every green PR.
    combined = github_client.get_combined_status(repo, ref) if not check_runs else None
    status = aggregate_ci(check_runs, combined)

    summary = [
        {"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")}
        for r in check_runs
    ]
    combined_summary = (
        None
        if combined is None
        else {
            "state": combined.get("state"),
            "contexts": [s.get("context") for s in (combined.get("statuses") or [])],
        }
    )
    previous_status = db.save_ci_status(
        work_item_id, pr_number, status,
        {"check_runs": summary, "combined_status": combined_summary},
    )

    # The row above is current state and is rewritten on every poll; the ledger
    # records facts, and "pending is still pending" is not one. A 45-minute wait
    # wrote 46 identical rows here, which is how `ci_status_consumed` reached 27%
    # of `audit_log` on the cluster. What is kept is every real change — including
    # the first observation, where there is nothing to compare against. A retry of
    # an observation whose write already committed sees no change and stays
    # silent, so the transition is recorded once, not once per attempt.
    if audit_emit is not None and previous_status != status:
        audit_emit(
            actor=actor,
            action="ci_status_consumed",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"repo": repo, "pr_number": pr_number, "status": status,
                     "from_status": previous_status,
                     "check_runs": summary, "combined_status": combined_summary},
        )

    # The names travel with the verdict (rc.130). `html_url` is GitHub's own
    # page for the run; `details_url` is what an external CI points at. The
    # legacy commit statuses carry the same two facts under other names.
    failing = [
        FailingCheck(
            name=str(r.get("name") or "?"),
            conclusion=str(r.get("conclusion") or ""),
            url=r.get("html_url") or r.get("details_url"),
        )
        for r in check_runs
        if r.get("status") == "completed" and r.get("conclusion") in _TERMINAL_FAILURE_CONCLUSIONS
    ]
    if combined is not None:
        failing += [
            FailingCheck(
                name=str(s.get("context") or "?"),
                conclusion=str(s.get("state") or ""),
                url=s.get("target_url"),
            )
            for s in (combined.get("statuses") or [])
            if s.get("state") in ("failure", "error")
        ]
    return CiStatusResult(
        work_item_id=work_item_id, pr_number=pr_number, status=status, failing_checks=failing,
    )
