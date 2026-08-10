"""O comando de teste do cliente exclui teste pelo NOME — e ninguém era avisado.

Medido em 2026-08-10 no bmo-fee-calculator-be-dse. O `.dse/validation.json` do
cliente roda:

    ./mvnw -B test -Dtest='!BmoFeeCalculatorBeApplicationTests' -DfailIfNoTests=false

`BmoFeeCalculatorBeApplicationTests` é o ÚNICO teste do repositório — um
`@SpringBootTest` com `contextLoads()`. Ou seja: o repo exclui exatamente a
prova de que o contexto Spring não sobe naquele ambiente. O DSE não sabia
disso, e o Tester (seguindo uma skill do próprio cliente cujo exemplo é
`@WebMvcTest(ReportOptionsController.class)`) escreveu testes de contexto que
NÃO estão na exclusão. Resultado: `Failed to load ApplicationContext`, duas
reescritas automáticas e um cap inteiro no wi_3355102d.

Não é nosso consertar o repo do cliente — decisão do operador, e ela continua
valendo. É nosso PARAR DE DESCOBRIR ISSO POR DEDUÇÃO a cada rodada: quando o
comando exclui teste por nome, o gate diz isso.

A fronteira do audit_log é respeitada aqui e é o motivo do segundo teste: o
NOME do teste excluído é conteúdo do repositório do cliente e fica no `detail`
(validation_runs, que a retenção limpa). Para o `audit_log`, que é append-only
e nunca é limpo, vai só a frase da plataforma e a CONTAGEM.
"""
from __future__ import annotations

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import _test_exclusions
from dse_validation.l1.quality_checks import test_check as run_test_check
from dse_validation.sandbox_exec import ExecResult

_MAVEN_REAL = [
    "sh", "-c",
    "./mvnw -B -Dmaven.compiler.release=17 test "
    "-Dtest='!BmoFeeCalculatorBeApplicationTests' -DfailIfNoTests=false",
]

_SUREFIRE_OUT = """\
[INFO] Tests run: 2, Failures: 0, Errors: 0, Skipped: 0
[INFO] BUILD SUCCESS
"""


class _Canned:
    def __init__(self, result):
        self._result = result

    def run(self, argv, timeout=None):  # noqa: ARG002 — paridade de assinatura
        return self._result


def _sandbox(stdout: str, returncode: int = 0):
    return _Canned(ExecResult(argv=["x"], returncode=returncode, stdout=stdout, stderr=""))


def test_the_maven_bang_exclusion_is_detected():
    assert _test_exclusions(_MAVEN_REAL) == ["BmoFeeCalculatorBeApplicationTests"]


def test_the_other_ecosystems_exclusions_are_detected():
    jest = ["npx", "jest", "--testPathIgnorePatterns", "legacy/.*\\.spec\\.ts"]
    pytest_cmd = ["pytest", "--ignore=tests/slow", "--deselect", "tests/x.py::test_y"]
    assert _test_exclusions(jest) == ["legacy/.*\\.spec\\.ts"]
    assert _test_exclusions(pytest_cmd) == ["tests/slow", "tests/x.py::test_y"]


def test_a_command_without_exclusions_says_nothing():
    """PIN: nenhum aviso onde não há exclusão — um aviso que aparece sempre é
    ruído, e ruído é ignorado justamente na rodada em que importava."""
    assert _test_exclusions(["npx", "jest", "--ci"]) == []
    assert _test_exclusions(["./mvnw", "-B", "test"]) == []
    assert _test_exclusions([]) == []


def test_the_detail_names_the_excluded_test():
    finding = run_test_check(_sandbox(_SUREFIRE_OUT), L1Config(test_cmd=_MAVEN_REAL))

    assert "BmoFeeCalculatorBeApplicationTests" in finding.detail, (
        "o nome é o que permite ao humano decidir; ele vai no detail, que vive "
        f"em validation_runs. Detail: {finding.detail[:300]!r}"
    )
    assert "exclu" in finding.detail.lower() or "exclud" in finding.detail.lower()


def test_the_summary_carries_the_count_but_never_the_customers_name():
    """audit_log é append-only e é copiado verbatim para o console: um valor
    escrito ali pode ser rotacionado, nunca apagado. Nome de teste do cliente
    não entra."""
    finding = run_test_check(_sandbox(_SUREFIRE_OUT), L1Config(test_cmd=_MAVEN_REAL))

    assert "BmoFeeCalculatorBeApplicationTests" not in finding.summary, (
        "conteúdo do repositório do cliente não pode entrar no audit_log"
    )
    assert "1" in finding.summary and "exclu" in finding.summary.lower(), (
        f"a contagem e o fato têm que aparecer. Summary: {finding.summary!r}"
    )


def test_the_advisory_does_not_change_the_verdict():
    """PIN: isto REPORTA, não reprova. Excluir teste é escolha do cliente."""
    finding = run_test_check(_sandbox(_SUREFIRE_OUT), L1Config(test_cmd=_MAVEN_REAL))
    assert finding.passed is True
