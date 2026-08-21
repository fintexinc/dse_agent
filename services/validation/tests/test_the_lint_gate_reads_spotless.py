"""O spotless finalmente rodou — e o gate não soube ler o que ele disse.

Cronologia completa do lint do calculation-engine, porque ela responde à
pergunta "o que quebrou?":

  até rc.103   spotless morria na rede em ~7s e o gate publicava PASS
               (o exit code era descartado quando havia diff) — o lint desse
               repositório NUNCA verificou nada.
  rc.104       o falso PASS morre: exit!=0 sem diagnóstico legível vira ERROR.
  rc.108/109   a JVM aprende o proxy e os DOIS hosts do P2 entram na lista.
  01:50 de hoje (wi_6d1e0f5fc7e): o lint levou 16,46s — rodou de verdade pela
               primeira vez — e reprovou a FORMATAÇÃO do teste que o próprio
               DSE escreveu (ordem de imports). Veredito legítimo. Mas o gate
               só fala ruff/eslint/tsc, então classificou como "não consegui
               ler" e ESCALOU como infra — em vez de FAIL, que compra o turno
               de Coder que arruma a formatação e deixa o laço se curar.

O dialeto do spotless-maven é um diff por arquivo:

    [ERROR] ...spotless-maven-plugin:2.43.0:check ... The following files had format violations:
    [ERROR]     src/test/java/com/.../PortfolioCalculationControllerMetricsTest.java
    [ERROR]         @@ -1,22 +1,23 @@
    [ERROR]         +
    [ERROR]          import·org.springframework...
    [ERROR] Run 'mvn spotless:apply' to fix these violations.

Duas armadilhas que estes testes pinam além do dialeto:

  - o caminho é relativo ao MÓDULO (`src/test/...`), e o diff do item é
    relativo ao repositório (`rest-adapter/src/test/...`). Casamento exato
    contra `changed_files` descartaria a violação NOSSA como "de outro
    arquivo" — e o gate passaria por cima do veredito que acabou de aprender
    a ler.
  - os nomes dos arquivos vêm ANTES dos diffs, e o detail guarda o `_tail` da
    saída — numa saída longa o tail corta justamente os nomes. O caminho tem
    de sobreviver no summary/issue line, não depender do tail.
"""
from __future__ import annotations

from dse_contracts import GateStatus
from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import lint_check
from dse_validation.sandbox_exec import ExecResult


class _Sandbox:
    def __init__(self, result: ExecResult):
        self._r = result

    def run(self, argv, cwd=None, timeout=None):  # noqa: ARG002
        return self._r


_ARQUIVO_REPO = ("rest-adapter/src/test/java/com/fintex/ce/adapter/rest/"
                 "controller/PortfolioCalculationControllerMetricsTest.java")
_ARQUIVO_MODULO = ("src/test/java/com/fintex/ce/adapter/rest/"
                   "controller/PortfolioCalculationControllerMetricsTest.java")

_SPOTLESS_RED = f"""\
[ERROR] Failed to execute goal com.diffplug.spotless:spotless-maven-plugin:2.43.0:check (default-cli) on project ce: The following files had format violations:
[ERROR]     {_ARQUIVO_MODULO}
[ERROR]         @@ -1,22 +1,23 @@
[ERROR]         ·import·org.junit.jupiter.api.Test;
[ERROR]         +
[ERROR]          import·org.springframework.beans.factory.annotation.Autowired;
[ERROR]          import·org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
[ERROR]          import·org.springframework.http.MediaType;
[ERROR]          import·org.springframework.test.web.servlet.MockMvc;
[ERROR] Run 'mvn spotless:apply' to fix these violations.
[ERROR] -> [Help 1]
"""

_SPOTLESS_NETWORK_DEAD = (
    "[ERROR] Failed to execute goal com.diffplug.spotless:spotless-maven-plugin:"
    "2.43.0:check (default-cli) on project ce: Execution default-cli of goal "
    "com.diffplug.spotless:spotless-maven-plugin:2.43.0:check failed: "
    "java.io.IOException: Failed to load eclipse jdt formatter\n"
)


def _cfg() -> L1Config:
    return L1Config._from_manifest_payload(
        {"version": 1, "commands": {"lint": ["./mvnw", "-B", "-q", "spotless:check"]}},
        source="test")


def _run(stdout: str, changed):
    sandbox = _Sandbox(ExecResult(argv=["x"], returncode=1, stdout=stdout, stderr=""))
    return lint_check(sandbox, _cfg(), changed_files=changed)


def test_a_format_violation_is_a_fail_that_buys_the_fixing_turn():
    finding = _run(_SPOTLESS_RED, {_ARQUIVO_REPO, "outro/arquivo.java"})

    assert finding.status is GateStatus.FAIL, (
        "veredito legítimo de formatação não é 'infra': ERROR escala e para o "
        "item; FAIL compra o turno de Coder que conserta a formatação"
    )
    assert finding.passed is False
    assert "1 lint issue" in finding.summary


def test_the_violated_file_is_named_by_its_repo_relative_path():
    """O spotless imprime o caminho relativo ao módulo; o Coder abre arquivos
    pelo caminho do repositório. A linha sintetizada carrega o caminho do
    diff — é ele que sobrevive ao _tail e chega ao turno seguinte."""
    finding = _run(_SPOTLESS_RED, {_ARQUIVO_REPO})

    assert _ARQUIVO_REPO in finding.detail
    assert "spotless:apply" in finding.detail.lower()


def test_a_violation_in_a_file_this_change_never_touched_does_not_fail_it():
    """A regra de sempre (`_only_in_changed_files`) vale para o dialeto novo:
    dívida de formatação pré-existente não é deste item."""
    finding = _run(_SPOTLESS_RED, {"src/main/java/com/fintex/ce/Outro.java"})

    assert finding.passed is True
    assert finding.status is GateStatus.PASS
    assert "elsewhere in the repository" in finding.summary


def test_two_violated_files_are_two_issues():
    segundo = _SPOTLESS_RED.replace(
        "[ERROR] Run 'mvn spotless:apply'",
        "[ERROR]     src/main/java/com/fintex/ce/adapter/rest/OutroController.java\n"
        "[ERROR]         @@ -3,4 +3,4 @@\n"
        "[ERROR] Run 'mvn spotless:apply'", 1)
    changed = {_ARQUIVO_REPO,
               "rest-adapter/src/main/java/com/fintex/ce/adapter/rest/OutroController.java"}
    finding = _run(segundo, changed)

    assert finding.status is GateStatus.FAIL
    assert "2 lint issue" in finding.summary


def test_diff_body_lines_are_never_mistaken_for_files():
    """As linhas do corpo do diff (`+`, `·import·...`, `@@`) não podem virar
    'arquivo violado': cada uma geraria um issue fantasma e o count mentiria."""
    finding = _run(_SPOTLESS_RED, {_ARQUIVO_REPO})

    assert "1 lint issue" in finding.summary, finding.summary


def test_the_network_death_is_still_an_error_not_a_fail():
    """A distinção da rc.104 fica de pé: spotless que morreu SEM verificar nada
    segue como ERROR (infra, escala nomeando o estágio) — só a violação real
    virou FAIL."""
    finding = _run(_SPOTLESS_NETWORK_DEAD, {_ARQUIVO_REPO})

    assert finding.status is GateStatus.ERROR
    assert "could not be read" in finding.summary
