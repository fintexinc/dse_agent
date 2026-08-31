"""O lint julga só o diff — e agora também pode RODAR só sobre o diff.

Medido no glide-path (7 rodadas do mesmo prompt): `npm run lint` varria o
monorepo inteiro em ~113s por volta e o gate DESCARTAVA na chegada tudo que
não fosse dos arquivos tocados — o filtro é pós-execução. O trabalho era
provadamente jogado fora, a cada volta.

O repo declara `commands.lint_subset` (molde do `test_subset`: comando, não
estágio) e o L1 anexa os caminhos do diff ao argv. O filtro de saída CONTINUA:
subset é otimização de custo, não mudança de veredito — e sem diff conhecido
(None) o comando cheio roda como sempre, porque escopo desconhecido não é
licença para julgar menos.
"""
from __future__ import annotations

import pytest

from dse_contracts import GateStatus  # noqa: F401 — molde dos vizinhos
from dse_validation.config import L1Config, L1ManifestError
from dse_validation.l1.quality_checks import lint_check
from dse_validation.sandbox_exec import ExecResult


class _Recorder:
    """Executor que grava o argv: a propriedade aqui é O QUE rodou."""

    def __init__(self, stdout: str, returncode: int):
        self._stdout, self._rc = stdout, returncode
        self.argvs: list[list[str]] = []

    def run(self, argv, timeout=None):  # noqa: ARG002
        self.argvs.append(list(argv))
        return ExecResult(argv=list(argv), returncode=self._rc,
                          stdout=self._stdout, stderr="")


def _payload(**commands):
    return {"version": 1, "commands": commands}


def test_the_key_parses_like_test_subset():
    """Comando, não estágio: sem `timeouts.lint_subset`, sem veredito próprio."""
    cfg = L1Config._from_manifest_payload(
        _payload(lint=["npx", "eslint", "."],
                 lint_subset=["npx", "eslint", "--cache"],
                 test=["pytest"]),
        source="test")
    assert cfg.lint_subset_cmd == ["npx", "eslint", "--cache"]

    with pytest.raises(L1ManifestError):
        L1Config._from_manifest_payload(
            {"version": 1, "commands": {"test": ["pytest"]},
             "timeouts": {"lint_subset": 60}},
            source="test")


def test_the_gate_appends_the_diff_paths_to_the_subset_argv():
    cfg = L1Config(lint_cmd=["npm", "run", "lint"],
                   lint_subset_cmd=["npx", "eslint", "--cache"])
    rec = _Recorder("src/old/a.ts:1:1: no-unused-vars msg\n", 1)
    finding = lint_check(rec, cfg, {"src/b.ts", "src/a.ts"})
    assert rec.argvs == [["npx", "eslint", "--cache", "src/a.ts", "src/b.ts"]], (
        "com diff conhecido e subset declarado, roda o subset + caminhos ordenados"
    )
    # O filtro de saída continua valendo: o achado é de OUTRO arquivo.
    assert finding.passed is True


def test_an_issue_in_a_touched_file_still_fails_under_the_subset():
    cfg = L1Config(lint_cmd=["npm", "run", "lint"],
                   lint_subset_cmd=["npx", "eslint", "--cache"])
    rec = _Recorder("src/a.ts:9:9: eqeqeq ours\n", 1)
    finding = lint_check(rec, cfg, {"src/a.ts"})
    assert finding.passed is False


def test_without_a_diff_the_full_lint_still_runs():
    """None = escopo DESCONHECIDO: subset aqui esconderia achado real."""
    cfg = L1Config(lint_cmd=["npm", "run", "lint"],
                   lint_subset_cmd=["npx", "eslint", "--cache"])
    rec = _Recorder("", 0)
    lint_check(rec, cfg, None)
    assert rec.argvs == [["npm", "run", "lint"]]


def test_without_the_key_nothing_changes():
    cfg = L1Config(lint_cmd=["npm", "run", "lint"])
    rec = _Recorder("", 0)
    lint_check(rec, cfg, {"src/a.ts"})
    assert rec.argvs == [["npm", "run", "lint"]]


def test_a_documentation_change_still_skips_the_gate_entirely():
    class _NeverRuns:
        def run(self, argv, timeout=None):  # noqa: ARG002
            raise AssertionError(f"rodou sem precisar: {argv}")

    cfg = L1Config(lint_cmd=["npm", "run", "lint"],
                   lint_subset_cmd=["npx", "eslint", "--cache"])
    finding = lint_check(_NeverRuns(), cfg, {"docs/guide.md"})
    assert finding.passed is True
