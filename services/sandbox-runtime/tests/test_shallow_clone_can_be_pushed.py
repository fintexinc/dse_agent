"""Repo com histórico de verdade tem que conseguir empurrar para o checkpoint.

Medido em produção 2026-08-18, primeiro item do
`fintexinc/calculation-engine-service` (147 commits): o item morreu em
`checkpoint_sandbox` com

    GitScopeViolation: push refused by the remote (scope)
    To /checkpoint.git
     ! [remote rejected] HEAD -> dse/wi_3b152b5c… (shallow

O rótulo mandou a investigação para App, permissão e allowlist — todos
inocentes. O "remote" é o bare repo DENTRO do Pod, e a palavra cortada pelo
truncamento era `(shallow update not allowed)`.

O clone é `--depth 50`. Repo com ≤50 commits recebe o histórico inteiro e não
tem fronteira rasa — por isso os testbeds (2 a 5 commits) sempre funcionaram e
nenhum teste pegou isto: o único teste de clone do repo usa upstream de UM
commit. Com fronteira, o push manda linhas `shallow` e o `git-receive-pack`
recusa, porque o bare repo é criado com `git init --bare` puro.

Aqui a profundidade é reduzida para 1 e o upstream tem 3 commits: mesma
geometria, sem criar 51 commits num teste.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.gitops import ScopedGitSession, install_pre_receive_guard  # noqa: E402


def _git(args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def _upstream_com_historico(tmp_path, commits: int):
    """Um repo 'do cliente' com mais de um commit — o caso normal do mundo."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(["init", "-q", "-b", "main", str(up)])
    _git(["-C", str(up), "config", "user.email", "u@x"])
    _git(["-C", str(up), "config", "user.name", "u"])
    for i in range(commits):
        (up / "README.md").write_text(f"linha {i}\n")
        _git(["-C", str(up), "add", "-A"])
        _git(["-C", str(up), "commit", "-q", "-m", f"commit {i}"])
    return up


def _workspace_raso(tmp_path, up, depth: int):
    ws = tmp_path / "ws"
    # `file://` e não o caminho: em clone LOCAL o git ignora `--depth` (usa
    # hardlinks) e o clone sairia completo — o teste passaria sem testar nada.
    _git(["clone", "--depth", str(depth), "--branch", "main", f"file://{up}", str(ws)])
    assert (ws / ".git" / "shallow").is_file(), (
        "pré-condição do teste: o clone precisa ser genuinamente raso"
    )
    return ws


def test_a_repo_deeper_than_the_clone_can_still_checkpoint(tmp_path):
    """O primeiro push do turno, no repo real. Sem o unshallow, o
    receive-pack recusa e o item morre antes de escrever qualquer linha."""
    up = _upstream_com_historico(tmp_path, commits=3)
    ws = _workspace_raso(tmp_path, up, depth=1)

    checkpoint = tmp_path / "checkpoint.git"
    checkpoint.mkdir()
    _git(["init", "--bare", str(checkpoint)])
    install_pre_receive_guard(str(checkpoint), "dse/wi-fundo")

    session = ScopedGitSession(workspace_dir=str(ws), branch="dse/wi-fundo")
    session.ensure_identity()
    # Mesma ordem do `_clone_target_repo` real: o unshallow tem que caber ANTES
    # de o origin virar o checkpoint — depois disso não há de onde completar.
    session.unshallow_if_needed()
    _git(["-C", str(ws), "checkout", "-b", "dse/wi-fundo"])
    _git(["-C", str(ws), "remote", "set-url", "origin", str(checkpoint)])

    session.push()  # vermelho: shallow update not allowed

    assert not (ws / ".git" / "shallow").is_file(), (
        "o workspace continua raso — o push só passou por acidente"
    )

    # E o checkpoint tem mesmo o commit — push aceito não é push completo.
    ref = _git(["-C", str(checkpoint), "rev-parse", "refs/heads/dse/wi-fundo"]).stdout.strip()
    assert ref == session.current_sha()


def test_a_plain_git_failure_is_not_reported_as_a_scope_violation(tmp_path):
    """O rótulo custou a investigação inteira: `push()` reetiquetava TODA
    falha de git como violação de escopo, e o hook nem tinha rodado. Só o que
    o hook recusa é violação de escopo."""
    from sandbox_runtime.scoped_git import GitScopeViolation

    up = _upstream_com_historico(tmp_path, commits=1)
    ws = tmp_path / "ws2"
    _git(["clone", "--branch", "main", str(up), str(ws)])
    session = ScopedGitSession(workspace_dir=str(ws), branch="dse/wi-x")
    session.ensure_identity()
    _git(["-C", str(ws), "checkout", "-b", "dse/wi-x"])
    # remote que não existe: falha de mecânica, não de escopo
    _git(["-C", str(ws), "remote", "set-url", "origin", str(tmp_path / "nao-existe.git")])

    with pytest.raises(Exception) as exc:
        session.push()
    assert not isinstance(exc.value, GitScopeViolation), (
        "falha comum de git rotulada como violação de escopo — foi o que "
        f"mandou a investigação para credencial e allowlist: {exc.value}"
    )
