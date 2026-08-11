"""Deterministic path classification (P1) — shared.

`is_test_path` used to belong to sandbox-runtime (TesterToolset); it was
promoted to the contract because the L1 plan_compliance also needs it: the
Tester writes tests BY DESIGN under test paths, and the plan (Planner) never
lists them — test files cannot count as "out of plan" (found on a real run
2026-07-22: L1 would fail every task that added a new test).
"""
from __future__ import annotations

import re

# Covers the common layouts: pytest (`tests/`, `test_*.py`, `*_test.py`,
# `conftest.py`), jest/vitest/node:test (`*.test.ts`, `*.spec.ts`,
# `__tests__/`, `test/`), go (`*_test.go`).
_TEST_PATH_RES = [
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"\.(test|spec)\.[jt]sx?$"),
    re.compile(r"_test\.go$"),
]


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    return any(rx.search(p) for rx in _TEST_PATH_RES)


# --- DISPOSABLE runtime/CLI artifacts (post-Coder prune) --------------------
# Ever since `expected_files` became ADVISORY in L1 (the Planner guesses the
# files from the TEXT of the issue, BEFORE reading the code — operator
# decision, see the l1-expected-files-advisory memory), the post-Coder prune
# can NO LONGER delete "everything outside the plan": a NEW and legitimate
# source file the fix had to create would fall outside `expected_files` and
# vanish silently before the commit. `is_disposable_artifact` restricts the
# prune to obvious GARBAGE (log/scratch/backup and the spontaneous report the
# CLI writes about its own work, e.g. BUG_FIX_REPORT.md).
#
# INVARIANT (covered in packages/contracts/tests/test_paths.py): NEVER
# classifies a source file as disposable. The asymmetry is deliberate — when in
# doubt, KEEP: garbage that slips through stays in the diff (caught by the L1
# line budget / human review); a source file deleted by mistake breaks the fix
# without leaving a trace in the diff.

# Extensions that are runtime garbage by definition (never source code).
_DISPOSABLE_EXTS = frozenset({
    ".log", ".tmp", ".temp", ".bak", ".orig", ".rej",
    ".swp", ".swo", ".pyc", ".pyo", ".pid",
})

# Exact basenames of OS/editor/process garbage.
_DISPOSABLE_BASENAMES = frozenset({
    ".ds_store", "thumbs.db", "desktop.ini", "nohup.out",
})

# ONLY doc/text files can be pruned by the report NAME convention (a
# .py/.js/.ts never matches through this rule — that is what shields source).
_REPORT_DOC_EXTS = frozenset({".md", ".markdown", ".txt", ".rst"})

# Words that give away a spontaneous CLI report (BUG_FIX_REPORT.md,
# IMPLEMENTATION_SUMMARY.md, CHANGES_WALKTHROUGH.txt…). Curated on purpose: it
# does NOT include README/CHANGELOG/CONTRIBUTING/LICENSE/requirements —
# legitimate, maintained documents and manifests, which must survive.
_REPORT_NAME_KEYWORDS = ("REPORT", "SUMMARY", "WALKTHROUGH", "FINDINGS", "VERIFICATION")


def is_disposable_artifact(path: str) -> bool:
    """True if `path` is an obvious DISPOSABLE runtime/CLI artifact — log,
    scratch, backup, OS/editor garbage, or a spontaneous report the CLI writes
    about its own work (BUG_FIX_REPORT.md).

    Used by the post-Coder prune to delete ONLY garbage. See the note above on
    the anti-source invariant and the keep-when-in-doubt asymmetry — in
    particular, the report-name heuristic only applies to doc/text extensions,
    so no source file (whatever its name) is ever disposable.
    """
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    if base.lower() in _DISPOSABLE_BASENAMES:
        return True
    stem, dot, ext = base.rpartition(".")
    if not dot:  # no extension (Makefile, LICENSE, Dockerfile…) — never garbage
        return False
    ext = "." + ext.lower()
    if ext in _DISPOSABLE_EXTS:
        return True
    if ext in _REPORT_DOC_EXTS:
        up = stem.upper()
        return any(kw in up for kw in _REPORT_NAME_KEYWORDS)
    return False


# Lockfile -> manifest that declares it. Running the package manager (npm test,
# poetry run…) rewrites lockfile metadata WITHOUT any dependency change (found
# on a real run 2026-07-22: npm changed 16 lines of package-lock.json and
# diff_budget failed the task as an "out of plan change"). Lockfile churn is
# only a REAL change when the paired manifest changed too.
LOCKFILE_MANIFESTS: dict[str, str] = {
    "package-lock.json": "package.json",
    "npm-shrinkwrap.json": "package.json",
    "yarn.lock": "package.json",
    "pnpm-lock.yaml": "package.json",
    "bun.lockb": "package.json",
    "poetry.lock": "pyproject.toml",
    "uv.lock": "pyproject.toml",
    "Pipfile.lock": "Pipfile",
    "Cargo.lock": "Cargo.toml",
    "Gemfile.lock": "Gemfile",
    "composer.lock": "composer.json",
    "go.sum": "go.mod",
}


def lockfile_manifest_for(path: str) -> str | None:
    """If `path` is a known lockfile, returns the path of the paired manifest
    (same directory); otherwise None."""
    p = path.replace("\\", "/")
    manifest = LOCKFILE_MANIFESTS.get(p.rsplit("/", 1)[-1])
    if manifest is None:
        return None
    head, _, _ = p.rpartition("/")
    return f"{head}/{manifest}" if head else manifest


def is_lockfile_churn(path: str, files_changed: list[str] | set[str]) -> bool:
    """True when `path` is a lockfile and the paired manifest is NOT in the
    diff — mechanical package-manager churn, not a declarable change."""
    manifest = lockfile_manifest_for(path)
    if manifest is None:
        return False
    changed = {f.replace("\\", "/") for f in files_changed}
    return manifest not in changed

# ---------------------------------------------------------------------------
# Documentation-only changes
# ---------------------------------------------------------------------------
#: A change touching nothing but these cannot break a linter, a type checker, a
#: test suite or a build. That is not a judgement call, and it is the one skip
#: that is safe without enumerating every config file that governs every tool.
DOC_EXTENSIONS = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})


def is_documentation_only(changed_files) -> bool:
    """True when every changed file is plainly documentation.

    Conservative in one direction on purpose: an empty change, an unknown
    scope, or a file with no extension (Dockerfile, Makefile, an entrypoint
    script — able to affect anything) all return False, i.e. DO THE WORK.
    Skipping something that could have found a defect is the error that
    matters; doing work that could not is merely slow.

    This lives in the contracts package because two very different consumers
    need the SAME answer: the L1 gates decide whether to run at all, and the
    workflow decides whether the Tester has anything to test. Two copies of the
    extension list would drift, and a drifted list is a false green."""
    if not changed_files:
        return False
    for f in changed_files:
        name = str(f).replace("\\", "/").rsplit("/", 1)[-1]
        if "." not in name:
            return False
        if ("." + name.rsplit(".", 1)[1].lower()) not in DOC_EXTENSIONS:
            return False
    return True
