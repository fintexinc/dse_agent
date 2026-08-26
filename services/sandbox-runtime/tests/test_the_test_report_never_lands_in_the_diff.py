"""O relatório de teste não entra no diff da PR.

Medido em wi_e9764c2d (glide-path, 26/08): o commit do turno trouxe
`apps/api/reports/junit.xml` com 12.733 linhas. O caminho não é escolha do
cliente — é a plataforma que pede o relatório (`reports.junit`, para o gate
ler contagem em vez de adivinhar por prosa), e o `git add -A` do checkpoint
varre o workspace inteiro. Resultado: a PR que um humano vai revisar nasce
com um XML gerado maior que a mudança.

A plataforma limpa a própria sujeira, e sabe exatamente onde ela está: o glob
vem do MESMO campo que o gate lê. A exclusão é LOCAL (`.git/info/exclude`),
nunca o `.gitignore` do cliente — o DSE não edita a política do repositório
para resolver um problema que é dele.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.gitops import bootstrap_workspace, checkpoint_workspace  # noqa: E402
from dse_contracts import CheckpointOpRequest, WorkspaceBootstrapRequest  # noqa: E402


def _workspace(tmp_path, *, manifesto: dict | None):
    ws = tmp_path / "workspace"
    req = WorkspaceBootstrapRequest.model_validate({
        "work_item_id": "wi-report", "branch": "dse/wi-report",
        "workspace_dir": str(ws), "checkpoint_path": str(tmp_path / "checkpoint.git"),
    })
    bootstrap_workspace(req)
    if manifesto is not None:
        (ws / ".dse").mkdir(parents=True, exist_ok=True)
        (ws / ".dse" / "validation.json").write_text(json.dumps(manifesto))
    return ws


def _checkpoint(ws, tmp_path):
    return checkpoint_workspace(CheckpointOpRequest.model_validate({
        "work_item_id": "wi-report", "branch": "dse/wi-report",
        "workspace_dir": str(ws), "phase": "implementing",
    }))


def _tracked(ws) -> list[str]:
    r = subprocess.run(["git", "ls-files"], cwd=str(ws), capture_output=True, text=True)
    return r.stdout.split()


def test_the_declared_report_is_not_committed(tmp_path):
    ws = _workspace(tmp_path, manifesto={
        "version": 1, "commands": {"test": ["npm", "test"]},
        "reports": {"junit": "apps/*/reports/junit.xml"},
    })
    alvo = ws / "apps" / "api" / "reports"
    alvo.mkdir(parents=True)
    (alvo / "junit.xml").write_text("<testsuites/>" + "x" * 5000)
    (ws / "src.ts").write_text("export const x = 1;\n")

    _checkpoint(ws, tmp_path)

    rastreados = _tracked(ws)
    assert "src.ts" in rastreados, "o trabalho do turno tem que ser preservado"
    assert not any("junit.xml" in f for f in rastreados), (
        f"o relatório entrou no diff da PR: {rastreados}"
    )


def test_the_customer_gitignore_is_never_edited(tmp_path):
    """A exclusão é LOCAL: o DSE não muda a política do repositório."""
    ws = _workspace(tmp_path, manifesto={
        "version": 1, "reports": {"junit": "reports/junit.xml"},
    })
    _checkpoint(ws, tmp_path)

    assert not (ws / ".gitignore").exists(), "o DSE escreveu no .gitignore do cliente"
    exclude = Path(ws) / ".git" / "info" / "exclude"
    assert exclude.exists() and "junit.xml" in exclude.read_text()


def test_a_repo_without_a_declared_report_is_untouched(tmp_path):
    ws = _workspace(tmp_path, manifesto={"version": 1, "commands": {"test": ["pytest"]}})
    (ws / "a.py").write_text("x = 1\n")
    _checkpoint(ws, tmp_path)
    assert "a.py" in _tracked(ws)


def test_no_manifest_at_all_still_checkpoints(tmp_path):
    """Manifesto ausente não pode derrubar o checkpoint — é ele que preserva
    o trabalho pago do turno."""
    ws = _workspace(tmp_path, manifesto=None)
    (ws / "b.py").write_text("y = 2\n")
    r = _checkpoint(ws, tmp_path)
    assert not r.failed
    assert "b.py" in _tracked(ws)
