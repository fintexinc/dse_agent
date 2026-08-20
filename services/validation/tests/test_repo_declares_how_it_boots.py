"""O repo declara COMO SUBIR — e a plataforma para de saber o que é `java -jar`.

A rc.101 fez o BUILD do preview parar de assumir a forma do repositório (o
comando passou a vir parseado do manifesto) e parou no meio: não existe campo
para dizer como o PROCESSO sobe. A receita `deployable` termina literalmente em

    JAR=$(ls target/*.jar | grep -v plain | head -1); exec java -jar "$JAR"

e o branch `ui` é uma escada npm construída para Angular. Consequência: um repo
que declara `preview.image: golang:1.23` sobe a imagem certa e a receita tenta
rodar um jar; um FE em Vite ganha as flags do Angular CLI.

`preview.start` e `install` são argv, validados pelas MESMAS regras dos
`commands.*` (nunca string de shell), e o script do pod vira
`clone; credenciais; [install]; [build]; exec [start]`. `install` vive no TOPO
do manifesto: quem instala dependência é o repositório, e o turno do Tester lê
a mesma chave (a rc.105 nasceu com `preview.install` e a de topo a dobrou uma
hora depois — ver test_install_is_one_key.py).

Nesta release a escada CONTINUA como fallback, de propósito: o parser recusa
chave desconhecida, então os repos vivos só podem declarar o campo depois que
esta versão estiver no ar. A escada morre na release seguinte, quando as PRs de
emenda (geradas pelo próprio DSE) tiverem sido mergeadas.
"""
from __future__ import annotations

import pytest

from dse_validation.config import L1ManifestError, PreviewConfig, parse_repo_preview
from dse_validation.preview import argocd

_LABELS = {"dse.fintex/work-item": "wi_teste"}


def _cfg() -> PreviewConfig:
    cfg = PreviewConfig()
    cfg.mode = "source"
    return cfg


def _declara(**campos):
    # `install` é chave de topo; o resto é bloco preview.
    topo = {"version": 1, "commands": {}}
    if "install" in campos:
        topo["install"] = campos.pop("install")
    return parse_repo_preview(dict(topo, preview=campos))


def _deployment(**kw) -> str:
    base = dict(repo="acme/svc", branch="dse/wi", kind="deployable")
    base.update(kw)
    return argocd._source_deployment("preview-wi", _LABELS, _cfg(), **base)


# ---------------------------------------------------------------------------
# O contrato
# ---------------------------------------------------------------------------

def test_the_manifest_accepts_start_and_install_as_argv():
    d = _declara(start=["./bin/server", "--port", "8080"], install=["go", "mod", "download"])
    assert d.start == ["./bin/server", "--port", "8080"]
    assert d.install == ["go", "mod", "download"]


def test_a_shell_string_is_refused_like_every_other_command():
    """Mesma regra dos `commands.*`: argv, nunca string de shell — quem quer
    encadear escreve `["sh","-c","a && b"]` e assume o `&&`."""
    with pytest.raises(L1ManifestError):
        _declara(start="./bin/server --port 8080")
    with pytest.raises(L1ManifestError):
        _declara(install="npm ci")


def test_an_empty_argv_is_refused():
    with pytest.raises(L1ManifestError):
        _declara(start=[])


def test_a_repo_that_declares_nothing_keeps_both_as_none():
    d = _declara(image="node:20-alpine")
    assert d.start is None and d.install is None


# ---------------------------------------------------------------------------
# A receita
# ---------------------------------------------------------------------------

def test_a_go_service_boots_with_its_own_command_not_with_java():
    y = _deployment(repo_preview=_declara(image="golang:1.23",
                                          start=["./bin/server"],
                                          install=["go", "mod", "download"]))
    assert "./bin/server" in y
    assert "go mod download" in y
    assert "java -jar" not in y, "a receita ainda tenta rodar um jar num repo Go"
    assert "grep -v plain" not in y


def test_a_ui_repo_boots_with_its_own_command_not_with_the_angular_ladder():
    y = _deployment(kind="ui", repo_preview=_declara(image="node:22-alpine",
                                                     start=["npx", "vite", "preview", "--host"]))
    assert "npx vite preview --host" in y
    assert "ng serve" not in y, "a escada do Angular sobreviveu a uma declaração explícita"


def test_the_declared_install_replaces_the_hardcoded_npm_install():
    y = _deployment(kind="ui", repo_preview=_declara(install=["pnpm", "install", "--frozen-lockfile"]))
    assert "pnpm install --frozen-lockfile" in y
    assert "npm install --no-audit" not in y


def test_without_a_declaration_the_recipe_is_the_one_that_ships_today():
    """A REDE desta release: a escada continua para quem ainda não declarou —
    ela só morre depois que as PRs de emenda entrarem."""
    y = _deployment(repo_preview=None)
    assert "java -jar" in y
    ui = _deployment(kind="ui", repo_preview=None)
    assert "npm install --no-audit" in ui
