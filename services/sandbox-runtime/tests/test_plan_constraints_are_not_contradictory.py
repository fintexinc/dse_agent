"""A instrução do Coder não pode se contradizer nem mentir sobre a política.

Medido 2026-08-10 no wi_049e6fb8 (5ª ocorrência do "primeiro turno vazio",
US$ 0,12 e zero arquivos). O que o Coder recebeu:

    - Modify ONLY production code in these files: <migração>, <entidade>,
      <serviço>, <controller>, AdvisorFeeCalculationServiceTest.java,
      ReportOptionsControllerTest.java.
    - Do NOT create or edit TEST files (...). Writing tests is a SEPARATE
      stage (the Tester) — any test change you make is reverted before the
      commit.

Duas coisas erradas na mesma frase:

1. **Contradição.** O `expected_files` do Planner inclui os testes que a
   mudança precisa, e o texto os apresenta como "modifique SOMENTE estes" logo
   antes de proibir tocar em teste. Instrução contraditória é a explicação
   mais simples para um turno que devolve nada — e o custo (US$ 0,12) é o de
   uma resposta curtíssima.

2. **Mentira sobre a política.** "any test change you make is reverted" era
   verdade até 2026-08-10. Naquele dia o Coder ganhou spec de cliente; horas
   depois, com a remoção do reauthor, ganhou TODAS — não existe mais revert de
   teste nenhum. Dizer ao ator que seu trabalho será desfeito é pedir que ele
   não o faça, e foi assim que o item parou três vezes num impasse que ele
   podia resolver.
"""
from __future__ import annotations

from sandbox_runtime.activities import plan_constraints_note

_FILES = [
    "src/main/resources/db/migration/V1__add_retired.sql",
    "src/main/java/com/acme/PayoutLevel.java",
    "src/test/java/com/acme/AdvisorFeeCalculationServiceTest.java",
    "src/test/java/com/acme/ReportOptionsControllerTest.java",
]


def test_the_only_these_files_list_never_contains_test_files():
    note = plan_constraints_note(_FILES)
    files_line = next(ln for ln in note.splitlines() if "these files" in ln)
    assert "PayoutLevel.java" in files_line, "os arquivos de produção seguem lá"
    assert "V1__add_retired.sql" in files_line
    assert "Test.java" not in files_line, (
        "a lista de 'modifique SOMENTE estes' não pode conter arquivos que a "
        "linha seguinte proíbe tocar — contradição é a causa mais simples do "
        "turno vazio medido 5x"
    )


def test_the_policy_line_matches_the_policy_in_force():
    """Não existe mais revert de edição de teste — a nota não pode prometer um.

    Este pin já afirmou duas políticas: "toda edição de teste é revertida" e,
    na rc.76, "só a do instrumento do Tester". As duas foram verdade quando
    escritas. Hoje o pós-turno não desfaz teste nenhum, e uma nota que ainda
    fale em reversão manda o Coder não tentar o que ele pode fazer."""
    note = plan_constraints_note(_FILES)
    low = note.lower()
    assert "are reverted" not in low and "is reverted" not in low, (
        f"a nota promete um revert que não existe mais. Nota: {note!r}"
    )
    assert "nothing reverts" in low, (
        "o silêncio não basta: o Coder foi instruído por semanas de que suas "
        "edições sumiam, então o texto tem que DESFAZER isso explicitamente"
    )
    assert "Tester" in note, "o note continua nomeando de quem é a ETAPA de autoria"


def test_a_plan_with_no_production_file_still_produces_a_usable_note():
    """Plano só com testes: a lista fica vazia, e afirmar 'modifique SOMENTE
    estes' com lista vazia proibiria tudo — a linha some."""
    note = plan_constraints_note(
        ["src/test/java/com/acme/OnlyTest.java"]
    )
    assert "these files" not in note
    assert "Tester" in note, "as demais regras continuam valendo"


def test_the_note_grants_the_permission_the_policy_grants():
    """A nota dizia só o que é PROIBIDO, e um ator que nunca é autorizado não
    exerce permissão nenhuma. A permissão hoje é a mais ampla possível —
    QUALQUER teste que a mudança quebra — e ela precisa estar escrita, com a
    sua contrapartida (a PR vê) e com o julgamento que se espera dele."""
    note = plan_constraints_note(_FILES)
    low = note.lower()
    assert "any test" in low, (
        f"a permissão não está nomeada. Nota: {note!r}"
    )
    assert "pr diff" in low or "pull request" in low, (
        "a permissão vem com a sua contrapartida: a mudança fica visível na PR"
    )
    assert "update the assertion" in low and "fix the code" in low, (
        "o que se pede é JULGAMENTO entre as duas saídas; oferecer só uma "
        "delas é escolher pelo ator"
    )


def test_the_one_rule_without_a_mechanism_behind_it_is_stated_as_such():
    """Apagar cobertura continua proibido — mas agora NADA impede. Com o revert
    fora, essa linha deixou de ser um lembrete de mecanismo e passou a ser a
    única defesa antes da revisão humana; ela tem que estar no imperativo."""
    low = plan_constraints_note(_FILES).lower()
    assert "never delete, skip or empty a test" in low
    assert "weaken" in low, (
        "apagar não é o único jeito de enfraquecer: afrouxar a asserção tem "
        "que ser nomeado, porque é o que sobrou de barato para o ator fazer"
    )
