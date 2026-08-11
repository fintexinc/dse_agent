"""A edição de teste tem que estar VISÍVEL para quem revisa a PR.

Em 2026-08-10 o DSE ganhou permissão para alterar qualquer teste, e no mesmo
dia perdeu todo mecanismo que a continha: o revert pós-turno, o oráculo de
autoria, o rename guard e o parque. A supervisão, disse a decisão, "muda de
lugar: passa a ser o diff da PR".

Só que uma linha de teste alterada no meio de um diff de 300 linhas não é
supervisão — é sorte. E o que está em jogo é o invariante que o CLAUDE.md
declara: *o DSE nunca aprova o próprio trabalho*. Se o Coder pode afrouxar a
asserção que o julga e ninguém aponta para isso, o gate humano continua
existindo no papel e não na prática. O L2 é vácuo e não existe detector de
spec enfraquecida — então este aviso é, hoje, a única coisa que faz a
supervisão ter lugar.

O que ele NÃO é: um bloqueio. Editar teste é trabalho legítimo e o próprio
sistema pede isso ao Coder. O aviso só garante que o revisor comece a leitura
sabendo onde olhar.
"""
from __future__ import annotations

from dse_validation.github.pr_finalizer import touched_tests_notice


def test_it_names_the_test_files_the_item_touched():
    notice = touched_tests_notice([
        "src/app/fee.service.ts",
        "src/app/fee.service.spec.ts",
        "src/test/java/com/acme/FeeTest.java",
    ])
    assert "fee.service.spec.ts" in notice
    assert "FeeTest.java" in notice
    assert "fee.service.ts" not in notice.replace("fee.service.spec.ts", ""), (
        "arquivo de produção não entra no aviso — ele já é o assunto da PR"
    )


def test_it_says_what_the_reviewer_is_being_asked_to_check():
    """Nomear sem dizer o que fazer com o nome é ruído. O revisor precisa saber
    QUE PERGUNTA responder — a asserção ficou mais fraca?"""
    low = touched_tests_notice(["src/app/fee.service.spec.ts"]).lower()
    assert "review" in low or "check" in low
    assert "weaken" in low or "weaker" in low, (
        "a pergunta é sobre enfraquecimento; 'este item mexeu em testes' "
        "sozinho não pede nada a ninguém"
    )


def test_no_test_touched_no_notice():
    """PIN: aviso que aparece sempre vira cabeçalho, e cabeçalho não é lido."""
    assert touched_tests_notice(["src/app/fee.service.ts", "README.md"]) == ""
    assert touched_tests_notice([]) == ""
    assert touched_tests_notice(None) == ""


def test_a_long_list_is_capped_but_says_it_was():
    """Truncar em silêncio é a mesma doença do `_tail`: o revisor concluiria
    que são 10 arquivos quando são 40."""
    many = [f"src/app/f{i}.spec.ts" for i in range(40)]
    notice = touched_tests_notice(many)
    assert "40" in notice, "a contagem real tem que aparecer"
    assert len(notice) < 2000


def test_the_pr_body_carries_the_notice():
    """A fronteira que importa: o aviso chega ao CORPO da PR, não só a um log."""
    from dse_validation.github.pr_finalizer import PR_BODY_TEMPLATE

    body = PR_BODY_TEMPLATE.format(
        work_item_id="wi_x", risk_class="low", summary="s",
        evidence_url="u", issue_link="",
        touched_tests_notice=touched_tests_notice(["src/app/fee.service.spec.ts"]),
    )
    assert "fee.service.spec.ts" in body
