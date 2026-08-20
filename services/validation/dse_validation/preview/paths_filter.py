"""FR-20 — UI-touching decision through a 100% DETERMINISTIC paths-filter (P1:
never by model). `fnmatch` of `files_changed` against `ui_path_globs` (defaults
in the `TriggerPreviewInput` contract). A backend-only PR => `skipped_backend_only`,
which counts as SUCCESS and never blocks.

Glob semantics (documented — stdlib fnmatch, P7):
  - fnmatch's `*` crosses `/` (regex `.*`), so `ui/**` matches `ui/a/b.css`;
  - globs starting with `**/` also match the path WITHOUT a directory prefix
    (e.g.: `**/*.css` matches `app.css` and `web/app.css`) — without that
    adjustment, `fnmatch("app.css", "**/*.css")` would be false and a PR that
    only changes a CSS file at the root would escape the preview.
"""
from __future__ import annotations

from dse_contracts.paths import file_matches_glob as _file_matches_glob


# Esta implementação era a CORRETA das três e por isso foi a promovida ao
# contrato (2026-08-20). O nome continua exportado daqui: os testes deste
# serviço o nomeiam.
file_matches_glob = _file_matches_glob


def is_ui_touching(files_changed: list[str], ui_path_globs: list[str]) -> bool:
    """True if ANY changed file matches ANY UI glob."""
    return any(
        file_matches_glob(path, glob) for path in files_changed for glob in ui_path_globs
    )


def matching_files(files_changed: list[str], ui_path_globs: list[str]) -> list[str]:
    """The files that triggered the decision (evidence for the audit, P8)."""
    return [
        path for path in files_changed
        if any(file_matches_glob(path, glob) for glob in ui_path_globs)
    ]


def preview_decision(
    files_changed: list[str],
    ui_path_globs: list[str],
    deployable_globs: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Plan 08 §D — deterministic previewability decision (P1).

    Returns `(kind, matching)` where kind ∈ {"ui", "deployable", "none"}:
      - "ui"          → touched the front end (UI globs) — takes precedence (the
                        visual preview is the most informative case for the
                        human reviewer);
      - "deployable"  → touched a deployable service (back end) — Dockerfile/
                        source/manifest;
      - "none"        → only docs/tests/non-deployable config → skip the preview
                        (counts as success, NEVER blocks — FR-20).
    `matching` are the files that triggered the decision (evidence, P8)."""
    ui_hits = matching_files(files_changed, ui_path_globs)
    if ui_hits:
        return "ui", ui_hits
    dep_hits = matching_files(files_changed, deployable_globs or [])
    if dep_hits:
        return "deployable", dep_hits
    return "none", []
