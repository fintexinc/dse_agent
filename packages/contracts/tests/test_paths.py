"""Classification of disposable paths (`is_disposable_artifact`).

Reconciliation of 2026-07-22: since `expected_files` became advisory in L1 (see
the operator decision), the post-Coder prune now deletes ONLY obvious CLI
garbage, instead of "everything outside the plan". The central invariant — the
one that shields the fix — is that NO source file is ever classified as
disposable.
"""
from __future__ import annotations

import pytest

from dse_contracts.paths import is_disposable_artifact


# The two cases from the statement, spelled out.
def test_spontaneous_cli_report_is_disposable():
    assert is_disposable_artifact("BUG_FIX_REPORT.md") is True


def test_legitimate_new_source_file_is_not_disposable():
    assert is_disposable_artifact("src/new-module.js") is False


# --- Anti-source INVARIANT: no code extension is disposable, not even when
# the NAME looks like a report (report.py, summary.js…). This is what
# guarantees the prune never deletes the fix. -------------------------------
_SOURCE_EXTS = [
    "py", "js", "jsx", "ts", "tsx", "mjs", "cjs", "go", "rs", "java", "rb",
    "php", "c", "cc", "cpp", "h", "hpp", "cs", "kt", "swift", "scala", "sql",
    "css", "scss", "html", "vue", "svelte", "sh", "yaml", "yml", "json",
    "toml", "xml", "proto", "gradle", "lua", "dart", "ex", "exs", "clj",
]


@pytest.mark.parametrize("ext", _SOURCE_EXTS)
def test_no_source_extension_is_disposable(ext):
    assert is_disposable_artifact(f"src/modulo.{ext}") is False
    # Not even with a report name — the name heuristic only applies to doc/text.
    assert is_disposable_artifact(f"REPORT.{ext}") is False
    assert is_disposable_artifact(f"pkg/IMPLEMENTATION_SUMMARY.{ext}") is False


@pytest.mark.parametrize(
    "path",
    [
        "build.log",
        "server.tmp",
        "scratch.temp",
        "config.py.bak",
        "patch.orig",
        "merge.rej",
        ".file.swp",
        "module.pyc",
        "daemon.pid",
    ],
)
def test_runtime_extensions_are_disposable(path):
    assert is_disposable_artifact(path) is True


@pytest.mark.parametrize("base", [".DS_Store", "Thumbs.db", "desktop.ini", "nohup.out"])
def test_editor_only_junk_is_disposable(base):
    assert is_disposable_artifact(base) is True
    assert is_disposable_artifact(f"sub/dir/{base}") is True


@pytest.mark.parametrize(
    "path",
    [
        "BUG_FIX_REPORT.md",
        "IMPLEMENTATION_SUMMARY.md",
        "docs/CHANGES_WALKTHROUGH.txt",
        "findings.md",  # case-insensitive: FINDINGS
        "sub/verification-notes.rst",  # VERIFICATION
        "SUMMARY.markdown",
    ],
)
def test_doc_reports_are_disposable(path):
    assert is_disposable_artifact(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "docs/architecture.md",
        "docs/api.md",
        "requirements.txt",  # NEVER — it is a dependency manifest
        "notes/getting-started.md",
        "LICENSE",
        "Makefile",
        "Dockerfile",
        ".gitignore",
        ".env",
    ],
)
def test_docs_and_legitimate_files_survive(path):
    assert is_disposable_artifact(path) is False


def test_normalises_the_windows_separator():
    assert is_disposable_artifact("sub\\dir\\BUG_FIX_REPORT.md") is True
    assert is_disposable_artifact("sub\\dir\\new-module.js") is False

