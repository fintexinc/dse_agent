"""Um matcher só para `forbidden_paths` — e ele mora no contrato.

Existiam DOIS, e eles DIVERGIAM:

  - `plan_compliance._is_forbidden` (o gate L1) casa SEGMENTO em qualquer
    profundidade: `packages/web/.github/workflows/ci.yml` é violação;
  - `sessions.classify_risk_class` (o classificador de risco) usava
    `startswith`/`fnmatch` ancorado na raiz: o MESMO arquivo saía "low".

Em monorepo a consequência não é cosmética: o plano é classificado baixo, o
gate humano não é acionado (a política só parqueia "high"), e só depois o L1
reprova o diff. Ou seja, o humano nunca foi perguntado sobre o caminho
protegido — a pergunta que este trabalho inteiro existe para fazer.

A promoção para o contrato é a mesma história do `is_test_path`: dois
consumidores em serviços diferentes, uma resposta só.
"""
from __future__ import annotations

try:  # o vermelho: a função ainda não existe
    from dse_contracts.paths import first_forbidden_match
except ImportError:  # pragma: no cover - some o assim que o verde entra
    first_forbidden_match = None  # type: ignore[assignment]

_SHIPPED = [".github/workflows/", "migrations/"]


def _match(path: str, patterns: list[str] | None = None) -> str | None:
    assert first_forbidden_match is not None, (
        "dse_contracts.paths.first_forbidden_match não existe — o matcher "
        "único ainda não foi promovido para o contrato"
    )
    return first_forbidden_match(path, _SHIPPED if patterns is None else patterns)


def test_matches_a_whole_segment_at_any_depth():
    """O caso de monorepo, que hoje os dois matchers respondem diferente."""
    assert _match("packages/web/.github/workflows/ci.yml") == ".github/workflows/"
    assert _match(".github/workflows/ci.yml") == ".github/workflows/"


def test_a_directory_named_like_a_protected_one_is_not_a_match():
    """`migrations_backup/` não é `migrations/`: alcançar mais fundo não pode
    trocar o falso negativo por um falso positivo."""
    assert _match("services/api/migrations_backup/0001.sql") is None


def test_a_leading_slash_pins_the_pattern_to_the_repository_root():
    """Convenção do .gitignore, e a extensão inteira da sintaxe: sem globs."""
    assert _match("packages/web/docs/index.md", ["/docs/"]) is None
    assert _match("docs/index.md", ["/docs/"]) == "/docs/"


def test_a_blank_entry_is_a_typo_never_everything():
    assert _match("qualquer/coisa.py", ["", "   ", "/"]) is None


def test_it_returns_the_pattern_that_matched_not_a_boolean():
    """Quem chama precisa NOMEAR a regra na mensagem que o humano lê."""
    assert _match("migrations/0099.sql") == "migrations/"


def test_it_normalises_the_windows_separator():
    assert _match("packages\\web\\migrations\\0001.sql") == "migrations/"
