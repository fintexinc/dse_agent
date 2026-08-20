"""Um repositório instala dependências de UM jeito.

A rc.105 abriu `preview.install`. Uma hora depois o Tester precisou do mesmo
passo — ele é quem cria o `node_modules` que o L1 depois reaproveita — e a
escolha era declarar de novo. Duas chaves para "instale as dependências" é a
complexidade que o operador mandou remover, e a segunda chega sempre
desatualizada em relação à primeira.

Então `install` sobe para o topo do manifesto, com dois consumidores: o Pod do
sandbox (Tester) e o Pod do preview. Preparo que só o preview precisa continua
cabendo em `preview.build`, que já existe e roda lá.

`preview.install` deixa de ser aceito de propósito: nenhum manifesto vivo o
declara (a chave nasceu hoje e as PRs de emenda ainda não mergearam), e aceitar
os dois seria manter as duas gramáticas para sempre.
"""
from __future__ import annotations

import pytest

from dse_contracts import GateStatus
from dse_validation.config import L1Config, L1ManifestError


def _manifest(**extra):
    base = {"version": 1, "commands": {"test": ["pytest", "-q"]}}
    base.update(extra)
    return base


def test_install_lives_at_the_top_of_the_manifest():
    cfg = L1Config._from_manifest_payload(
        _manifest(install=["go", "mod", "download"]), source="test")
    assert cfg.install_cmd == ["go", "mod", "download"]


def test_a_manifest_without_install_declares_nothing():
    cfg = L1Config._from_manifest_payload(_manifest(), source="test")
    assert cfg.install_cmd == []


def test_install_is_argv_like_every_other_command():
    with pytest.raises(L1ManifestError) as err:
        L1Config._from_manifest_payload(_manifest(install="npm ci"), source="test")
    assert err.value.status is GateStatus.ERROR
    assert "install" in str(err.value)


def test_the_preview_block_no_longer_carries_its_own_install():
    """A chave dobrada some com nome: o erro tem de dizer o que fazer, senão o
    autor do manifesto lê 'unknown field' e chuta outro nome."""
    with pytest.raises(L1ManifestError) as err:
        L1Config._from_manifest_payload(
            _manifest(preview={"install": ["npm", "ci"], "start": ["./x"]}),
            source="test")
    assert "install" in str(err.value)


def test_the_subset_command_is_a_command_but_not_a_gate():
    """`commands.test_subset` passa pela mesma porta de argv dos outros, e
    NÃO vira um quinto estágio do L1: nada de `timeouts.test_subset`, nada de
    desligá-lo por `disabled_stages`, nada de veredito próprio."""
    cfg = L1Config._from_manifest_payload(
        _manifest(commands={"test": ["pytest", "-q"],
                            "test_subset": ["pytest", "-q", "--no-cov"]}),
        source="test")
    assert cfg.test_subset_cmd == ["pytest", "-q", "--no-cov"]

    with pytest.raises(L1ManifestError):
        L1Config._from_manifest_payload(
            _manifest(commands={"test": ["pytest"]},
                      timeouts={"test_subset": 60}), source="test")
