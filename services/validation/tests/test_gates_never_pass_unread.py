"""Um gate que não conseguiu LER a ferramenta não pode dizer PASS.

Auditoria de 2026-08-20, reproduzida na máquina em 6 dialetos. Três gates
publicam verde sem ter avaliado nada:

  - `lint` e `typecheck`: com o diff em mãos, o exit code é descartado DE
    PROPÓSITO (a pergunta é "esta mudança introduziu problema?", não "o
    repositório está limpo"). Correto — mas o parser só conhece o formato
    `path:line:col: CODE msg` do ruff/flake8 (lint) e o do mypy/tsc
    (typecheck), e só lê stdout. Ferramenta de outro dialeto, ou que escreve no
    stderr, produz ZERO linhas reconhecidas → zero problemas → PASS, com a
    ferramenta tendo saído 1.
  - `sast`: `json.loads(result.stdout or "{}")`. Um bandit morto pelo OOM
    killer não imprime nada; `or "{}"` transforma isso em zero findings → PASS
    num gate de SEGURANÇA que não escaneou uma linha. O comentário logo acima
    já nomeava o risco.

A regra que estes testes fixam: **ausência de evidência não é evidência de
ausência**. Quando o parser não reconhece NENHUMA linha e a ferramenta saiu
diferente de zero, o veredito honesto é `ERROR` — não FAIL.

Por que ERROR e não FAIL: FAIL entra em `failed_checks` e compra um turno de
Coder. Num repo com dívida de formatação pré-existente, é um turno pago que
NENHUM ator do laço consegue fechar — exatamente o incidente que o filtro por
arquivos-mudados existe para impedir. ERROR entra em `_l1_infra_gates` e escala
nomeando o estágio, que é o que um humano precisa ler.
"""
from __future__ import annotations

from dse_contracts import GateStatus

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import lint_check, typecheck_check
from dse_validation.l1.sast import sast_check
from dse_validation.sandbox_exec import ExecResult

_TOCADO = {"src/main/java/A.java"}


class _Stub:
    """Um resultado enlatado por comando. O parsing é o que está sob teste."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self._r = ExecResult(argv=["x"], returncode=returncode, stdout=stdout, stderr=stderr)

    def run(self, argv, cwd=None, timeout=300):  # noqa: ARG002 - Protocol shape
        return self._r


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

_SPOTLESS = (
    "> Task :spotlessJavaCheck FAILED\n"
    "The following files had format violations:\n"
    "    src/main/java/A.java\n"
)


def test_lint_that_rejected_the_tree_without_a_parseable_line_is_an_error():
    f = lint_check(_Stub(1, _SPOTLESS), L1Config(lint_cmd=["./gradlew", "spotlessCheck"]), _TOCADO)
    assert f.status is GateStatus.ERROR, (
        f"a ferramenta saiu 1 e o gate disse {f.status}: {f.summary!r}"
    )
    assert f.passed is False


def test_lint_reads_stderr_too():
    """ruff escreve diagnóstico no stderr quando o stdout está tomado; o `or`
    que descartava stderr já custou dois dias (#60) em outro gate."""
    f = lint_check(
        _Stub(1, "", "src/main/java/A.java:12:5: E501 line too long"),
        L1Config(lint_cmd=["ruff", "check", "."]), {"src/main/java/A.java"},
    )
    assert f.status is GateStatus.FAIL, f.summary
    assert "1 lint issue" in f.summary


def test_lint_with_issues_only_outside_the_change_still_passes():
    """A REDE: dívida pré-existente do repositório não é desta mudança. Este é
    o comportamento que o conserto não pode quebrar."""
    f = lint_check(
        _Stub(1, "other/legacy.py:3:1: F401 unused"),
        L1Config(lint_cmd=["ruff", "check", "."]), _TOCADO,
    )
    assert f.passed is True and f.status is GateStatus.PASS


def test_a_clean_lint_still_passes():
    f = lint_check(_Stub(0, ""), L1Config(lint_cmd=["ruff", "check", "."]), _TOCADO)
    assert f.passed is True and f.status is GateStatus.PASS


# ---------------------------------------------------------------------------
# typecheck
# ---------------------------------------------------------------------------

def test_typecheck_that_failed_without_a_parseable_line_is_an_error():
    saida = "# github.com/acme/svc\n./main.go:12:2: undefined: foo\n"
    f = typecheck_check(_Stub(1, "", saida), L1Config(typecheck_cmd=["go", "vet", "./..."]), _TOCADO)
    assert f.status is not GateStatus.PASS, (
        f"go vet saiu 1 com diagnóstico no stderr e o gate disse PASS: {f.summary!r}"
    )
    assert f.passed is False


def test_typecheck_with_errors_only_outside_the_change_still_passes():
    f = typecheck_check(
        _Stub(1, "other/legacy.ts(4,1): error TS2345: bad"),
        L1Config(typecheck_cmd=["tsc", "--noEmit"]), _TOCADO,
    )
    assert f.passed is True and f.status is GateStatus.PASS


# ---------------------------------------------------------------------------
# sast — o `or "{}"`
# ---------------------------------------------------------------------------

class _SastStub:
    """Responde ao probe de `.py` com um arquivo, e ao bandit com o enlatado."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self._bandit = ExecResult(argv=["bandit"], returncode=returncode,
                                  stdout=stdout, stderr=stderr)

    def run(self, argv, cwd=None, timeout=300):  # noqa: ARG002
        if argv and argv[0] == "sh":
            return ExecResult(argv=list(argv), returncode=0, stdout="./app.py\n", stderr="")
        return self._bandit


def test_a_bandit_killed_by_the_oom_killer_is_not_a_clean_scan():
    f = sast_check(_SastStub(137, "", ""), L1Config())
    assert f.status is GateStatus.ERROR, (
        f"bandit morto (137) com stdout vazio virou {f.status}: {f.summary!r} — "
        "PASS aqui é um gate de segurança aprovando o que não leu"
    )
    assert f.passed is False


def test_a_broken_pod_exec_is_not_a_clean_scan():
    f = sast_check(_SastStub(1, "", "unable to upgrade connection: pod does not exist"), L1Config())
    assert f.status is GateStatus.ERROR, f.summary
    assert f.passed is False


def test_a_real_bandit_run_still_reports_normally():
    """A rede: bandit que RODOU continua sendo lido como sempre."""
    f = sast_check(_SastStub(0, '{"results": [], "metrics": {}}'), L1Config())
    assert f.passed is True and f.status is GateStatus.PASS


def test_the_modern_ruff_arrow_format_is_read():
    """Descoberto pelo próprio conserto (2026-08-20): o ruff instalado NESTE
    repositório imprime o formato `full`, com a localização numa linha de seta.
    O gate não falava esse dialeto — antes virava verde falso; sem esta leitura
    passaria a escalar TODO repo Python. Um ERROR honesto expõe o dialeto que
    falta; um PASS o esconde."""
    saida = (
        "F401 [*] `os` imported but unused\n"
        " --> src/app.py:1:8\n"
        "  |\n"
        "1 | import os\n"
        "  |        ^^\n"
        "help: Remove unused import: `os`\n"
        "\nFound 1 error.\n"
    )
    f = lint_check(_Stub(1, saida), L1Config(lint_cmd=["ruff", "check", "."]),
                   {"src/app.py"})
    assert f.status is GateStatus.FAIL, f.summary
    assert "1 lint issue" in f.summary


def test_the_arrow_format_still_respects_the_changed_files_filter():
    """A normalização existe para isto: a comparação com o diff acontece no
    começo da linha, e a forma de seta começa em `-->`."""
    saida = "F401 unused\n --> other/legacy.py:1:8\n"
    f = lint_check(_Stub(1, saida), L1Config(lint_cmd=["ruff", "check", "."]), _TOCADO)
    assert f.passed is True, f"dívida fora do diff virou problema desta mudança: {f.summary!r}"
