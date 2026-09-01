"""Três honestidades pequenas do preview, cada uma paga com dinheiro medido.

**1. A evidência vem do container que QUEBROU.** `pod_failure_detail` pedia
`--all-containers --tail=40`: no pod com sidecar de Postgres — verborrágico no
boot — as 40 linhas são do banco, e a linha do container `web` (a causa) fica
de fora. Medido duas vezes: a triage recebeu log do Postgres, chutou, e o
autofix editou o manifesto do cliente com base no chute.

**2. O hostname do ingress chega ao processo.** Quando o repo declara `start`,
a receita descarta as próprias flags — inclusive `--allowed-hosts`. O SPA do
glide-path usa vite ^5.4.19, que desde a 5.4.12 BLOQUEIA Host desconhecido
(`server.allowedHosts` default vazio): sem o hostname, trocaríamos um crash por
uma tela "Blocked request". A plataforma sabe o nome; ela passa a dizê-lo, e o
`start` declarado usa `--allowed-hosts "$DSE_PREVIEW_HOST"`.

**3. A espera do DSE honra o `ready_timeout_s` declarado.** Ele só mexia no
`failureThreshold` da probe; quem desiste é o `kubectl wait`, que ignorava a
declaração. Um preview de UI instala, compila a API e sobe dois servidores —
declarar 1050s e ser abandonado aos 900 é degradar um preview que ia subir.
"""
from __future__ import annotations

import subprocess

from dse_validation.config import PreviewConfig, parse_repo_preview
from dse_validation.preview import argocd

_LABELS = {"dse.fintex/work-item": "wi_teste"}


def _cfg(host_template: str | None = None) -> PreviewConfig:
    cfg = PreviewConfig()
    cfg.mode = "source"
    if host_template:
        cfg.external_host_template = host_template
    return cfg


# ---------------------------------------------------------------------------
# 1. o container que quebrou fala primeiro
# ---------------------------------------------------------------------------

_RUIDO_POSTGRES = "\n".join(
    f"2026-09-01 00:0{i} UTC [1] LOG:  database system is ready" for i in range(9)
)
_CAUSA_WEB = "sh: 1: git: not found"


def _fake_kubectl(por_container: dict[str, str], visto: list):
    def _k(cfg, args, timeout=25):  # noqa: ARG001
        visto.append(list(args))
        alvo = "web" if "web" in args else "todos"
        return subprocess.CompletedProcess(
            args, 0, por_container.get(alvo, ""), "")
    return _k


def test_the_app_container_is_read_before_the_sidecars(monkeypatch):
    visto: list = []
    monkeypatch.setattr(argocd, "_kubectl", _fake_kubectl(
        {"web": _CAUSA_WEB, "todos": _RUIDO_POSTGRES}, visto))

    detalhe = argocd.pod_failure_detail(_cfg(), "preview-wi", "preview degraded: timeout")

    assert _CAUSA_WEB in detalhe, "a causa estava no container do app"
    assert "database system is ready" not in detalhe, (
        "o sidecar empurrou a causa para fora da janela — foi assim que a "
        "triage recebeu log do Postgres e chutou"
    )
    assert "-c" in visto[0] and "web" in visto[0], "a primeira leitura é do app"


def test_the_sidecars_still_answer_when_the_app_said_nothing(monkeypatch):
    """Container do app mudo (morreu antes de escrever) não pode virar
    silêncio: o que houver no pod ainda é melhor que o relógio pelado."""
    visto: list = []
    monkeypatch.setattr(argocd, "_kubectl", _fake_kubectl(
        {"web": "", "todos": _RUIDO_POSTGRES}, visto))

    detalhe = argocd.pod_failure_detail(_cfg(), "preview-wi", "preview degraded: timeout")
    assert "database system is ready" in detalhe


def test_a_cluster_that_does_not_answer_keeps_the_original_reason(monkeypatch):
    def _explode(cfg, args, timeout=25):  # noqa: ARG001
        raise RuntimeError("cluster fora do ar")

    monkeypatch.setattr(argocd, "_kubectl", _explode)
    assert argocd.pod_failure_detail(_cfg(), "preview-wi", "preview degraded: x") == (
        "preview degraded: x")


# ---------------------------------------------------------------------------
# 2. o hostname do ingress vira dado no container
# ---------------------------------------------------------------------------

def _deployment(kind: str, cfg: PreviewConfig) -> str:
    return argocd._source_deployment(
        "preview-wi", _LABELS, cfg, repo="acme/app", branch="dse/wi", kind=kind)


def test_the_ingress_hostname_reaches_the_container_in_both_recipes():
    cfg = _cfg("https://{namespace}.preview.example.com")
    for kind in ("ui", "deployable"):
        d = _deployment(kind, cfg)
        assert "DSE_PREVIEW_HOST" in d, f"kind={kind} não recebeu o hostname"
        assert "preview-wi.preview.example.com" in d


def test_without_an_ingress_template_no_host_variable_is_invented():
    d = _deployment("ui", _cfg())
    assert "DSE_PREVIEW_HOST" not in d


# ---------------------------------------------------------------------------
# 3. a espera respeita o que o repo declarou
# ---------------------------------------------------------------------------

def _decl(**preview):
    return parse_repo_preview({"version": 1, "preview": preview}, source="test")


def test_the_wait_extends_to_the_declared_ready_timeout():
    cfg = _cfg()
    base = argocd._ready_timeout(cfg, None)
    assert argocd._ready_timeout(cfg, _decl(ready_timeout_s=1050)) == 1050
    assert base < 1050, "fixture inútil se o default já fosse o teto"


def test_a_declaration_never_shortens_the_platform_wait():
    """Só estica. Encurtar trocaria um preview lento por um degradado — e a
    espera do DSE cobre o sync do Argo, não só o boot do app."""
    cfg = _cfg()
    base = argocd._ready_timeout(cfg, None)
    assert argocd._ready_timeout(cfg, _decl(ready_timeout_s=30)) == base


def test_without_a_declaration_the_wait_is_exactly_todays():
    cfg = _cfg()
    assert argocd._ready_timeout(cfg, _decl()) == argocd._ready_timeout(cfg, None)
