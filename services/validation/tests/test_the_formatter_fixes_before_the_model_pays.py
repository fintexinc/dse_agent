"""Quando o lint reprova e o repo sabe consertar, o conserto vem antes do modelo.

O passo é determinístico e roda no sandbox, sobre o diff que o DSE acabou de
escrever, usando o comando que o REPOSITÓRIO declarou. Nenhum LLM decide nada
aqui — e é essa a diferença entre 7 segundos e quatro turnos de Coder.

Três propriedades que os testes abaixo pinam, todas aprendidas em produção:

  - o conserto só roda quando `lint` é o que reprovou. Rodar um formatador
    porque o `test` falhou seria mexer no código do cliente sem relação alguma
    com o veredito;
  - se o comando não mudou nada, o turno de Coder acontece como sempre. Um
    formatador que roda e não altera arquivo nenhum significa que a reprovação
    NÃO era de formatação, e insistir seria um laço infinito barato em vez de
    um caro;
  - o resultado é sempre RE-VALIDADO. O formatador conserta o que ele conhece,
    e o gate continua sendo quem diz se acabou — jamais o comando que acabou de
    editar os arquivos.
"""
from __future__ import annotations

from dse_validation.config import L1Config
from dse_validation.l1.autofix import lint_autofix


class _Sandbox:
    """Fake do sandbox: registra o que rodou e diz se o diff mudou."""

    def __init__(self, *, mudou: bool, rc: int = 0):
        self._mudou = mudou
        self._rc = rc
        self.rodados: list[list[str]] = []

    def run(self, argv, cwd=None, timeout=None):  # noqa: ARG002
        from dse_validation.sandbox_exec import ExecResult

        self.rodados.append(list(argv))
        joined = " ".join(argv)
        if "diff" in joined:
            saida = "M\tsrc/Foo.java\n" if self._mudou else ""
            return ExecResult(argv=argv, returncode=0, stdout=saida, stderr="")
        return ExecResult(argv=argv, returncode=self._rc, stdout="", stderr="")


def _cfg(fix=("./mvnw", "-B", "-q", "spotless:apply")) -> L1Config:
    payload = {"version": 1, "commands": {"lint": ["./mvnw", "spotless:check"]}}
    if fix:
        payload["commands"]["lint_fix"] = list(fix)
    return L1Config._from_manifest_payload(payload, source="test")


def test_it_runs_the_command_the_repo_declared():
    sandbox = _Sandbox(mudou=True)
    r = lint_autofix(sandbox, _cfg(), failed_checks=["lint"])

    assert r.ran is True and r.changed is True
    assert ["./mvnw", "-B", "-q", "spotless:apply"] in sandbox.rodados


def test_a_repo_without_the_command_pays_the_model_as_before():
    sandbox = _Sandbox(mudou=True)
    r = lint_autofix(sandbox, _cfg(fix=None), failed_checks=["lint"])

    assert r.ran is False
    assert sandbox.rodados == [], "sem declaração, nada roda"


def test_only_a_lint_failure_triggers_it():
    """Rodar um formatador porque o `test` falhou seria editar o código do
    cliente sem relação com o veredito."""
    sandbox = _Sandbox(mudou=True)
    r = lint_autofix(sandbox, _cfg(), failed_checks=["test", "build"])

    assert r.ran is False
    assert sandbox.rodados == []


def test_a_fix_that_changes_nothing_hands_the_turn_back_to_the_model():
    """Formatador que roda e não altera arquivo significa que a reprovação NÃO
    era de formatação. Insistir viraria um laço infinito barato."""
    sandbox = _Sandbox(mudou=False)
    r = lint_autofix(sandbox, _cfg(), failed_checks=["lint"])

    assert r.ran is True and r.changed is False


def test_a_failing_formatter_never_blocks_the_loop():
    """O conserto é oportunista: se o comando do repo quebra, o turno de Coder
    acontece como sempre. Ele nunca é a razão de um item parar."""
    sandbox = _Sandbox(mudou=False, rc=2)
    r = lint_autofix(sandbox, _cfg(), failed_checks=["lint"])

    assert r.ran is True and r.changed is False
    assert "exited 2" in r.detail, "o que aconteceu tem de ficar legível no ledger"
