"""Op `--op post_turn` — the Coder's post-turn INSIDE the sandbox (K8s runtime).

Same deterministic sequence the worker runs on the Docker runtime
(`_run_coder_turn_impl`), off the same source of truth (`workspace_hygiene` and
`scoped_git`, vendored into the image): prune disposables → restore lockfile
churn → revert test edits → scoped commit/push (fixed refspec, pre-receive hook
on the remote). Returns the lists for the worker to audit (P8 stays in the
worker; there is no Postgres nor audit credential here).
"""
from __future__ import annotations

from dse_contracts import PostTurnRequest, PostTurnResult

try:  # dev/test: worker packages in the venv
    from sandbox_runtime.scoped_git import ScopedGitSession
    from sandbox_runtime.workspace_hygiene import (
        prune_disposable_artifacts,
        restore_lockfile_churn,
        revert_test_edits,
    )
except ImportError:  # image: copies vendored at build time (Dockerfile)
    from ._scoped_git import ScopedGitSession  # type: ignore[no-redef]
    from ._workspace_hygiene import (  # type: ignore[no-redef]
        prune_disposable_artifacts,
        restore_lockfile_churn,
        revert_test_edits,
    )

from .gitops import _ensure_safe_directory


def run_post_turn(req: PostTurnRequest) -> PostTurnResult:
    try:
        _ensure_safe_directory()

        pruned: list[str] = []
        kept: list[str] = []
        if req.expected_files:
            pruned, kept = prune_disposable_artifacts(
                req.workspace_dir, req.expected_files, req.work_item_id
            )
        # BEFORE the hygiene block, not after. `post-checkout` fires on
        # path-limited checkouts too (measured), and the hygiene steps run
        # `git checkout -- <lockfile>` moments after the turn's own
        # `npm install` repointed core.hooksPath at `.husky/` — that is the
        # same window the OOM loop came out of, one call earlier.
        session = ScopedGitSession(workspace_dir=req.workspace_dir, branch=req.branch)
        session.ensure_identity()

        restored = restore_lockfile_churn(req.workspace_dir)
        reverted = revert_test_edits(
            req.workspace_dir, req.turn_start_sha, req.work_item_id
        )
        if session.has_changes():
            session.commit(req.commit_message)
        session.push()
        sha = session.current_sha()
        files_changed = (
            session.files_changed_against(req.turn_start_sha)
            if sha != req.turn_start_sha
            else []
        )
        return PostTurnResult(
            sha=sha,
            files_changed=files_changed,
            pruned=pruned,
            kept_out_of_plan=kept,
            restored_lockfiles=restored,
            reverted_tests=reverted,
        )
    except Exception as exc:  # noqa: BLE001 — P6 (includes GitScopeViolation)
        return PostTurnResult(
            error=f"{type(exc).__name__}: {str(exc)[:400]}",
            error_kind="gitops_error",
        )
