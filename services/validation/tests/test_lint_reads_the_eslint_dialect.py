"""O gate de lint aprende o dialeto do ESLint — o formatter padrão do JS.

O parser canônico entende `path:line:col: CODE msg` (ruff, flake8, tsc) e o
spotless aprendeu por síntese (rc.110). O ESLint, que é o lint de fato de todo
repositório JS/TS, imprime em `stylish`: o CAMINHO numa linha, os problemas
indentados abaixo dela. O DSE nunca tinha lido isso porque os testbeds eram
Java e Angular-com-ruff; o glide-path-planner-93 é o primeiro TS real.

Sem este dialeto, um erro introduzido pelo item faz o eslint sair !=0 sem uma
linha que o parser reconheça — e a cláusula de ilegibilidade transforma isso
em ERROR (escala, ninguém conserta) em vez de FAIL (o Coder conserta o que
acabou de escrever).

WARNING não conta como issue, e isso é fidelidade, não leniência: o ESLint sai
0 com warnings, o CI deste repo os declara dívida conhecida (236 no `apps/api`
hoje), e contá-los faria o item pagar por debt de qualquer arquivo que
encostasse. `error` reprova; `warning` não.

Formatos capturados de uma execução real (`npx eslint apps/api`, 2026-08-25).
"""
from __future__ import annotations

from dse_validation.l1.quality_checks import _eslint_violations

_STYLISH = """
/workspace/apps/api/src/health/health.controller.ts
  12:5   error    'unused' is defined but never used  @typescript-eslint/no-unused-vars
  68:1   warning  This line has a length of 101. Maximum allowed is 100  @stylistic/max-len

/workspace/apps/api/src/other.ts
  3:1  error  Unexpected console statement  no-console

✖ 3 problems (2 errors, 1 warning)
"""


def test_the_stylish_dialect_becomes_canonical_lines():
    linhas = _eslint_violations(_STYLISH, None)

    assert any(ln.startswith("apps/api/src/health/health.controller.ts:12:5:") for ln in linhas), (
        f"o erro do ESLint não virou linha canônica: {linhas}"
    )
    assert any("no-console" in ln for ln in linhas), "a regra é o que ensina o conserto"


def test_warnings_are_not_issues_the_repo_itself_exits_zero_on_them():
    linhas = _eslint_violations(_STYLISH, None)
    assert not any("max-len" in ln for ln in linhas), (
        "warning virou issue — o item passaria a pagar pelos 236 warnings "
        "pré-existentes de qualquer arquivo que encostasse"
    )
    assert len(linhas) == 2, linhas


def test_a_clean_run_yields_nothing():
    assert _eslint_violations("", None) == []
    assert _eslint_violations("✖ 0 problems (0 errors, 0 warnings)\n", None) == []


def test_paths_resolve_against_the_diff_like_spotless_does():
    """O ESLint imprime caminho ABSOLUTO do pod (/workspace/...); o diff do
    item é relativo ao repositório. Sem resolver, o escopo descartaria a
    violação NOSSA como 'de outro arquivo'."""
    linhas = _eslint_violations(_STYLISH, {"apps/api/src/other.ts"})
    assert any(ln.startswith("apps/api/src/other.ts:3:1:") for ln in linhas), linhas


def test_output_that_is_not_eslint_is_left_alone():
    """O dialeto só compete quando é do ESLint — nunca inventa issue sobre a
    saída de outra ferramenta."""
    assert _eslint_violations("BUILD FAILURE\n  at com.acme.Foo\n", None) == []
