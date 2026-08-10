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
   verdade até a decisão de operador de 2026-08-10: hoje o Coder PODE editar
   spec de cliente (a edição entra no diff da PR); o que o pós-turno reverte é
   só o INSTRUMENTO do Tester. Dizer ao ator que seu trabalho será desfeito é
   pedir que ele não o faça.
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
    """Desde 2026-08-10 o revert protege só o instrumento do TESTER."""
    note = plan_constraints_note(_FILES)
    assert "reverted" not in note.lower() or "tester" in note.lower(), (
        "não afirmar que TODA edição de teste é revertida — hoje só a das "
        "specs que o Tester autorou é"
    )
    assert "Tester" in note, "o note continua nomeando de quem é a etapa"


def test_a_plan_with_no_production_file_still_produces_a_usable_note():
    """Plano só com testes: a lista fica vazia, e afirmar 'modifique SOMENTE
    estes' com lista vazia proibiria tudo — a linha some."""
    note = plan_constraints_note(
        ["src/test/java/com/acme/OnlyTest.java"]
    )
    assert "these files" not in note
    assert "Tester" in note, "as demais regras continuam valendo"


def test_the_note_grants_the_permission_the_policy_grants():
    """A nota dizia só o que é PROIBIDO. Desde a decisão de operador de
    2026-08-10 o Coder PODE atualizar spec de cliente — e um ator que nunca é
    autorizado não exerce a permissão. Medido: o item parou três vezes num
    impasse que ele tinha permissão de resolver."""
    note = plan_constraints_note(_FILES)
    low = note.lower()
    assert "customer" in low or "pre-existing" in low, (
        "a nota precisa NOMEAR a spec do cliente como editável — hoje ela só "
        "fala do que é proibido"
    )
    assert "pr diff" in low or "pull request" in low, (
        "a permissão vem com a sua contrapartida: a mudança fica visível na PR"
    )
    assert "tester" in low, "a fronteira que sobrevive continua nomeada"
