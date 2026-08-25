"""Dependência privada via git no lockfile não pode matar o `npm install`.

Medido em wi_ef766cdd (glide-path-planner-93, 2026-08-25): o package-lock traz
`git+ssh://git@github.com/fintexinc/wealth-components.git`. No pod, ssh não
atravessa o egress-proxy e o pod não segura credencial POR DESENHO (P2/ADR-12)
— o `npm install` morre, `node_modules` não existe, o typecheck cai com
`tsc: not found` (rc=127) e o laço escala em `coder_made_no_change`, porque o
código nunca teve culpa.

O conserto usa a máquina que JÁ existe: o proxy termina o request `http://` e
injeta o installation token com escopo derivado do PATH (auditado; residual
"mesma instalação" documentado no proxy). O bootstrap passa a ensinar o git
GLOBAL do pod a reescrever as formas ssh/scp-like/https de github.com para o
`http://` proxiado com o header placeholder — o token continua existindo só
na memória do proxy.
"""
from __future__ import annotations

import os
import subprocess
import sys

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.gitops import bootstrap_workspace  # noqa: E402
from dse_contracts import WorkspaceBootstrapRequest  # noqa: E402


def _bootstrap_req(tmp_path, wi="wi-transport", **over):
    base = dict(
        work_item_id=wi,
        branch=f"dse/{wi}",
        workspace_dir=str(tmp_path / "workspace"),
        checkpoint_path=str(tmp_path / "checkpoint.git"),
    )
    base.update(over)
    return WorkspaceBootstrapRequest.model_validate(base)


def _global_config(home) -> str:
    r = subprocess.run(["git", "config", "--global", "--list"],
                       capture_output=True, text=True,
                       env={**os.environ, "HOME": str(home)})
    return r.stdout


def test_bootstrap_teaches_git_to_fetch_private_git_dependencies(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HTTP_PROXY", "http://egress-proxy:8806")

    bootstrap_workspace(_bootstrap_req(tmp_path))

    cfg = _global_config(home)
    assert "url.http://github.com/.insteadof=ssh://git@github.com/" in cfg, (
        "a forma do lockfile (git+ssh) continua indo para ssh — que não "
        "atravessa o proxy"
    )
    assert "url.http://github.com/.insteadof=git@github.com:" in cfg, (
        "a forma scp-like também aparece em lockfiles"
    )
    assert "url.http://github.com/.insteadof=https://github.com/" in cfg, (
        "https vira CONNECT opaco — o proxy não consegue injetar"
    )
    assert "http.proxy=http://egress-proxy:8806" in cfg
    assert "x-dse-inject-credential" in cfg.lower()
    assert "http.followredirects=false" in cfg.lower(), (
        "o 301 do GitHub para https derrubaria o fetch de volta no tunnel"
    )
    assert "x-access-token" not in cfg, "credencial escrita no pod viola P2/ADR-12"


def test_without_a_proxy_nothing_is_written(tmp_path, monkeypatch):
    """Docker/dev local: sem egress-proxy o transporte de sempre continua —
    o bootstrap não polui o git global de quem roda fora do pod."""
    home = tmp_path / "home2"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)

    bootstrap_workspace(_bootstrap_req(tmp_path))

    assert "insteadof" not in _global_config(home).lower()
