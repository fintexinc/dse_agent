"""O gate de teste adivinhava o dialeto do runner pelo stdout — e reprovava
suíte verde em toda linguagem que ele não conhecia.

`_test_counts` sabe dois dialetos: o par `N passed` (pytest/jest) e o espelho do
Surefire. Fora deles `counts is None`, e a regra de evidência
(`passed = result.ok and counts.executed > 0`) transforma isso em FAIL:

    go test ./...      ok  example/pkg  0.002s        -> FAIL, verde
    cargo test         test result: ok. 5 passed      -> FAIL, verde
    dotnet test        Passed: 5                      -> FAIL, verde
    rspec              5 examples, 0 failures         -> FAIL, verde
    phpunit            OK (5 tests)                   -> FAIL, verde

Um repositório Go correto NUNCA passa no L1. E o veredito não é só errado: FAIL
diz "o seu diff quebrou os testes", então o laço de fix queima turnos de Coder
atrás de um assert que nunca falhou, até escalar no teto.

A saída não é uma regex por linguagem — é parar de ler prosa. Todo runner emite
JUnit XML (pytest --junitxml, jest-junit, surefire nativo, cargo nextest,
dotnet --logger junit, rspec formatter, phpunit --log-junit), e o repositório
declara onde o relatório cai. As regexes de dialeto viram fallback.

E o veredito de quando ninguém consegue ler muda de FAIL para ERROR: "não
consegui ler" é problema de configuração, com dono e conserto próprios; não é
uma acusação contra o diff. `executed == 0` continua FAIL — aí houve evidência
de verdade, e ela diz que nada rodou.
"""
from __future__ import annotations

from dse_contracts import GateStatus

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import test_check as run_test_check
from dse_validation.sandbox_exec import ExecResult


class _Sandbox:
    """Fake que distingue o comando de teste da LEITURA do relatório — o gate
    passa a fazer dois execs, e um canned único esconderia justamente isso."""

    def __init__(self, *, suite: ExecResult, reports: str = ""):
        self._suite = suite
        self._reports = reports
        self.calls: list[list[str]] = []

    def run(self, argv, timeout=None):  # noqa: ARG002 - paridade de assinatura
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if "find ." in joined:
            return ExecResult(argv=argv, returncode=0, stdout=self._reports, stderr="")
        return self._suite


def _cfg(*, junit: str | None = "target/surefire-reports/*.xml") -> L1Config:
    payload = {"version": 1, "commands": {"test": ["go", "test", "./..."]}}
    if junit is not None:
        payload["reports"] = {"junit": junit}
    return L1Config._from_manifest_payload(payload, source="test")


def _xml(*, tests: int, failures: int = 0, errors: int = 0, skipped: int = 0,
         name: str = "example") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuites><testsuite name="{name}" tests="{tests}" '
        f'failures="{failures}" errors="{errors}" skipped="{skipped}" '
        'time="0.5"></testsuite></testsuites>\n'
    )


def _run(*, returncode: int, stdout: str, reports: str = "", junit="target/x/*.xml"):
    sandbox = _Sandbox(
        suite=ExecResult(argv=["go", "test"], returncode=returncode,
                         stdout=stdout, stderr=""),
        reports=reports,
    )
    return run_test_check(sandbox, _cfg(junit=junit)), sandbox


_GO_GREEN = "ok  \texample/pkg\t0.002s\n"


def test_a_green_go_suite_passes_when_the_repo_declares_its_report():
    finding, _sb = _run(returncode=0, stdout=_GO_GREEN, reports=_xml(tests=5))

    assert finding.status is GateStatus.PASS
    assert finding.passed is True
    assert "5" in finding.summary


def test_a_red_rspec_suite_fails_with_the_numbers_the_report_carries():
    finding, _sb = _run(returncode=1, stdout="5 examples, 2 failures\n",
                        reports=_xml(tests=5, failures=2))

    assert finding.status is GateStatus.FAIL
    assert "2" in finding.summary


def test_the_report_is_authoritative_over_a_stdout_count():
    """Contagem de rodapé é prosa; o relatório é o que o runner registrou. Onde
    discordam, o arquivo ganha."""
    finding, _sb = _run(returncode=0, stdout="Tests:  1 passed, 1 total\n",
                        reports=_xml(tests=42))

    assert finding.status is GateStatus.PASS
    assert "42" in finding.summary


def test_a_suite_that_executed_nothing_still_fails():
    """A defesa do defeito B continua: gate que aprova a ausência de evidência
    não é gate. Aqui houve leitura — e ela diz que nada executou."""
    finding, _sb = _run(returncode=0, stdout=_GO_GREEN,
                        reports=_xml(tests=3, skipped=3))

    assert finding.status is GateStatus.FAIL
    assert finding.passed is False


def test_nobody_could_read_it_is_an_error_not_an_accusation():
    """Sem relatório declarado e com stdout que nenhum dialeto entende, o gate
    dizia FAIL — que o laço de fix lê como "o seu diff quebrou os testes" e
    paga turnos de Coder atrás de um assert que não existe."""
    finding, _sb = _run(returncode=0, stdout=_GO_GREEN, junit=None)

    assert finding.status is GateStatus.ERROR
    assert finding.passed is False
    assert "reports.junit" in finding.summary + finding.detail


def test_a_red_run_nobody_can_read_is_an_error_too():
    finding, _sb = _run(returncode=1, stdout="cargo: something went sideways\n",
                        junit=None)

    assert finding.status is GateStatus.ERROR
    assert "reports.junit" in finding.summary + finding.detail


def test_a_declared_report_that_produced_no_file_falls_back_to_stdout():
    """Relatório declarado mas ausente não é veredito: o runner pode não ter
    chegado a escrevê-lo. Se o stdout for legível, ele decide."""
    finding, _sb = _run(returncode=0, stdout="Tests:  4 passed, 4 total\n",
                        reports="")

    assert finding.status is GateStatus.PASS
    assert "4" in finding.summary


def test_the_gate_only_reads_reports_when_the_repo_declares_them():
    _finding, sb = _run(returncode=0, stdout="Tests:  4 passed, 4 total\n", junit=None)

    assert not any("find ." in " ".join(a) for a in sb.calls), (
        "sem declaração não há exec extra: o gate não sai procurando arquivo"
    )
