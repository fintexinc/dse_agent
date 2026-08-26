"""A emenda cobra o que REPROVA a rodada — não só a receita de boot do preview.

Medido quatro vezes seguidas no glide-path-planner-93 (25-26/08): comando de
gate ausente vira `NOT_CONFIGURED`, `NOT_CONFIGURED` reprova a rodada, e o
Coder NÃO pode consertar — gates leem o manifesto do SHA base, por desenho.
O item escala, o operador descobre pelo texto do erro, e alguém abre a PR à
mão. Foi exatamente o que aconteceu com `lint`: três rodadas pagas (~US$ 6
cada) para chegar na mesma frase.

O mecanismo de emenda JÁ existia e cobrava um item só (`preview.start`) — a
lacuna era o QUE ele cobra. Comando de gate ausente é a falha mais cara que o
manifesto pode ter, porque nada dentro do laço a alcança.

`disabled_stages` é resposta legítima: o repositório que não tem linter
DESLIGA o estágio e não é mais cobrado por ele — a plataforma cobra a
DECISÃO, não o comando.
"""
from __future__ import annotations

import json

from sandbox_runtime import manifest_bootstrap as mb


def _manifesto(**over) -> str:
    base = {
        "version": 1,
        "commands": {"lint": ["ruff", "check", "."], "typecheck": ["mypy", "."],
                     "test": ["pytest"], "build": ["make", "build"]},
    }
    base.update(over)
    return json.dumps(base)


def test_a_missing_gate_command_is_charged():
    faltando = mb.missing_declarations(_manifesto(
        commands={"typecheck": ["mypy", "."], "test": ["pytest"], "build": ["make"]}))
    assert "commands.lint" in faltando, (
        "lint ausente reprova a rodada por NOT_CONFIGURED e o Coder não pode "
        "consertar — se a emenda não cobra, o item morre e alguém abre PR à mão"
    )


def test_every_gate_command_is_covered_not_just_lint():
    faltando = mb.missing_declarations(json.dumps({"version": 1}))
    for chave in ("commands.lint", "commands.typecheck", "commands.test",
                  "commands.build"):
        assert chave in faltando, chave


def test_a_disabled_stage_is_a_legitimate_answer_and_is_not_charged():
    """O repo sem linter DESLIGA o estágio — a plataforma cobra a decisão,
    não o comando."""
    faltando = mb.missing_declarations(_manifesto(
        commands={"typecheck": ["mypy", "."], "test": ["pytest"], "build": ["make"]},
        disabled_stages=["lint"]))
    assert "commands.lint" not in faltando


def test_a_complete_manifest_is_never_nagged():
    assert mb.missing_declarations(_manifesto()) == []


def test_the_reason_travels_so_the_pr_says_why():
    """A PR de emenda explica por que cada chave importa — sem isso o revisor
    recebe um diff sem tese."""
    nomes = [nome for nome, _porque in mb._REQUIRED]
    assert "commands.lint" in nomes
    for _nome, porque in mb._REQUIRED:
        assert porque and len(porque) > 20
