"""A ordem de reauthor escreve NO CAMINHO ORDENADO — nunca numa cópia `-dse`.

Medido em produção 2026-08-10 (wi_8b083140, DUAS vezes): o humano clicou
Reauthor; a ordem nomeou
`.../fee-schedule-calculations.service.spec.ts`; e o
`tester_spec_reauthored` registrou como sucesso a escrita de
`.../fee-schedule-calculations.service-dse.spec.ts`. O arquivo quebrado ficou
intacto, uma cópia nova nasceu ao lado, e o clique seguinte criaria `-dse2`.
Decisão humana sem efeito, para sempre.

A causa é o guard de renomeação, criado para o Tester NUNCA destruir teste do
cliente — regra certa no caso geral. Mas quando um humano ORDENA a reescrita
daquele caminho exato, a ordem vale mais que a heurística de autoria: é a
invariante `ordem >= autoria` que o repositório já declara para as specs do
Tester, e a decisão de operador de 2026-08-10 (specs de cliente são editáveis)
estende ao resto. O guard continua valendo para toda escrita NÃO ordenada.
"""
from __future__ import annotations

from sandbox_runtime.activities import _TesterContext, _write_paths_for_authoring


def _ctx() -> _TesterContext:
    return _TesterContext(
        package_json="{}",
        example_test="# exemplo",
        existing_tests={
            "src/app/x/fee.service.spec.ts",
            "src/app/x/outra.spec.ts",
        },
        diff="",
        skills_note="",
        workspace_dir=None,
        reference_spec="",
    )


def test_an_ordered_path_is_written_in_place(tmp_path):
    """O caminho ordenado é escrito COMO ORDENADO, mesmo existindo e mesmo não
    sendo do DSE — foi um humano que mandou."""
    paths = _write_paths_for_authoring(
        ["src/app/x/fee.service.spec.ts"], _ctx(),
        reauthor_ordered=["src/app/x/fee.service.spec.ts"],
    )
    assert paths == ["src/app/x/fee.service.spec.ts"], (
        "a ordem humana tem que reescrever o arquivo NOMEADO — a cópia -dse "
        "deixa o quebrado no lugar e torna o clique inútil (wi_8b083140, 2x)"
    )


def test_a_path_nobody_ordered_still_gets_the_rename_guard():
    """PIN: sem ordem, o guard continua protegendo o teste do cliente."""
    paths = _write_paths_for_authoring(
        ["src/app/x/outra.spec.ts"], _ctx(), reauthor_ordered=[],
    )
    assert paths == ["src/app/x/outra-dse.spec.ts"], (
        "escrita não ordenada num teste existente do cliente continua "
        "renomeando — é o guard do issue #1, que segue valendo"
    )


def test_an_unordered_path_is_renamed_even_while_another_is_ordered():
    """A autorização é POR CAMINHO, não um interruptor global do turno."""
    paths = _write_paths_for_authoring(
        ["src/app/x/fee.service.spec.ts", "src/app/x/outra.spec.ts"], _ctx(),
        reauthor_ordered=["src/app/x/fee.service.spec.ts"],
    )
    assert paths == [
        "src/app/x/fee.service.spec.ts",
        "src/app/x/outra-dse.spec.ts",
    ]
