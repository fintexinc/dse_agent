"""Um matcher de glob só — porque o terceiro estava errado e decidia um gate.

Auditoria de 2026-08-20: três implementações da mesma pergunta ("este caminho
casa com este padrão?"). Duas byte-equivalentes; a terceira,
`sessions._matches_any`, usava `g.lstrip('*/')` — que remove TODO `*` e `/` do
começo, transformando `**/*payment*` em `payment*`.

O efeito não é cosmético. `_HIGH_RISK_GLOBS` existe para mandar mudança
sensível ao gate humano, e um `process_payment.py` NA RAIZ do repositório saía
`low`: nenhum dos dois ramos casava, o plano era auto-aprovado e ninguém era
perguntado. Arquivo idêntico dentro de um diretório qualquer casava.

A correção é apagar duas cópias e apontar todo mundo para a que já estava certa
(`preview/paths_filter.file_matches_glob`), promovida ao contrato — o mesmo
caminho que `is_test_path` e `first_forbidden_match` já fizeram. As três LISTAS
de globs continuam separadas: elas respondem perguntas diferentes. O que se
compartilha é o primitivo.
"""
from __future__ import annotations

try:  # o vermelho: o primitivo ainda não mora no contrato
    from dse_contracts.paths import file_matches_glob
except ImportError:  # pragma: no cover
    file_matches_glob = None  # type: ignore[assignment]


def _casa(path: str, glob: str) -> bool:
    assert file_matches_glob is not None, (
        "dse_contracts.paths.file_matches_glob não existe — o primitivo segue "
        "duplicado em três lugares, um deles errado"
    )
    return file_matches_glob(path, glob)


def test_a_double_star_glob_matches_at_the_root_too():
    """O bug medido: `**/` significa "em qualquer profundidade, INCLUSIVE
    nenhuma". Sem isto, o arquivo sensível na raiz escapa do gate."""
    assert _casa("process_payment.py", "**/*payment*") is True
    assert _casa("src/billing/process_payment.py", "**/*payment*") is True


def test_it_still_matches_the_ordinary_way():
    assert _casa("migrations/0001.sql", "migrations/*") is True
    assert _casa(".github/workflows/ci.yml", ".github/workflows/*") is True


def test_it_does_not_match_what_it_should_not():
    assert _casa("src/app.py", "**/*payment*") is False
    assert _casa("docs/readme.md", "migrations/*") is False


def test_the_risk_classifier_sends_a_root_payment_file_to_the_human_gate():
    """O consumidor que o bug atingia, ponta a ponta."""
    from sandbox_runtime.sessions import classify_risk_class

    assert classify_risk_class(["process_payment.py"], 40, []) == "high", (
        "arquivo de pagamento na raiz foi classificado como risco baixo — o "
        "plano auto-aprova e nenhum humano é perguntado"
    )
    assert classify_risk_class(["oauth_config.py"], 40, []) == "high"
    assert classify_risk_class(["src/app.py"], 40, []) == "low"


# ---------------------------------------------------------------------------
# Ecossistemas que o DSE roda mas `is_test_path` não conhecia (2026-08-20)
# ---------------------------------------------------------------------------
from dse_contracts.paths import is_test_path  # noqa: E402
# Não é contabilidade: `TesterToolset` recusa escrita fora de caminho de teste
# (`toolsets.py`, ToolPermissionError) e a autoria descarta o arquivo com "test
# path refused". Num repo Ruby o Tester NÃO CONSEGUIA escrever
# `spec/foo_spec.rb` — ponto final, o item morre sem teste nenhum. O mesmo vale
# para .NET (`FooTests.cs`). Três linhas de regex desbloqueiam duas linguagens.

def test_ruby_rspec_layout_is_a_test_path():
    assert is_test_path("spec/models/user_spec.rb") is True
    assert is_test_path("spec/spec_helper.rb") is True
    assert is_test_path("lib/user_spec.rb") is True


def test_dotnet_layout_is_a_test_path():
    assert is_test_path("src/Acme.Domain.Tests/UserTests.cs") is True
    assert is_test_path("UserTest.cs") is True


def test_a_directory_merely_named_like_spec_is_not_swallowed():
    """`specs/` de documentação (OpenAPI, ADR) não é suíte de teste."""
    assert is_test_path("docs/specification.md") is False
    assert is_test_path("src/inspector.py") is False
