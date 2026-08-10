"""O evento não pode acusar o Tester por código que o Coder escreveu.

Medido no wi_957b9aad (2026-08-10). O turno do Tester terminou com
`outcome=typecheck_failed, returncode=2`, e o `failure_output` era, inteiro:

    src/app/admin/grid-payout/grid-payout.component.ts(311,5): error TS2322: ...
    src/app/admin/grid-payout/store/reducers/grid-payouts.reducer.ts(135,41): TS2345
    src/app/admin/grid-payout/store/reducers/grid-payouts.reducer.ts(165,21): TS18048
    src/app/components/.../fee-schedule-calculations.service.ts(313,11): TS6133

Quatro erros, QUATRO arquivos de produção, zero specs. O ledger registrou isso
como `tester_failed_retrying` (tentativas 2 e 3) e, no teto, o item morreria
como `tester_retry_cap_exhausted`.

O mecanismo está certo — incrementa `coder_retry_count` e devolve a rodada ao
Coder. Quem mente é o NOME, e o nome é o que um humano lê para decidir onde
está o gargalo. Esta sessão registrou "o Tester é o gargalo" em memória
justamente lendo eventos assim; parte daquela conclusão pode ter sido o rótulo.

Fronteira que este teste fixa: a acusação segue os CAMINHOS citados na saída.
Só produção → o Coder. Qualquer spec citada → segue Tester, porque aí o turno
do Tester realmente pode ser a causa e classificar a menos seria pior.
"""
from __future__ import annotations

from dse_orchestrator.workflows import _failure_blames_the_coder

_FE_REAL = (
    "src/app/admin/grid-payout/grid-payout.component.ts(311,5): error TS2322: Type "
    "'(number | undefined)[]' is not assignable to type 'number[]'.\n"
    "src/app/admin/grid-payout/store/reducers/grid-payouts.reducer.ts(135,41): error "
    "TS2345: Argument of type 'number | undefined' is not assignable.\n"
    "src/app/admin/grid-payout/store/reducers/grid-payouts.reducer.ts(165,21): error "
    "TS18048: 'payout.levels' is possibly 'undefined'.\n"
    "src/app/components/edit-page/customize-schedule-page/select-template/services/"
    "fee-schedule-calculations.service.ts(313,11): error TS6133: "
    "'filterActivePayoutLevels' is declared but its value is never read.\n"
    "command terminated with exit code 2\n"
)


def test_typecheck_errors_only_in_production_files_blame_the_coder():
    assert _failure_blames_the_coder(_FE_REAL) is True, (
        "todos os quatro erros estão em arquivo de produção; chamar isso de "
        "falha do Tester é o ledger ensinando a conclusão errada"
    )


def test_one_spec_in_the_output_is_enough_to_keep_the_tester_named():
    mixed = _FE_REAL + (
        "src/app/admin/grid-payout/store/reducers/grid-payouts.reducer.dse.spec.ts"
        "(12,3): error TS2554: Expected 2 arguments, but got 1.\n"
    )
    assert _failure_blames_the_coder(mixed) is False, (
        "com uma spec citada, o turno do Tester pode ser a causa — não "
        "reclassificar é a escolha conservadora"
    )


def test_output_without_any_recognizable_path_keeps_the_old_name():
    """Sem caminho nenhum não há o que atribuir: preserva o comportamento
    anterior em vez de chutar."""
    assert _failure_blames_the_coder("Killed\ncommand terminated with exit code 137") is False
    assert _failure_blames_the_coder("") is False
    assert _failure_blames_the_coder(None) is False


def test_a_spec_in_a_non_dse_convention_still_counts_as_a_spec():
    """A convenção do cliente também é spec: `*.spec.ts` sem o marcador `-dse`
    é teste do repositório, e ele mantém o Tester na conversa."""
    out = "src/app/x/thing.spec.ts(4,1): error TS2322: nope\n"
    assert _failure_blames_the_coder(out) is False
