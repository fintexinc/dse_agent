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
    assert "2 failed" in finding.summary, finding.summary
    assert "7 failed" in finding.summary or "2 failed" in finding.summary


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
