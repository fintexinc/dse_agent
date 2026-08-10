"""Patch compliance against the plan and immutable SHAs.

The diff is always ``base_sha...head_sha``. Branch names are mutable and may
not even exist in the sandbox clone; accepting them here was the cause of the
regression where ``main...HEAD`` got the WorkItem stuck.
"""
from __future__ import annotations

import logging

import re

from dse_contracts import GateStatus, L1Finding, PlanArtifact
from dse_contracts.paths import is_test_path

from dse_validation.config import _env_int  # config.py owns every env read in this service
from dse_validation.sandbox_exec import SandboxExecutor


class DiffSummary:
    def __init__(
        self,
        files_changed: list[str],
        total_lines_changed: int,
        *,
        base_sha: str,
        head_sha: str,
        lines_by_file: dict[str, int] | None = None,
    ):
        self.files_changed = files_changed
        self.total_lines_changed = total_lines_changed
        self.base_sha = base_sha
        self.head_sha = head_sha
        # Per-file breakdown, so the budget can charge each line to whoever
        # wrote it. A caller that does not supply it gets an empty map and
        # therefore pays for EVERY line: the budget errs strict, never loose.
        self.lines_by_file = dict(lines_by_file or {})

    @property
    def non_test_lines_changed(self) -> int:
        """Lines of the diff that live outside test paths.

        `is_test_path` is the same predicate the TesterToolset enforces on
        `write_file` (`toolsets.py`), so it is not a suffix heuristic invented
        here: it is exactly the set of paths the Tester was allowed to write.
        """
        return self.total_lines_changed - sum(
            n for path, n in self.lines_by_file.items() if is_test_path(path)
        )


class DiffComputationError(RuntimeError):
    pass


_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


def _verify_commit(executor: SandboxExecutor, sha: str, label: str, timeout: int) -> None:
    if not _FULL_GIT_SHA_RE.fullmatch(sha):
        raise DiffComputationError(
            f"{label} must be a full Git SHA of 40 or 64 hexadecimal characters"
        )
    result = executor.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], timeout=timeout)
    if not result.ok:
        raise DiffComputationError(f"{label}={sha} does not exist as a commit in the sandbox")


def compute_diff_summary(
    executor: SandboxExecutor,
    base_sha: str,
    head_sha: str,
    timeout: int = 60,
) -> DiffSummary:
    """``git diff --numstat <base_sha>...<head_sha>`` inside the sandbox — sums
    added+removed lines per file (binary files report "-" in the numstat; we
    count them as a touched file but 0 lines, so diffs with assets don't
    break)."""
    _verify_commit(executor, base_sha, "base_sha", timeout)
    _verify_commit(executor, head_sha, "head_sha", timeout)
    result = executor.run(
        ["git", "diff", "--numstat", f"{base_sha}...{head_sha}"], timeout=timeout
    )
    if result.returncode != 0:
        raise DiffComputationError(
            f"git diff --numstat failed (exit={result.returncode}): {result.stderr.strip()}"
        )
    files: list[str] = []
    lines_by_file: dict[str, int] = {}
    total = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        changed = 0
        if added.isdigit():
            changed += int(added)
        if removed.isdigit():
            changed += int(removed)
        files.append(path)
        lines_by_file[path] = changed
        total += changed
    return DiffSummary(
        files_changed=files,
        total_lines_changed=total,
        base_sha=base_sha,
        head_sha=head_sha,
        lines_by_file=lines_by_file,
    )


def _is_forbidden(path: str, forbidden_paths: list[str]) -> str | None:
    """The first pattern in `forbidden_paths` covering `path`, or None.

    Two properties, both of them bug fixes and not features:

    - it matches at ANY DEPTH. The old rule was a bare `startswith`, i.e. pinned
      to the repository root, so `packages/web/.github/workflows/ci.yml` did not
      match `.github/workflows/`. In a monorepo the shipped defaults matched
      nothing, and this is the ONLY hard path gate that sees the real diff
      (`policy.py` classifies risk from `expected_files`, which stopped gating
      the diff on 2026-07-22).
    - it matches whole path SEGMENTS. `startswith` also let `migrations/` cover
      `migrations_backup/0001.sql`; widening the reach without fixing that
      would have swapped a false negative for a false positive.

    Both fall out of wrapping path and pattern in "/" before comparing. A
    pattern that STARTS with "/" is pinned to the root — .gitignore's own
    convention, and the entire extent of the syntax: no globs, no wildcards.
    """
    haystack = "/" + path.replace("\\", "/").strip("/") + "/"
    for forbidden in forbidden_paths:
        pattern = forbidden.replace("\\", "/")
        needle = "/" + pattern.strip("/") + "/"
        if needle == "//":  # blank entry: a typo in the config, never "everything"
            continue
        hit = haystack.startswith(needle) if pattern.startswith("/") else needle in haystack
        if hit:
            return forbidden
    return None


_DIFF_BUDGET_ENV = "DSE_L1_DIFF_BUDGET_LINES"


def _effective_diff_budget(plan: PlanArtifact) -> tuple[int, str]:
    """The line budget in force, and where it came from.

    `PlanArtifact.diff_budget_lines` is a compile-time constant in practice:
    the workflow never sends the field, so every plan ever produced carries the
    contract's 400 and a large repository has no way to say its PRs are
    legitimately bigger. A line budget is an operational THRESHOLD — it does not
    choose which code runs — so the platform may set it from the environment,
    the same line `config.py` draws for the stage timeouts, and `_env_int` is
    what keeps a typo in the deployment from taking the whole activity down.

    The repo-scoped home for this number is `.dse/validation.json`, which is
    already loaded immutably from the base SHA; putting it there needs
    `config.py` to accept the field and the pipeline to hand it over, and both
    are outside this change.
    """
    budget = _env_int(_DIFF_BUDGET_ENV, plan.diff_budget_lines)
    if budget != plan.diff_budget_lines:
        return budget, _DIFF_BUDGET_ENV
    return budget, "PlanArtifact.diff_budget_lines"


def diff_budget_finding(diff: DiffSummary, plan: PlanArtifact) -> L1Finding:
    # no_code_change: the plan declares there is NO code change, but the
    # immutable diff changed files — a real inconsistency, so it fails. (This
    # is NOT expected_files; it is about whether a diff exists at all.)
    if plan.no_code_change and diff.files_changed:
        return L1Finding(
            check="diff_budget",
            passed=False,
            status=GateStatus.FAIL,
            detail=(
                "PlanArtifact.no_code_change=true, but the immutable diff "
                f"{diff.base_sha[:12]}...{diff.head_sha[:12]} changed {diff.files_changed}"
            ),
            summary=(
                "PlanArtifact.no_code_change=true, but the immutable diff "
                f"changed {len(diff.files_changed)} file(s)"
            ),
        )

    # OPERATOR DECISION (2026-07-22, 3rd real occurrence): expected_files no
    # longer fails the diff. The Planner predicts files from the TEXT of the
    # issue, BEFORE reading the code; in a bug fix the defect almost always
    # lives in a different layer than the symptom suggests (the issue talked
    # about DELETE /api/transactions → server.js; the bug was in src/store.js —
    # the Coder picked the right file and the gate failed the CORRECT fix).
    #
    # Safety gates that REMAIN (they don't depend on the Planner's prediction):
    #   - line budget (here): real anti-sprawl;
    #   - forbidden_paths: a SEPARATE hard check (migrations/, workflows/…);
    #   - sandbox scoped to the repo; empty plan blocked in the workflow
    #     (patch reject-empty-expected-files-v1), before L1.
    # expected_files is still used to CLASSIFY RISK in the workflow — it just
    # stopped being an equality gate on the diff.
    #
    # Test paths are NOT charged. This gate exists to contain the CODER's
    # sprawl, and it was being blown by another agent: measured on a real run,
    # 379 of the 400 lines, of which 218 (57.5% of the budget) were the two test
    # files the TESTER wrote one activity earlier — the Coder paid, with a
    # retry, for a diff it did not write. The exemption is not an escape hatch
    # for the Coder either — but the boundary MOVED on 2026-08-10: what the
    # post-turn reverts is now the loop's own INSTRUMENT (the specs whose git
    # subject is `tester(<this work_item_id>)`), not every test path. The
    # Coder's edits to a PRE-EXISTING customer spec survive, land in the PR diff
    # and are reviewed there — and they are not charged here either. Reading
    # this as "all test edits are reverted" is how the old comment lied.
    budget, budget_source = _effective_diff_budget(plan)
    charged = diff.non_test_lines_changed
    over_budget = charged > budget
    if not over_budget:
        return L1Finding(
            check="diff_budget",
            passed=True,
            detail=(
                f"diff within budget: {charged}/{budget} lines outside test paths "
                f"(budget from {budget_source}), {diff.total_lines_changed} lines "
                f"total across {len(diff.files_changed)} file(s) "
                "(expected_files is advisory; forbidden_paths validates the paths)"
            ),
            summary=f"diff within budget: {charged}/{budget} lines outside test paths",
        )
    return L1Finding(
        check="diff_budget",
        passed=False,
        detail=(
            f"diff of {charged} lines outside test paths "
            f"({diff.total_lines_changed} total across {len(diff.files_changed)} file(s)) "
            f"exceeds diff_budget_lines={budget}, from {budget_source}"
        ),
        summary=(
            f"diff of {charged} lines outside test paths exceeds "
            f"diff_budget_lines={budget}"
        ),
    )


def forbidden_paths_finding(diff: DiffSummary, plan: PlanArtifact) -> L1Finding:
    violations: list[tuple[str, str]] = []
    for f in diff.files_changed:
        hit = _is_forbidden(f, plan.forbidden_paths)
        if hit:
            violations.append((f, hit))

    if not violations:
        return L1Finding(
            check="forbidden_paths",
            passed=True,
            detail=f"no file touched under the plan's forbidden_paths ({plan.forbidden_paths})",
            summary="no file touched under the plan's forbidden_paths",
        )

    detail = "; ".join(
        f"{f} is under a path forbidden by PlanArtifact.forbidden_paths='{hit}'" for f, hit in violations
    )
    # The paths themselves are repository content and stay in `detail`; the
    # count is the platform's own and is what the ledger gets.
    return L1Finding(
        check="forbidden_paths",
        passed=False,
        detail=detail,
        summary=f"{len(violations)} file(s) under a path forbidden by the plan",
    )


logger = logging.getLogger(__name__)


def compute_diff_or_none(executor: SandboxExecutor, base_sha: str, head_sha: str):
    """The diff, or None when it cannot be computed.

    Computed ONCE per pipeline and handed to both consumers. It used to run
    twice — once to scope the per-file gates, once inside
    `plan_compliance_findings` — which is six `kubectl exec` round trips where
    three suffice, and which also made `_PIPELINE_FIXED_COST_SECONDS` (sized for
    three 60s git calls) understate the activity's fixed cost by 180s against
    its own start_to_close."""
    try:
        return compute_diff_summary(executor, base_sha, head_sha)
    except Exception:  # noqa: BLE001
        logger.warning("l1: the diff could not be computed — gates fall back to whole-repo scope",
                       exc_info=True)
        return None


def changed_files_or_none(
    executor: SandboxExecutor, base_sha: str, head_sha: str
) -> set[str] | None:
    """The paths this change touched, or None when the diff cannot be computed.

    None is deliberately not an empty set: an empty set would scope every
    per-file gate down to nothing and pass a broken change silently. None means
    "scope unknown", and the gates then judge everything, as they did before."""
    try:
        diff = compute_diff_summary(executor, base_sha, head_sha)
    except Exception:  # noqa: BLE001
        # Deliberately every exception, not just DiffComputationError. This
        # function only narrows the scope of other gates; it must never be the
        # reason the L1 activity dies. Whatever went wrong, "scope unknown" is
        # the safe answer and the gates go back to judging everything.
        logger.warning("l1: the diff could not be computed — gates fall back to whole-repo scope",
                       exc_info=True)
        return None
    return {f.lstrip("./") for f in diff.files_changed}


def plan_compliance_findings(
    executor: SandboxExecutor,
    plan: PlanArtifact,
    base_sha: str,
    head_sha: str,
    diff=None,
) -> list[L1Finding]:
    """`diff` is the one the pipeline already computed. Passing it avoids a
    second round of `kubectl exec` calls for an answer we have."""
    if diff is not None:
        return [diff_budget_finding(diff, plan), forbidden_paths_finding(diff, plan)]
    try:
        diff = compute_diff_summary(executor, base_sha, head_sha)
    except DiffComputationError as exc:
        return [
            L1Finding(
                check="git_diff",
                passed=False,
                status=GateStatus.ERROR,
                detail=str(exc),
                # `exc` wraps git's stderr — repository content.
                summary="the diff between base and head could not be computed",
            )
        ]
    return [diff_budget_finding(diff, plan), forbidden_paths_finding(diff, plan)]
