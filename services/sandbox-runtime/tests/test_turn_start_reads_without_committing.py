"""O início do turno LÊ o sha — não commita, não empurra.

Medido no glide-path: 4 commits por volta, e o primeiro era um
`checkpoint(turn-start)` cujo único consumidor é `base_sha` — uma LEITURA.
Pior que o custo: com sujeira herdada de uma activity que morreu no meio, o
turn-start commitava o lixo como se fosse fase, e a PR nascia com um commit
que nenhum turno pediu. Sujeira herdada pertence ao diff do turno que a
encontrar (post_turn), onde o L1 a julga.

`turn-start` vira leitura pura: devolve o HEAD como está, deixa o working
tree intacto e não toca o remote. As demais fases continuam checkpoints de
verdade.
"""
from __future__ import annotations

import os
import subprocess
import sys

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.gitops import bootstrap_workspace, checkpoint_workspace  # noqa: E402
from dse_contracts import CheckpointOpRequest, WorkspaceBootstrapRequest  # noqa: E402


def _workspace(tmp_path):
    ws = tmp_path / "workspace"
    bootstrap_workspace(WorkspaceBootstrapRequest.model_validate({
        "work_item_id": "wi-ts", "branch": "dse/wi-ts",
        "workspace_dir": str(ws), "checkpoint_path": str(tmp_path / "checkpoint.git"),
    }))
    return ws


def _git(ws, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(ws), capture_output=True, text=True)
    return r.stdout.strip()


def _checkpoint(ws, phase):
    return checkpoint_workspace(CheckpointOpRequest.model_validate({
        "work_item_id": "wi-ts", "branch": "dse/wi-ts",
        "workspace_dir": str(ws), "phase": phase,
    }))


def test_turn_start_reads_head_and_leaves_the_dirty_tree_alone(tmp_path):
    ws = _workspace(tmp_path)
    head_antes = _git(ws, "rev-parse", "HEAD")
    (ws / "lixo-herdado.txt").write_text("sobras de uma activity que morreu\n")

    r = _checkpoint(ws, "turn-start")

    assert not r.failed
    assert r.sha == head_antes, "turn-start devolve o HEAD como está"
    assert _git(ws, "rev-parse", "HEAD") == head_antes, "nenhum commit novo"
    assert "lixo-herdado.txt" in _git(ws, "status", "--porcelain"), (
        "a sujeira fica no working tree — o post_turn a commita como parte do "
        "diff do turno, onde o L1 julga"
    )
    assert "turn-start" not in _git(ws, "log", "--oneline", "-3")


def test_other_phases_still_checkpoint_for_real(tmp_path):
    ws = _workspace(tmp_path)
    head_antes = _git(ws, "rev-parse", "HEAD")
    (ws / "trabalho.txt").write_text("progresso pago\n")

    r = _checkpoint(ws, "implementing")

    assert not r.failed
    assert r.sha != head_antes, "fase de verdade commita o progresso"
    assert _git(ws, "status", "--porcelain") == ""
