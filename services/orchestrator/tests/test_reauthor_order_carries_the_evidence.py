"""A ordem de reescrita tem que levar o MOTIVO, não a cauda do log.

Medido no wi_3355102d (2026-08-10). O detail do gate `test` tinha 4.329 chars.
`reauthor_context` monta a ordem com `detail[-1500:]` — os ÚLTIMOS 1500 — e o
que sobrou foi:

    ApplicationContext no detail inteiro?  sim
    ApplicationContext nos ultimos 1500?   nao
    [ERROR]            nos ultimos 1500?   nao

O Tester recebeu a ordem "reescreva estas specs" acompanhada de 1500 caracteres
da lista `Unconditional classes:` do relatório de auto-configuração do Spring.
Nunca soube que o contexto não carregava. Reescreveu no mesmo estilo, DUAS
vezes (as duas rodadas automáticas do F3), e bateu na mesma parede — o
resultado esperado de quem não recebeu a causa.

É a mesma doença do `_tail(stdout or stderr)` do #60, do outro lado do cano:
`_detail_with` (quality_checks.py) foi escrito EXATAMENTE para pôr as linhas
que o gate contou na FRENTE, e o corte por `[-1500:]` joga fora essa cabeça.

O que este teste fixa: o trecho que viaja na ordem contém a evidência atribuída
quando ela existe, e continua caindo na cauda quando o texto não tem cabeça
(o contexto do Tester, onde o output vem por último, não pode regredir).
"""
from __future__ import annotations

from dse_orchestrator.workflows import _reauthor_evidence

# Recorte fiel do detail real do wi_3355102d: summary, bloco atribuído com as
# linhas que o gate contou, e a cauda gigante do relatório do Spring.
_SPRING_NOISE = "\n".join(
    f"    org.springframework.boot.autoconfigure.Filler{i}AutoConfiguration"
    for i in range(120)
)
_REAL_DETAIL = (
    "summary: Tests run: 2, Failures: 0, Errors: 2, Skipped: 0\n"
    "--- the 20 line(s) this gate counted ---\n"
    "[ERROR] Tests run: 1, Failures: 0, Errors: 1, Skipped: 0 <<< FAILURE! - in "
    "com.fintex.bmofeecalculatorbe.controller.rest.ReportOptionsControllerTest\n"
    "[ERROR]   ReportOptionsControllerTest » IllegalState Failed to load ApplicationContext f...\n"
    "[ERROR]   AdvisorFeeCalculationServiceTest » IllegalState Failed to load ApplicationCont...\n"
    "--- raw output (tail) ---\n"
    "Exclusions:\n-----------\n" + _SPRING_NOISE + "\nUnconditional classes:\n" + _SPRING_NOISE
)


def test_the_order_carries_the_lines_the_gate_counted():
    excerpt = _reauthor_evidence(_REAL_DETAIL, 1500)

    assert "ApplicationContext" in excerpt, (
        "sem a causa, o Tester reescreve no mesmo estilo e bate na mesma "
        "parede — foi o que custou as duas rodadas automáticas do wi_3355102d"
    )
    assert "ReportOptionsControllerTest" in excerpt, "a spec culpada tem que ser nomeável"
    assert "Unconditional classes" not in excerpt, (
        "o relatório de auto-configuração do Spring é o ruído que ESTAVA "
        "ocupando os 1500 caracteres"
    )
    assert len(excerpt) <= 1500


def test_the_summary_survives_even_when_the_counted_block_is_long():
    """O summary é a única linha que diz QUANTOS falharam; ele não pode ser
    empurrado para fora pelo próprio bloco atribuído."""
    long_block = "\n".join(f"[ERROR] failure number {i}" for i in range(400))
    detail = (
        "summary: Tests run: 400, Failures: 400, Errors: 0, Skipped: 0\n"
        "--- the 400 line(s) this gate counted ---\n" + long_block +
        "\n--- raw output (tail) ---\ntail noise"
    )

    excerpt = _reauthor_evidence(detail, 1500)

    assert excerpt.startswith("summary: Tests run: 400"), excerpt[:80]
    assert len(excerpt) <= 1500


def test_without_a_counted_block_the_tail_is_still_the_evidence():
    """PIN de não-regressão: `_tester_failure_context` põe a política na frente
    e o output do runner NO FIM. Ali a cauda é a evidência certa, e o conserto
    de um call site não pode estragar o outro."""
    policy = "The test suite you must satisfy is FAILING.\n" + ("- rule\n" * 200)
    detail = policy + "Exit code: 2\nOutput:\nsrc/x.ts(3,1): error TS2322: nope\n"

    excerpt = _reauthor_evidence(detail, 1500)

    assert "error TS2322" in excerpt, "aqui a evidência está no fim; ela tem que sobreviver"
    assert len(excerpt) <= 1500


def test_a_short_detail_travels_whole():
    detail = "summary: 1 failed\n--- the 1 line(s) this gate counted ---\n[ERROR] boom"
    assert _reauthor_evidence(detail, 1500) == detail


def test_empty_detail_is_not_a_crash():
    assert _reauthor_evidence("", 1500) == ""
    assert _reauthor_evidence(None, 1500) == ""
