"""A failing gate has to say what it saw — in the right stream, and truthfully.

Three defects, all measured on the Angular testbed, cost two days between them.
Every one of them was a REPORTING defect: each gate's verdict was correct and
each gate's evidence described something else.

1. `_tail(result.stdout or result.stderr)` — `or` discards stderr whenever
   stdout is non-empty, and for a Node toolchain stdout is never empty. One
   `npx jest --ci` run wrote 24,610 lines to stdout (captured `console.*` plus
   the istanbul coverage table) and 7,074 to stderr (the `FAIL` headers and the
   count line). `detail` was 40 lines of the coverage table, every time. No
   value of `_MAX_DETAIL_LINES` could have fixed it: the answer was never in
   the stream being tailed.

2. `_PYTEST_SUMMARY_RE` required `passed` first with `failed` optional after it
   — pytest's order. Jest writes `2 failed, 275 passed`, so the failure count
   was silently dropped and a run with two broken suites published the
   byte-identical summary to a green one: `summary: 275 passed`. That string is
   what `audit_log` keeps and what `_l1_failure_context` hands the next Coder
   turn, so the fix loop was told to repair a suite it was told passed.

3. `detail` was `summary + tail`, never the lines the gate actually counted. A
   `typecheck` failure showed the alphabetical END of a 262-error dump — errors
   in files the change never opened — while the three that failed the gate sat
   in the omitted head. Handed other people's errors, the Coder produced a
   byte-identical diff across four paid rounds.

These tests drive the public check functions with the real output shapes those
runs produced. Each one fails if its fix is reverted — verified by mutation, not
by reading.
"""
from __future__ import annotations

import pytest

from dse_contracts import GateStatus

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import typecheck_check
# aliased: pytest would collect the imported `test_check` itself as a test.
from dse_validation.l1.quality_checks import test_check as run_test_check
from dse_validation.sandbox_exec import ExecResult


class _Stub:
    """Returns one canned result. The gate's parsing is what is under test."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self._result = ExecResult(
            argv=["x"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    def run(self, argv, cwd=None, timeout=300):  # noqa: ARG002 - Protocol shape
        return self._result


#: What jest actually printed for `wi_t1-c6b6fb78`. Counts and FAIL headers go
#: to STDERR; stdout is console noise and the coverage table.
_JEST_FAIL_STDERR = (
    "FAIL src/app/components/homepage/dashboard-list.component.badge.spec.ts\n"
    "  - DashboardList > renders a badge\n"
    "    NullInjectorError: No provider for Store!\n"
    "FAIL src/app/shared/components/report-status-badge.component.spec.ts\n"
    "Test Suites: 2 failed, 275 passed, 277 total\n"
    "Tests:       7 failed, 4981 passed, 4988 total\n"
)
_JEST_PASS_STDERR = (
    "Test Suites: 275 passed, 275 total\nTests:       4975 passed, 4975 total\n"
)
#: Long enough that a 40-line tail of stdout alone contains none of the above.
_COVERAGE_TABLE = "\n".join(f"src/app/f{i}.ts | 91.5 | 76.4 | 88.8 |" for i in range(200))


def _test_cfg() -> L1Config:
    return L1Config(test_cmd=["npx", "jest", "--ci"])


# ---------------------------------------------------------------------------
# Defect 2 — the summary told the opposite of the verdict beside it.
# ---------------------------------------------------------------------------
def test_a_failing_jest_run_names_its_failures_in_the_summary():
    """The regression that cost the two days. `275 passed` beside a FAIL sent
    every investigation at the gate instead of at the broken suite."""
    finding = run_test_check(_Stub(1, _COVERAGE_TABLE, _JEST_FAIL_STDERR), _test_cfg())
    assert finding.status is GateStatus.FAIL
    # Evoluiu com o defeito 5 (2026-08-10): entre os dois rodapés do jest
    # ("Test Suites: 2 failed..." e "Tests: 7 failed..."), o gate passa a
    # publicar o de TESTES — que é o que o laço de conserto precisa saber.
    # Antes vencia o primeiro rodapé com falha, que era o de suítes.
    assert "7 failed" in finding.summary, finding.summary
    assert "4981 passed" in finding.summary


def test_a_failing_run_and_a_green_run_cannot_publish_the_same_summary():
    """States the property directly, so a future parser that happens to satisfy
    the assertion above by luck still cannot reintroduce the bug."""
    failing = run_test_check(_Stub(1, _COVERAGE_TABLE, _JEST_FAIL_STDERR), _test_cfg())
    green = run_test_check(_Stub(0, _COVERAGE_TABLE, _JEST_PASS_STDERR), _test_cfg())
    assert failing.summary != green.summary
    assert green.status is GateStatus.PASS


def test_pytests_own_order_still_reads_correctly():
    """The pattern this replaced was written for pytest and must keep working:
    pytest puts `passed` first, jest puts `failed` first."""
    finding = run_test_check(_Stub(1, "", "== 272 passed, 3 failed in 12.4s =="), _test_cfg())
    assert "3 failed" in finding.summary
    assert finding.status is GateStatus.FAIL


def test_all_tests_green_but_a_nonzero_exit_says_so_explicitly():
    """This repo runs jest with `collectCoverage` and a global threshold, so the
    command can reject a run in which every test passed. Reporting only the pass
    count there is what sends the fix loop hunting a failing test that does not
    exist."""
    finding = run_test_check(_Stub(1, _COVERAGE_TABLE, _JEST_PASS_STDERR), _test_cfg())
    assert finding.status is GateStatus.FAIL
    assert "no test failed" in finding.summary, finding.summary


# ---------------------------------------------------------------------------
# Defect 1 — the evidence came from the stream without the diagnosis in it.
# ---------------------------------------------------------------------------
def test_the_failing_suites_survive_a_flood_of_stdout():
    """stdout is 200 lines of coverage table; the FAIL headers are on stderr.
    Under `stdout or stderr` the detail contained neither suite name."""
    finding = run_test_check(_Stub(1, _COVERAGE_TABLE, _JEST_FAIL_STDERR), _test_cfg())
    assert "dashboard-list.component.badge.spec.ts" in finding.detail
    assert "report-status-badge.component.spec.ts" in finding.detail


def test_a_compiler_error_on_stderr_reaches_the_detail():
    """`ng build` writes its bundle-size table to stdout and its error to
    stderr. The ledger's `build` detail was the size table."""
    finding = typecheck_check(
        _Stub(2, _COVERAGE_TABLE, "src/a.ts(3,9): error TS2322: nope"),
        L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"]),
    )
    assert "TS2322" in finding.detail


# ---------------------------------------------------------------------------
# Defect 3 — `detail` showed the tail, never the lines that failed the gate.
# ---------------------------------------------------------------------------
def test_the_counted_errors_appear_in_the_detail_not_just_the_tail():
    """262 pre-existing errors sort after the one this change introduced, so a
    40-line tail of the output contains every error except the one that
    matters."""
    mine = "src/app/features/dashboard/badge.ts(4,11): error TS2345: mine"
    theirs = "\n".join(
        f"src/zz/other{i}.spec.ts(1,1): error TS2322: not mine" for i in range(200)
    )
    finding = typecheck_check(
        _Stub(2, f"{mine}\n{theirs}", ""),
        L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"]),
        changed_files={"src/app/features/dashboard/badge.ts"},
    )
    assert finding.status is GateStatus.FAIL
    assert "1 type error" in finding.summary
    assert mine in finding.detail, (
        "the gate counted this line and then showed the operator 40 other ones"
    )


def test_a_passing_gate_does_not_pad_its_detail_with_attributed_lines():
    """Scoping means a green gate can still have hundreds of unattributed
    errors; they must not be presented as this change's evidence."""
    theirs = "\n".join(
        f"src/zz/other{i}.spec.ts(1,1): error TS2322: not mine" for i in range(50)
    )
    finding = typecheck_check(
        _Stub(2, theirs, ""),
        L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"]),
        changed_files={"src/app/untouched.ts"},
    )
    assert finding.passed is True
    assert "the 0 line(s)" not in finding.detail


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_neither_stream_is_dropped(stream):
    """The property behind defect 1, stated without reference to a runner."""
    marker = "MARKER-THAT-MUST-SURVIVE"
    kwargs = {"stdout": "", "stderr": ""} | {stream: marker}
    finding = run_test_check(_Stub(1, **kwargs), _test_cfg())
    assert marker in finding.detail


# ---------------------------------------------------------------------------
# Defeito 4 (medido no wi_893de651, 2026-08-10, testbed Java): o surefire
# escreve as falhas como "[ERROR] ..." — COM colchetes — e o extrator de
# evidência só reconhecia FAIL/FAILED/ERROR nus. O detail colapsava para a
# linha-resumo ("summary: Tests run: 1, Failures: 0, Errors: 1") e o Coder
# consertou CEGO por três rodadas pagas (tateou o application-test.yml sem
# nunca ver a exceção real). Mesma família do defeito 3: veredito certo,
# evidência descrevendo outra coisa.
# ---------------------------------------------------------------------------
_SUREFIRE_FAIL_STDOUT = (
    "[INFO] Running com.fintex.bmofeecalculatorbe.BmoFeeCalculatorBeApplicationTests\n"
    "[ERROR] Tests run: 1, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 4.1 s "
    "<<< FAILURE! -- in com.fintex.bmofeecalculatorbe.BmoFeeCalculatorBeApplicationTests\n"
    "[ERROR] contextLoads  Time elapsed: 0.004 s  <<< ERROR!\n"
    "java.lang.IllegalStateException: Failed to load ApplicationContext\n"
    "Caused by: org.springframework.beans.factory.BeanCreationException: "
    "Error creating bean with name 'dataSource'\n"
    "[INFO] Tests run: 1, Failures: 0, Errors: 1, Skipped: 0\n"
    # o mundo real: o mvn imprime um RODAPÉ longo depois das falhas — reactor
    # summary, help links, total time. O tail cru mostra só isso; a evidência
    # ([ERROR] do teste) vive ACIMA, fora do alcance de qualquer tail.
    + "".join(f"[INFO] rodape irrelevante {i}\n" for i in range(300))
    + "[ERROR] BUILD FAILURE\n"
)


def test_a_surefire_failure_reaches_the_detail_with_its_error_lines():
    finding = run_test_check(
        _Stub(1, _SUREFIRE_FAIL_STDOUT, ""),
        L1Config(test_cmd=["mvn", "test"]),
    )
    assert finding.status is GateStatus.FAIL
    assert "[ERROR]" in finding.detail, (
        "o detail tem que carregar as linhas [ERROR] do surefire — sem elas o "
        "fix loop conserta cego (wi_893de651: três rodadas tateando o yml)"
    )
    assert "contextLoads" in finding.detail, (
        "a linha que NOMEIA o teste quebrado é a evidência mínima"
    )


# ---------------------------------------------------------------------------
# Defeito 5 (medido 2026-08-10, e o mais caro do dia): `_test_counts` retorna na
# PRIMEIRA linha com falha em vez de ler o RODAPÉ. Duas consequências opostas,
# ambas com saída real capturada da produção:
#
#   BE (wi_82254f59, surefire): o surefire imprime uma linha por CLASSE antes do
#   total. O gate publicou "Tests run: 1, Failures: 0, Errors: 1" — a primeira
#   classe — quando o total era "Tests run: 2, ... Errors: 2".
#
#   FE (wi_176dfa72, jest): o repo roda com `verbose`, então o NOME de cada
#   teste é impresso. Um teste chamado "...for 403 errors..." casou o contador e
#   o gate publicou "403 errors" sobre uma suíte cujo rodapé real dizia
#   "Tests: 3 failed, 4972 passed, 4975 total".
#
# O operador leu "403 erros" e concluiu que o FE estava longe do verde. Estava a
# TRÊS asserções. Toda instrução entregue ao Coder e todo last_error do dia
# passaram por este parser.
# ---------------------------------------------------------------------------
_SUREFIRE_REAL_OUTPUT = (
    "[ERROR] Tests run: 1, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 19.361 s "
    "<<< FAILURE! - in com.fintex.bmofeecalculatorbe.controller.rest.ReportOptionsControllerTest\n"
    "[ERROR] com.fintex...ReportOptionsControllerTest  Time elapsed: 19.361 s  <<< ERROR!\n"
    "[ERROR] Tests run: 1, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 4.973 s "
    "<<< FAILURE! - in com.fintex.bmofeecalculatorbe.service.AdvisorFeeCalculationServiceTest\n"
    "[ERROR] Errors: \n"
    "[ERROR]   ReportOptionsControllerTest » IllegalState Failed to load ApplicationContext f...\n"
    "[ERROR] Tests run: 2, Failures: 0, Errors: 2, Skipped: 0\n"
    "[ERROR] Failed to execute goal ... surefire ...: There are test failures.\n"
)

_JEST_REAL_VERBOSE_OUTPUT = (
    "  ● GridPayoutComponent › should return false for 403 errors when retrying\n"
    "    expect(received).toBe(expected)\n"
    "  ✓ should map 401 errors to a friendly message (4 ms)\n"
    "FAIL src/app/admin/grid-payout/grid-payout.component.spec.ts (7.877 s)\n"
    "Test Suites: 1 failed, 274 passed, 275 total\n"
    "Tests:       3 failed, 4972 passed, 4975 total\n"
    "Snapshots:   0 total\n"
)


def test_the_surefire_total_wins_over_the_first_class_line():
    finding = run_test_check(
        _Stub(1, _SUREFIRE_REAL_OUTPUT, ""), L1Config(test_cmd=["mvn", "test"]),
    )
    assert finding.status is GateStatus.FAIL
    assert "Tests run: 2" in finding.summary, (
        f"o gate tem que publicar o TOTAL do surefire, nao a primeira classe: {finding.summary!r}"
    )
    assert "Errors: 2" in finding.summary


def test_a_test_name_mentioning_403_errors_is_not_a_count():
    finding = run_test_check(
        _Stub(1, "", _JEST_REAL_VERBOSE_OUTPUT), _test_cfg(),
    )
    assert finding.status is GateStatus.FAIL
    assert "403" not in finding.summary, (
        f"o NOME de um teste virou contagem de falhas: {finding.summary!r} — foi "
        "isto que fez '3 falhas de 4975' ser reportado ao operador como '403 erros'"
    )
    assert "3 failed" in finding.summary and "4972 passed" in finding.summary


def test_the_two_dialects_already_covered_keep_working():
    """PIN: os rodapés que já liam certo continuam lendo certo."""
    jest = run_test_check(_Stub(1, _COVERAGE_TABLE, _JEST_FAIL_STDERR), _test_cfg())
    assert "7 failed" in jest.summary or "2 failed" in jest.summary
    pytest_run = run_test_check(_Stub(1, "", "== 272 passed, 3 failed in 12.4s =="), _test_cfg())
    assert "3 failed" in pytest_run.summary


# ---------------------------------------------------------------------------
# Multi-módulo: o rodapé VERDE maior apagou o módulo que falhou (2026-08-19)
# ---------------------------------------------------------------------------
# Medido no wi_8c26a5e7 (`calculation-engine-service`, reator Maven de vários
# módulos): o módulo verde imprimiu `Tests run: 1141` e o `bootstrap` imprimiu
# `Tests run: 151, Failures: 1, Errors: 7`. A regra "vence o rodapé de maior
# executed" — correta DENTRO de um módulo, onde o total nunca é menor que a
# linha de classe — escolheu o 1141 e o gate publicou "no test failed, but the
# command exited 1: the suite's own policy rejected the run (coverage threshold
# or similar)". Esse texto foi para o audit_log e daí para o próximo turno do
# Coder via _l1_failure_context: ele caçou um problema de cobertura que não
# existe, fez um no-op, e o freio de fingerprint matou o item. A
# não-convergência foi fabricada pelo resumo.
#
# A regra nova: entre rodapés, um que NOMEIA falha vence o verde maior; entre
# os que falham, continua vencendo o de maior executed (o total do módulo ≥
# suas linhas de classe nas duas métricas, então as guardas de 10/08 acima
# continuam valendo por construção). Nunca se soma: linha de classe + total do
# próprio módulo dobrariam a conta.

_SUREFIRE_MULTI_MODULE_RED = (
    # módulo 1 (verde, o maior do reator — o que apagava a falha)
    "[INFO] Results:\n"
    "[INFO] \n"
    "[INFO] Tests run: 1141, Failures: 0, Errors: 0, Skipped: 252\n"
    "[INFO] \n"
    # módulo 2 (bootstrap): linha por classe primeiro, total do módulo depois
    "[ERROR] Tests run: 8, Failures: 1, Errors: 7, Skipped: 0, Time elapsed: 0.173 s "
    "<<< FAILURE! -- in com.fintex.ce.workflow.GitHubActionsWorkflowTest\n"
    "[ERROR] Failures: \n"
    "[ERROR]   GitHubActionsWorkflowTest.testWorkflowFileExists:19 Workflow file should "
    "exist at .github/workflows/ci.yml ==> expected: <true> but was: <false>\n"
    "[ERROR] Tests run: 151, Failures: 1, Errors: 7, Skipped: 5\n"
    "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.2.5:test "
    "(default-test) on project bootstrap: There are test failures.\n"
)


def test_a_failing_module_is_not_erased_by_a_bigger_green_one():
    finding = run_test_check(
        _Stub(1, _SUREFIRE_MULTI_MODULE_RED, ""), L1Config(test_cmd=["mvn", "test"]),
    )
    assert finding.status is GateStatus.FAIL
    assert "Tests run: 151" in finding.summary, (
        f"o módulo que FALHOU tem que aparecer no resumo, não o verde maior: "
        f"{finding.summary!r}"
    )
    assert "Failures: 1" in finding.summary and "Errors: 7" in finding.summary
    assert "coverage threshold" not in finding.summary, (
        "o ramo 'no test failed…' disparou sobre uma suíte com 8 testes "
        "quebrados — é exatamente o diagnóstico errado que o Coder recebeu"
    )


def test_between_failing_footers_the_module_total_still_wins():
    """A guarda de 10/08 (wi_82254f59) reafirmada sob a regra nova: a linha de
    classe (8) e o total do módulo (151) falham ambos — vence o total."""
    finding = run_test_check(
        _Stub(1, _SUREFIRE_MULTI_MODULE_RED, ""), L1Config(test_cmd=["mvn", "test"]),
    )
    assert "Tests run: 8," not in finding.summary


def test_an_all_green_reactor_still_reports_the_biggest_footer():
    """Verde continua verde: sem rodapé falhando, a regra de hoje."""
    verde = (
        "[INFO] Tests run: 1141, Failures: 0, Errors: 0, Skipped: 252\n"
        "[INFO] Tests run: 151, Failures: 0, Errors: 0, Skipped: 5\n"
        "[INFO] BUILD SUCCESS\n"
    )
    finding = run_test_check(_Stub(0, verde, ""), L1Config(test_cmd=["mvn", "test"]))
    assert finding.status is GateStatus.PASS
    assert "Tests run: 1141" in finding.summary


def test_a_genuine_suite_policy_rejection_still_says_so():
    """O ramo 'coverage threshold or similar' existe para o caso real: TODOS os
    rodapés verdes e mesmo assim exit != 0. Ele continua."""
    verde_exit1 = (
        "[INFO] Tests run: 1141, Failures: 0, Errors: 0, Skipped: 252\n"
        "[ERROR] Rule violated for bundle ce: instructions covered ratio is 0.79\n"
    )
    finding = run_test_check(_Stub(1, verde_exit1, ""), L1Config(test_cmd=["mvn", "test"]))
    assert finding.status is GateStatus.FAIL
    assert "coverage threshold or similar" in finding.summary
