"""O que o Coder LÊ tem que conceder o que a política concede.

Decisão de operador de 2026-08-10: spec de cliente é editável pelo DSE — a mudança
entra no diff da PR, onde o revisor a vê. O mecanismo acompanhou na mesma data
(`workspace_hygiene.revert_test_edits` deixou de apagar edição de spec de cliente e
passou a proteger só o INSTRUMENTO do Tester).

O texto não acompanhou. Medido em 2026-08-10 (auditoria multi-agente): o Coder nunca é
autorizado em lugar nenhum, e o que ele lê POR ÚLTIMO — posição deliberada, para ser a
coisa mais fresca antes de agir (`workflows.py`, `_agent_instruction`) — é:

    _tester_failure_context : "do not weaken or delete the tests"
    _l1_failure_context     : "Fix these, and change nothing else"

Um agente que lê "não enfraqueça nem apague os testes" e "não mude mais nada" não vai
atualizar uma asserção obsoleta, por mais que a política permita. É a explicação mais
simples para o item ter parado três vezes num impasse que ele tinha permissão de
resolver.

O que estes testes exigem: a instrução distingue os dois casos em vez de proibir os
dois. Instrumento do Tester continua intocável; spec de cliente que o diff quebrou é
trabalho — julgar se a asserção ficou obsoleta (atualizar) ou se o código novo está
errado (consertar).
"""
from __future__ import annotations

from dse_orchestrator.workflows import WorkItemLifecycleWorkflow as _WF


class _TesterResult:
    """A forma que `_tester_failure_context` lê (duck-typed, como em produção)."""

    def __init__(self, *, returncode: int = 1, failure_output: str = "",
                 status=None, suite_hung: bool = False):
        self.returncode = returncode
        self.failure_output = failure_output
        self.status = status
        self.suite_hung = suite_hung


class _Finding:
    def __init__(self, check: str, detail: str):
        self.check = check
        self.detail = detail
        self.passed = False
        self.status = None


def test_the_tester_failure_note_does_not_forbid_what_policy_allows():
    notes = _WF._tester_failure_context(
        _TesterResult(failure_output="FAIL src/app/x.spec.ts\nexpect(a).toBe(b)\n")
    )
    text = "\n".join(notes)
    assert "do not weaken or delete the tests" not in text, (
        "proibição ABSOLUTA: desde 2026-08-10 atualizar spec de CLIENTE é permitido, e "
        "esta frase é a última coisa que o Coder lê antes de agir"
    )
    assert "tester" in text.lower(), (
        "a proibição que SOBREVIVE precisa ser nomeada: o instrumento do Tester"
    )
    assert "never delete" in text.lower(), (
        "a permissão é para ATUALIZAR asserção obsoleta, nunca para apagar cobertura"
    )


def test_the_l1_failure_note_does_not_say_change_nothing_else():
    notes = _WF._l1_failure_context(
        [_Finding("test", "FAIL src/app/grid-payout.component.spec.ts")]
    )
    text = "\n".join(notes)
    assert "change nothing else" not in text, (
        "'change nothing else' proíbe justamente a edição de spec que a política "
        "concede — e chega ao Coder junto com a evidência da spec quebrada"
    )
    assert "test" in text and "grid-payout" in text, "a evidência continua chegando"


def test_an_infra_ending_still_forbids_rewriting_tests():
    """PIN: quando a suíte foi MORTA pelo runtime (não reprovou por asserção),
    reescrever teste continua sendo a coisa errada — não há veredito para atender."""
    notes = _WF._tester_failure_context(
        _TesterResult(returncode=137, failure_output="killed")
    )
    text = "\n".join(notes)
    assert "do not rewrite tests" in text, (
        "ending de infra não é desacordo teste-vs-código; aqui a proibição continua"
    )
