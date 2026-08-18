"""agent-runner git ops (bootstrap/checkpoint) — Phase 1, plan 09.

They run IN PROCESS (real git in tmp dirs, no docker): the same code the K8s
driver executes via `kubectl exec` and that Docker can execute via `docker
exec`. They prove idempotency across the bootstrap's three states, the
clone-from-checkpoint recovery (the chaos rebuild, now in-sandbox) and that the
pre-receive hook is still in charge (force/wrong branch refused).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

# agent_runner lives in the sandbox image — imported by path in the tests
_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.gitops import bootstrap_workspace, checkpoint_workspace  # noqa: E402
from dse_contracts import CheckpointOpRequest, WorkspaceBootstrapRequest  # noqa: E402


def _bootstrap_req(tmp_path, wi="wi-g1", **over):
    base = dict(
        work_item_id=wi,
        branch=f"dse/{wi}",
        workspace_dir=str(tmp_path / "workspace"),
        checkpoint_path=str(tmp_path / "checkpoint.git"),
    )
    base.update(over)
    return WorkspaceBootstrapRequest.model_validate(base)


def test_bootstrap_init_then_idempotent_then_recover_from_checkpoint(tmp_path):
    req = _bootstrap_req(tmp_path)

    first = bootstrap_workspace(req)
    assert not first.failed and first.created and first.sha

    again = bootstrap_workspace(req)
    assert not again.failed and not again.created and again.sha == first.sha

    # commit + checkpoint inside the workspace
    ws = tmp_path / "workspace"
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("X = 1\n")
    ck = checkpoint_workspace(
        CheckpointOpRequest(
            work_item_id="wi-g1", branch=req.branch, phase="coder",
            workspace_dir=str(ws),
        )
    )
    assert not ck.failed and ck.sha != first.sha and ck.phase == "coder"

    # "Pod death": the workspace vanishes, the checkpoint survives → bootstrap CLONES
    shutil.rmtree(ws)
    recovered = bootstrap_workspace(req)
    assert not recovered.failed and not recovered.created
    assert recovered.sha == ck.sha  # recovered exactly the last checkpoint
    assert (ws / "src" / "app.py").read_text() == "X = 1\n"


def test_checkpoint_remote_still_enforces_scope(tmp_path):
    req = _bootstrap_req(tmp_path, wi="wi-g2")
    bootstrap_workspace(req)
    ws = str(tmp_path / "workspace")

    # raw push to another branch → the pre-receive hook (installed by bootstrap)
    # refuses server-side, exactly as in the worker flow
    proc = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/other-branch"],
        cwd=ws, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "refused" in proc.stderr or "rejected" in proc.stderr.lower()


def test_bootstrap_error_is_structured_not_raised(tmp_path):
    # checkpoint_path pointing at a FILE → git init --bare fails and the error
    # comes back structured (P6), never a raw exception crossing the exec
    bogus = tmp_path / "not-a-dir"
    bogus.write_text("x")
    result = bootstrap_workspace(_bootstrap_req(tmp_path, checkpoint_path=str(bogus)))
    assert result.failed and result.error_kind == "gitops_error"


def test_bootstrap_with_repo_clone_failure_is_fail_closed(tmp_path):
    """`repo` requested + clone impossible (dead host on port 1) → error_kind
    'clone_error'; it NEVER falls back to the empty workspace (that would mask
    the problem)."""
    req = WorkspaceBootstrapRequest(
        work_item_id="wi-clone",
        branch="dse/wi-clone",
        base_branch="main",
        repo="acme/nonexistent",
        repo_host="127.0.0.1:1",  # immediate connection refused (fail-fast)
        workspace_dir=str(tmp_path / "workspace"),
        checkpoint_path=str(tmp_path / "checkpoint.git"),
    )
    res = bootstrap_workspace(req)
    assert res.failed
    assert res.error_kind == "clone_error"
    # the workspace did NOT turn into an empty fallback git repo
    assert not (tmp_path / "workspace" / ".git").exists() or not (tmp_path / "workspace" / ".dse-task-branch").exists()


def test_bootstrap_clone_from_local_repo_repoints_origin_to_checkpoint(tmp_path):
    """Clone happy path (using a local repo as 'upstream' via file://):
    materializes the task branch, RE-POINTS origin at the checkpoint and does the
    first scoped push — proves the mechanics without network/proxy."""
    import subprocess

    # local 'upstream': a repo with one commit on main
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    subprocess.run(["git", "-C", str(upstream), "config", "user.email", "u@x"], check=True)
    subprocess.run(["git", "-C", str(upstream), "config", "user.name", "u"], check=True)
    (upstream / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(upstream), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(upstream), "commit", "-q", "-m", "base"], check=True)

    # _clone_target_repo builds https://<host>/<repo>.git; we point host+repo at
    # the local path via the file:// scheme, embedding the path in "repo".
    # (git accepts a file path as a remote; we use repo_host="" and repo=<abs path> without .git)
    import agent_runner.gitops as gitops
    req = WorkspaceBootstrapRequest(
        work_item_id="wi-ok",
        branch="dse/wi-ok",
        base_branch="main",
        repo="placeholder",
        workspace_dir=str(tmp_path / "ws"),
        checkpoint_path=str(tmp_path / "cp.git"),
    )
    # inject the local URL in place of the https:// one (the rest of the mechanics is the test's target)
    orig = gitops._git

    def fake_clone_url(req_inner):
        # replicates _clone_target_repo but against the local upstream
        gitops._git(["clone", "--depth", "50", "--branch", req_inner.base_branch, str(upstream), req_inner.workspace_dir])
        from agent_runner.gitops import ScopedGitSession, write_task_branch_marker
        session = ScopedGitSession(workspace_dir=req_inner.workspace_dir, branch=req_inner.branch)
        session.ensure_identity()
        gitops._git(["checkout", "-b", req_inner.branch], cwd=req_inner.workspace_dir)
        write_task_branch_marker(req_inner.workspace_dir, req_inner.branch)
        gitops._git(["remote", "set-url", "origin", req_inner.checkpoint_path], cwd=req_inner.workspace_dir)
        session.push()
        return session.current_sha()

    # `finally` restaurando os DOIS: antes só o `_git` voltava, e o
    # `_clone_target_repo` ficava trocado pelo fake para o resto da sessão —
    # todo teste posterior que exercitasse o clone real media o dublê. Custou
    # um vermelho fantasma em 2026-08-18.
    orig_clone = gitops._clone_target_repo
    gitops._clone_target_repo = fake_clone_url
    try:
        res = bootstrap_workspace(req)
    finally:
        gitops._git = orig
        gitops._clone_target_repo = orig_clone
    assert not res.failed and res.created and res.sha
    # origin re-pointed at the checkpoint (the upstream URL is gone from the config)
    import subprocess as sp
    remotes = sp.run(["git", "-C", str(tmp_path / "ws"), "remote", "get-url", "origin"],
                     capture_output=True, text=True).stdout.strip()
    assert remotes == str(tmp_path / "cp.git")
    assert str(upstream) not in remotes


def test_a_rebuild_re_establishes_the_git_identity(tmp_path, monkeypatch):
    """`git config user.*` lives in the workspace's own .git/config, and a
    rebuild throws that workspace away and clones a fresh one from the
    checkpoint. The identity set on the first bootstrap is gone, so every commit
    after a rebuild died on "Please tell me who you are" — and because the fix
    loop rebuilds before retrying, the retry could never succeed. Seen at
    attempt 14 on the VPS, failing the turn-start checkpoint commit.
    """
    import subprocess
    from agent_runner import gitops
    from dse_contracts import WorkspaceBootstrapRequest

    # The sandbox Pod has NO ambient git identity. A dev machine does, so
    # without this isolation the commit below succeeds either way and the test
    # proves nothing — it passed against the unfixed code before this was added.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)
    monkeypatch.delenv("EMAIL", raising=False)

    ckpt = tmp_path / "cp.git"
    ws1, ws2 = tmp_path / "ws1", tmp_path / "ws2"
    branch = "dse/task-1"

    def git(args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    # A checkpoint that already carries the task branch — the rebuild's input.
    git(["init", "--bare", str(ckpt)])
    git(["init", "-b", branch, str(ws1)])
    git(["config", "user.email", "a@b.c"], cwd=ws1)
    git(["config", "user.name", "a"], cwd=ws1)
    (ws1 / "f.txt").write_text("x")
    git(["add", "-A"], cwd=ws1)
    git(["commit", "-m", "seed"], cwd=ws1)
    git(["push", str(ckpt), f"HEAD:refs/heads/{branch}"], cwd=ws1)

    gitops.bootstrap_workspace(
        WorkspaceBootstrapRequest(
            work_item_id="wi-1", branch=branch,
            workspace_dir=str(ws2), checkpoint_path=str(ckpt),
        )
    )

    # The property, asserted directly rather than through a commit: git can
    # auto-detect an identity from the host on a dev machine, so committing here
    # succeeds either way and proves nothing. In the Pod there is nothing to
    # auto-detect from, and the commit fails. What has to be true is that the
    # rebuilt workspace CARRIES the identity.
    def cfg(key):
        return subprocess.run(
            ["git", "config", "--local", key], cwd=ws2, capture_output=True, text=True
        ).stdout.strip()

    assert cfg("user.email") == "coder@dse.local", "the rebuild left no git identity"
    assert cfg("user.name") == "dse-coder"

    # And the commit the fix loop makes right after a rebuild now works.
    r = subprocess.run(
        ["git", "-c", "user.useConfigOnly=true", "commit",
         "-m", "checkpoint(turn-start): wi-1", "--allow-empty"],
        cwd=ws2, capture_output=True, text=True,
    )
    assert r.returncode == 0, f"commit after rebuild failed: {r.stderr[:200]}"
