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
    apt = next(l for l in d.splitlines() if "apt-get install" in l)
    assert "ca-certificates" in apt, (
        "slim sem bundle de CA: o clone https morre com CAfile: none"
    )
