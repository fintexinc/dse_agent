"""Os modelos de entrada das Activities são OS DO CONTRATO — não cópias locais.

Custou uma PR inteira em produção (2026-08-11, wi_780a530d, US$ 3,87): L1 PASS,
L2 PASS, o trabalho pronto, e o item morreu no último passo com

    activity_retries_exhausted:finalize_pr:
    AttributeError: 'FinalizePrInput' object has no attribute 'files_changed'

O campo tinha sido acrescentado ao contrato em `dse_contracts.activities`, o
workflow passou a enviá-lo, e `_finalize_pr` passou a lê-lo. Mas a Activity
decodifica com uma CÓPIA LOCAL do modelo, definida neste mesmo arquivo — e a
cópia não tinha o campo. O payload chegou, o pydantic descartou o extra, e o
acesso ao atributo estourou.

O que torna isso digno de um teste em vez de um remendo: **o aviso já estava
escrito, quatro linhas acima da classe culpada**, desde um incidente idêntico
em 2026-07-22 —

    "a local shadow fell behind, missing work_item_id/base_sha — AttributeError
     in production while the contract tests passed against the canonical model.
     Never redefine contract models locally."

Um comentário não impede a próxima cópia. Este teste impede: ele compara os
CAMPOS, então uma sombra que nasça amanhã já nasce vermelha, e um campo novo no
contrato falha aqui antes de falhar em produção.

Escopo honesto: `RunL2ReviewInput` continua sendo uma cópia local, e por isso
está na lista de exceções abaixo com o motivo. Ela tem campos que o contrato
não tem (`iteration`, `l1_passed`), então unificá-la é uma decisão de contrato,
não uma limpeza — e fazê-la de carona numa correção de produção seria o mesmo
tipo de atalho que produziu este incidente.
"""
from __future__ import annotations

import pytest
from dse_contracts import activities as canonical

from dse_validation import activities as local

#: Modelos que a Activity decodifica e que TÊM de ser o contrato, sem cópia.
_MUST_BE_CANONICAL = ("FinalizePrInput", "ConsumeCiStatusInput", "RunL1PipelineInput")

#: A exceção, com o motivo — nunca uma lista muda.
_KNOWN_LOCAL = {
    "RunL2ReviewInput": (
        "tem `iteration` e `l1_passed`, que o contrato não tem; unificar é "
        "decisão de contrato, não limpeza"
    ),
}


@pytest.mark.parametrize("name", _MUST_BE_CANONICAL)
def test_the_activity_decodes_with_the_contract_model(name: str):
    assert getattr(local, name) is getattr(canonical, name), (
        f"{name} é uma CÓPIA local do modelo de contrato. Foi assim que a PR do "
        f"wi_780a530d morreu depois de passar L1 e L2: o workflow mandou um "
        f"campo novo, a cópia não o tinha, e `inp.<campo>` estourou em produção "
        f"com a suíte verde. Importe de `dse_contracts.activities`."
    )


def test_no_new_shadow_appears_unnoticed():
    """Qualquer classe redefinida nos dois lugares tem de estar declarada como
    exceção, COM motivo. Uma sombra nova nasce vermelha aqui."""
    import ast
    import pathlib

    src = pathlib.Path(local.__file__).read_text(encoding="utf-8")
    local_classes = {
        node.name for node in ast.parse(src).body if isinstance(node, ast.ClassDef)
    }
    canonical_names = {
        node.name
        for node in ast.parse(
            pathlib.Path(canonical.__file__).read_text(encoding="utf-8")
        ).body
        if isinstance(node, ast.ClassDef)
    }
    shadows = (local_classes & canonical_names) - set(_KNOWN_LOCAL)
    assert not shadows, (
        f"sombra(s) nova(s) do contrato: {sorted(shadows)}. Ou importe de "
        "`dse_contracts.activities`, ou declare em `_KNOWN_LOCAL` com o motivo."
    )


def test_the_field_that_broke_production_survives_the_decode():
    """O caso literal, no ponto exato onde ele quebrou: o payload que o workflow
    manda, decodificado pelo modelo que a Activity usa."""
    inp = local.FinalizePrInput(
        work_item_id="wi_780a530d",
        tenant_id="fintex-poc",
        repo="fintexinc/bmo-fee-calculator-fe-dse",
        branch="dse/wi_780a530d",
        base_branch="main",
        summary="retire payout levels",
        files_changed=["src/app/x.component.ts", "src/app/x.component.spec.ts"],
    )
    assert inp.files_changed == [
        "src/app/x.component.ts",
        "src/app/x.component.spec.ts",
    ], "o campo não sobreviveu ao decode — é exatamente o AttributeError de produção"
