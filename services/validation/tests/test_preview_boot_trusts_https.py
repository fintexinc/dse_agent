"""O boot do preview clona por https — a imagem tem que confiar em ALGUÉM.

Medido no wi_b95a1d0b (glide-path, 2026-08-31): `node:22-bookworm-slim` +
`apt-get install --no-install-recommends git` = git SEM `ca-certificates`
(o bundle é só Recommends do git), e o clone morre com
"server certificate verification failed. CAfile: none" — CrashLoopBackOff
eterno, preview nunca resolve, card de finalizado nunca sai.

Os previews anteriores passavam por sorte de imagem: temurin traz o bundle,
e o `apk add git` do alpine o puxa como dependência dura. A receita declara
`--no-install-recommends` de propósito (imagem enxuta); então o que ela
recomenda de menos, ela precisa pedir por nome.
"""
from __future__ import annotations

from dse_validation.config import PreviewConfig
from dse_validation.preview import argocd

_LABELS = {"dse.fintex/work-item": "wi_teste"}


def _cfg() -> PreviewConfig:
    cfg = PreviewConfig()
    cfg.mode = "source"
    return cfg


def test_the_apt_recipe_installs_the_ca_bundle():
    d = argocd._source_deployment(
        "preview-wi", _LABELS, _cfg(),
        repo="acme/svc", branch="dse/wi", kind="deployable",
    )
    apt = next(linha for linha in d.splitlines() if "apt-get install" in linha)
    assert "ca-certificates" in apt, (
        "slim sem bundle de CA: o clone https morre com CAfile: none"
    )


def test_the_ui_recipe_reaches_git_on_debian_too():
    """Irmão do caso apt (wi_83ca26c9, 2026-09-01): a receita de UI só tinha
    `apk add` com `|| true` — em node:22-bookworm-slim (debian) o apk não
    existe, o erro é engolido e o boot morre em `git: not found`. Os previews
    de UI anteriores eram alpine; o primeiro repo debian de kind=ui achou o
    buraco. O degrau apt vem com ca-certificates pelo mesmo motivo do
    deployable (CAfile: none)."""
    d = argocd._source_deployment(
        "preview-wi", _LABELS, _cfg(),
        repo="acme/app", branch="dse/wi", kind="ui",
    )
    apt = [linha for linha in d.splitlines() if "apt-get install" in linha]
    assert apt, "receita ui sem degrau apt: debian fica sem git"
    assert "ca-certificates" in apt[0]
    assert "git" in apt[0]
