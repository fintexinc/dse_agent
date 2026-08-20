"""Defect B (diagnosed 2026-08-07, wi_dc571c08/wi_866b96ce + manual probe): the
`test` gate approved the ABSENCE of evidence. `passed` was the exit code alone,
`_test_counts` only decorated the summary — and was blind to Surefire's dialect
(`Tests run: 1, Failures: 0`), so a green Java run read "no test count found" —
and the branch that literally says "no test count found" still stamped PASS on
exit 0. On the Java testbed the gate's own command excludes the repo's only
test class, so a pristine tree passed with ZERO tests executed.

Red before the fix. The fix: `_test_counts` returns NUMBERS (executed/failed)
across pytest, jest and surefire dialects, and `passed` requires exit 0 AND
executed > 0. Declared absence (empty test_cmd -> NOT_CONFIGURED, pinned by
test_empty_commands_are_not_configured_never_green) stays the only escape.
"""
from __future__ import annotations

from pathlib import Path

from dse_contracts import GateStatus

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import _test_counts
from dse_validation.l1.quality_checks import test_check as run_test_check
from dse_validation.sandbox_exec import ExecResult


class _CannedSandbox:
    def __init__(self, result: ExecResult):
        self._result = result

    def run(self, argv, timeout=None):  # noqa: ARG002 - signature parity
        return self._result


def _canned(stdout: str, returncode: int) -> _CannedSandbox:
    return _CannedSandbox(
        ExecResult(argv=["x"], returncode=returncode, stdout=stdout, stderr="")
    )


# Captured verbatim from a real `./mvnw ... test` run on the Java testbed
# (bmo-fee-calculator-be-dse, sandbox Pod agent-runner:v0.1.0-rc.31, 2026-08-07).
_SUREFIRE_GREEN = """\
[INFO] -------------------------------------------------------
[INFO]  T E S T S
[INFO] -------------------------------------------------------
[INFO] Running com.fintex.bmofeecalculatorbe.SmokeProbeTest
[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.093 s - in com.fintex.bmofeecalculatorbe.SmokeProbeTest
[INFO]
[INFO] Results:
[INFO]
[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0
[INFO]
[INFO] ------------------------------------------------------------------------
[INFO] BUILD SUCCESS
[INFO] ------------------------------------------------------------------------
"""

_SUREFIRE_RED = "[ERROR] Tests run: 5, Failures: 2, Errors: 1, Skipped: 1\n"

# Surefire ran classes but every test in them was skipped: nothing executed.
_SUREFIRE_ALL_SKIPPED = "[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 3\n"


def test_surefire_green_counts_are_recognized_as_numbers():
    counts = _test_counts(_SUREFIRE_GREEN)
    assert counts is not None
    assert counts.executed == 1
    assert counts.failed == 0


def test_surefire_failures_and_errors_both_count_as_failed():
    counts = _test_counts(_SUREFIRE_RED)
    assert counts is not None
    assert counts.failed == 3, "2 failures + 1 error"
    assert counts.executed == 4, "5 run - 1 skipped"


def test_pytest_and_jest_dialects_still_parse_as_numbers():
    py = _test_counts("272 passed, 3 failed\n")
    assert (py.executed, py.failed) == (275, 3)
    je = _test_counts("Tests: 2 failed, 275 passed, 277 total\n")
    assert (je.executed, je.failed) == (277, 2)


def test_exit_zero_with_no_count_anywhere_is_not_a_pass():
    """The old branch said "no test count found in the output" and stamped PASS
    anyway. A gate that approves the absence of evidence is not a gate.

    O que este teste defende — NÃO PASSA — é o que sempre defendeu. O status
    saiu de FAIL para ERROR na rc.107 e a diferença importa: aqui ninguém
    conseguiu LER a execução, e FAIL, no ledger e no `_l1_failure_context`,
    diz ao próximo turno de Coder que o diff quebrou os testes. Ele persegue um
    assert que não existe até o teto. `executed == 0` sobre relatório legível
    continua FAIL (test_exit_zero_with_zero_tests_executed_is_not_a_pass)."""
    finding = run_test_check(_canned("", 0), L1Config(test_cmd=["./mvnw", "test"]))
    assert finding.passed is False
    assert finding.status == GateStatus.ERROR
    assert "could not be read" in finding.summary
    assert "reports.junit" in finding.summary


def test_exit_zero_with_zero_tests_executed_is_not_a_pass():
    finding = run_test_check(
        _canned(_SUREFIRE_ALL_SKIPPED, 0), L1Config(test_cmd=["./mvnw", "test"])
    )
    assert finding.passed is False
    assert finding.status == GateStatus.FAIL


def test_a_real_surefire_green_run_passes_with_evidence():
    finding = run_test_check(_canned(_SUREFIRE_GREEN, 0), L1Config(test_cmd=["./mvnw", "test"]))
    assert finding.passed is True
    assert finding.status == GateStatus.PASS


def test_a_red_run_without_counts_still_names_the_exit_code():
    """Idem: não passa, e o código de saída continua visível. O status é ERROR
    porque a leitura falhou — não porque um teste falhou."""
    finding = run_test_check(_canned("boom, no runner summary", 1), L1Config(test_cmd=["./mvnw", "test"]))
    assert finding.passed is False
    assert finding.status == GateStatus.ERROR
    assert "exit 1" in finding.summary


def test_the_full_real_testbed_green_log_yields_evidence_and_pass():
    """DoD 3 — the ENTIRE captured log of a green `./mvnw ... test` run on the
    Java testbed (sandbox Pod, 2026-08-07, 1456 lines including the dependency
    download flood), not a hand-picked excerpt: the legitimate path stays green
    under the evidence rule, with tests_run > 0 recognized from Surefire."""
    log = (Path(__file__).parent / "fixtures" / "testbed_java_green_mvnw_test.txt").read_text()
    counts = _test_counts(log)
    assert counts is not None
    assert counts.executed == 1
    assert counts.failed == 0
    finding = run_test_check(_canned(log, 0), L1Config(test_cmd=["./mvnw", "test"]))
    assert finding.passed is True
    assert finding.status == GateStatus.PASS
