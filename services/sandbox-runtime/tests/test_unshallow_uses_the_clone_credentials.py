"""O unshallow tem que sair pelo MESMO caminho do clone.

Segunda falha do mesmo item (2026-08-18, rc.96): o completar-histórico foi
adicionado, o rótulo do erro melhorou (`GitCommandError`, não mais
`GitScopeViolation`) — e o push continuou recusado.

A causa: o clone carrega proxy e credencial como `-c` na LINHA DE COMANDO
(`http.proxy`, `http.extraHeader`), e `-c` não persiste no `.git/config`. Um
`git fetch --unshallow` posterior sai sem proxy e sem credencial, contra um
egress default-deny — falha, o workspace continua raso, e o push é recusado
exatamente como antes.

É a mesma lição do `core.hooksPath`, que este repo já pagou em três call
sites: configuração que vive no comando morre com o comando. Quem precisa dela
tem que carregá-la também.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dse_contracts import WorkspaceBootstrapRequest

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

import agent_runner.gitops as gitops  # noqa: E402


def test_the_unshallow_carries_the_proxy_and_the_credential(tmp_path, monkeypatch):
    chamadas: list[list[str]] = []

    def fake_git(args, cwd=None):
        chamadas.append(list(args))
        # simula o clone: cria um workspace RASO, como o --depth faz num repo
        # com histórico maior que a profundidade
        if "clone" in args:
            ws = Path(args[-1])
            (ws / ".git").mkdir(parents=True, exist_ok=True)
            (ws / ".git" / "shallow").write_text("deadbeef\n")
        return type("P", (), {"stdout": "sha\n", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(gitops, "_git", fake_git)
    monkeypatch.setenv("HTTP_PROXY", "http://egress-proxy:8080")

    class _Session:
        def __init__(self, **kw):
            self.workspace_dir = kw.get("workspace_dir")

        def ensure_identity(self):
            return None

        def unshallow_if_needed(self):
            return None

        def push(self):
            return None

        def current_sha(self):
            return "sha"

    monkeypatch.setattr(gitops, "ScopedGitSession", _Session)
    monkeypatch.setattr(gitops, "write_task_branch_marker", lambda *a, **k: None)

    req = WorkspaceBootstrapRequest(
        work_item_id="wi-proxy", branch="dse/wi-proxy", base_branch="main",
        repo="acme/repo", repo_host="github.com",
        workspace_dir=str(tmp_path / "ws"), checkpoint_path=str(tmp_path / "cp.git"),
    )
    gitops._clone_target_repo(req)

    unshallow = [c for c in chamadas if "--unshallow" in c]
    assert unshallow, (
        "clone raso não foi completado no ponto do clone — no push já é tarde, "
        "porque ali o origin já é o checkpoint local, que não tem o histórico"
    )
    cmd = " ".join(unshallow[0])
    assert "http.proxy=http://egress-proxy:8080" in cmd, (
        "o unshallow saiu SEM proxy: o egress é default-deny e a busca morre "
        f"em 'Network is unreachable' — {cmd}"
    )
    assert "X-Dse-Inject-Credential: github" in cmd, (
        f"o unshallow saiu sem a credencial que o clone usou — {cmd}"
    )
