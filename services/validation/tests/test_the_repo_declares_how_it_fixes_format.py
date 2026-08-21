"""O repositório declara o comando que CONSERTA o que o lint reprova.

Medido no calculation-engine (2026-08-21): quatro turnos de Coder — ~US$ 4 e
~14 minutos — para acertar ordem de imports que o `spotless:apply` conserta em
7 segundos, com 100% de acerto. O laço não convergiu em nenhuma das quatro.

Formatação é a classe de falha em que a ferramenta que ACUSA também sabe
CONSERTAR. Pedir a um modelo que reescreva imports para casar com um formatador
é caro, lento e não determinístico — é exatamente o trabalho que o formatador
faz perfeitamente.

A tentação era ensinar `spotless:apply` à plataforma. Isso é a escada do Tester
de novo com outro nome: no mês seguinte viriam `ruff format`, `prettier
--write`, `gofmt -w`, `dotnet format`, `rubocop -a`, `cargo fmt`. A plataforma
não aprende NENHUM nome de ferramenta — ela aprende o conceito "existe um
comando que conserta o que este gate reprova", e cada repositório preenche com
o dele. Toda linguagem tem um formatador com modo de escrita; a chave é a mesma
para todas.

`lint_fix` é comando, não estágio: sem gate próprio, sem `timeouts.lint_fix`,
fora de `disabled_stages`, sem veredito. Ele só existe para ser executado
quando `lint` reprova.
"""
from __future__ import annotations

import pytest

from dse_contracts import GateStatus
from dse_validation.config import L1Config, L1ManifestError


def _manifesto(**comandos):
    base = {"lint": ["ruff", "check", "."]}
    base.update(comandos)
    return {"version": 1, "commands": base}


def test_the_repo_can_declare_how_to_fix_what_lint_refuses():
    cfg = L1Config._from_manifest_payload(
        _manifesto(lint_fix=["ruff", "format", "."]), source="test")
    assert cfg.lint_fix_cmd == ["ruff", "format", "."]


def test_a_repo_that_declares_none_has_none():
    cfg = L1Config._from_manifest_payload(_manifesto(), source="test")
    assert cfg.lint_fix_cmd == []


def test_it_is_argv_like_every_other_command():
    """Mesma porta dos `commands.*`: nunca string de shell. Quem quer encadear
    escreve `["sh","-c","a && b"]` e assume o `&&`."""
    with pytest.raises(L1ManifestError) as err:
        L1Config._from_manifest_payload(
            _manifesto(lint_fix="./mvnw spotless:apply"), source="test")
    assert err.value.status is GateStatus.ERROR


def test_it_is_a_command_and_never_a_gate():
    """Um quinto veredito é a última coisa que o L1 precisa. `lint_fix` não
    tem timeout próprio nem pode ser desligado por `disabled_stages` — desligar
    o conserto sem desligar a acusação não é um estado que faça sentido."""
    with pytest.raises(L1ManifestError):
        L1Config._from_manifest_payload(
            {"version": 1, "commands": {"lint": ["x"]}, "timeouts": {"lint_fix": 60}},
            source="test")
    with pytest.raises(L1ManifestError):
        L1Config._from_manifest_payload(
            {"version": 1, "commands": {"lint": ["x"]}, "disabled_stages": ["lint_fix"]},
            source="test")
