"""Um gate que ERROU diz o que rodou, com que exit, e quanto cada stream escreveu.

O `lint exit=2` fantasma (revalidação do wi_b95a1d0b, 2026-08-31) custou uma
hora de forense e ficou irreproduzível: o `detail` do finding trazia o cabeçalho
"exited 2 and printed no diagnostic" e o tail — VAZIO — e nada dizia se a
ferramenta escreveu nada ou se a saída se perdeu (um `kubectl exec` morto
devolve rc −1/127 com o stderr do kubectl, não o do gate). "Saída vazia" e
"saída perdida" eram indistinguíveis.

A evidência já persiste em `validation_runs.findings[*].detail` (a retenção
não a toca) — e NÃO no `audit_log`, que é append-only e inexpurgável
(`test_what_the_gate_saw_never_reaches_the_ledger` pina isso de propósito). O
que faltava era o cabeçalho de fatos da execução.
"""
from __future__ import annotations

import pytest

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import build_check, lint_check, typecheck_check
# `test_check` importado com esse nome seria COLETADO pelo pytest como teste.
from dse_validation.l1.quality_checks import test_check as run_test_check
from dse_validation.sandbox_exec import ExecResult


class _Mudo:
    """A ferramenta saiu com exit 2 sem escrever um byte em stream nenhum."""

    def __init__(self, argv_visto: list):
        self._visto = argv_visto

    def run(self, argv, timeout=None):  # noqa: ARG002
        self._visto.append(list(argv))
        return ExecResult(argv=list(argv), returncode=2, stdout="", stderr="")


@pytest.mark.parametrize(
    "gate, cfg_kwargs, chamada",
    [
        ("lint", {"lint_cmd": ["npx", "eslint", "--cache"]},
         lambda ex, cfg: lint_check(ex, cfg, {"src/a.ts"})),
        ("typecheck", {"typecheck_cmd": ["npx", "tsc", "--noEmit"]},
         lambda ex, cfg: typecheck_check(ex, cfg, {"src/a.ts"})),
        ("test", {"test_cmd": ["npm", "test"]},
         lambda ex, cfg: run_test_check(ex, cfg, {"src/a.ts"})),
        ("build", {"build_cmd": ["npm", "run", "build"]},
         lambda ex, cfg: build_check(ex, cfg, {"src/a.ts"})),
    ],
)
def test_an_errored_gate_names_the_argv_the_exit_code_and_what_each_stream_wrote(
    gate, cfg_kwargs, chamada
):
    visto: list = []
    finding = chamada(_Mudo(visto), L1Config(**cfg_kwargs))

    assert finding.check == gate and finding.passed is False
    detail = finding.detail
    assert " ".join(visto[0]) in detail, "o argv que RODOU (não o declarado)"
    assert "exit=2" in detail
    assert "stdout=0 bytes" in detail and "stderr=0 bytes" in detail, (
        "sem isto, 'a ferramenta não escreveu' e 'a saída se perdeu' são a mesma frase"
    )
