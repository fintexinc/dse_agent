"""O repo desliga um gate SEU dizendo isso com todas as letras — nunca mentindo.

Medido no wi_8c26a5e7 (`calculation-engine-service`): `test` (574s, incluindo a
re-rodada de baseline no mesmo cronômetro) + `build` (387s — o pom fixa
`<skipTests>false</skipTests>` no surefire e o `-DskipTests` do manifesto é
ignorado em silêncio, então o `package` roda a suíte INTEIRA de novo) = ~16
minutos de L1 por ciclo, num laço que itera.

O operador pediu para desligar os dois "por enquanto". As duas rotas óbvias são
ambas erradas, e este arquivo existe para que ninguém as redescubra:

  - **anular o comando** no manifesto: `_not_configured` tem `passed=False` e o
    veredito do pipeline é `all(f.passed)` — o L1 inteiro reprova, o Coder
    recebe "no test command in the trusted manifest" como coisa a consertar, e
    não há conserto possível (o manifesto é lido do base SHA). Uma nova tarefa
    impossível por construção.
  - **stub `echo`**: o gate de teste exige EVIDÊNCIA de execução; o único stub
    que passa imprime uma contagem falsa, e o ledger registraria testes que
    nunca rodaram.

`disabled_stages` é a porta honesta: o estágio sai `PASS` com summary
`not run: disabled by the repository manifest (disabled_stages)` — o ledger diz
que não rodou e por quê. A governança é a mesma do `forbidden_paths`: o campo é
lido do base SHA, então desligar um gate custa um merge revisado por humano.

Só os QUATRO gates do repo são desligáveis. `sast` e `secret_scan` são da
plataforma — repo não desliga scan de segredo.
"""
from __future__ import annotations

import pytest

from dse_contracts import GateStatus

from dse_validation.config import L1Config, L1ManifestError
from dse_validation.l1.quality_checks import (
    build_check,
    lint_check,
    typecheck_check,
)
from dse_validation.l1.quality_checks import test_check as run_test_check


class _MustNotRun:
    """Executor que estoura se o gate tentar executar qualquer coisa —
    "desligado" significa desligado, não "rodou e ignoramos"."""

    def run(self, argv, cwd=None, timeout=300):  # noqa: ARG002
        raise AssertionError(f"estágio desligado executou um comando: {argv!r}")


# ---------------------------------------------------------------------------
# Parse do manifesto
# ---------------------------------------------------------------------------

def _manifest(**extra):
    return {
        "version": 1,
        "commands": {
            "lint": ["sh", "-c", "./mvnw -B -q spotless:check"],
            "test": ["sh", "-c", "./mvnw -B test"],
            "build": ["sh", "-c", "./mvnw -B -q -DskipTests package"],
        },
        **extra,
    }


def test_the_manifest_can_disable_its_own_stages():
    cfg = L1Config._from_manifest_payload(
        _manifest(disabled_stages=["test", "build"]), source="base:.dse/validation.json"
    )
    assert cfg.manifest_status == GateStatus.PASS, cfg.manifest_detail
    assert cfg.disabled_stages == frozenset({"test", "build"})
    # os comandos ficam INTACTOS: a receita do preview lê commands.build[2]
    # do mesmo arquivo, e desligar o gate não pode quebrar o preview.
    assert cfg.build_cmd, "desligar o gate não apaga o comando"


def test_without_the_field_nothing_is_disabled():
    cfg = L1Config._from_manifest_payload(_manifest(), source="m")
    assert cfg.disabled_stages == frozenset()


def test_a_platform_scan_cannot_be_disabled_by_the_repo():
    for scan in ("sast", "secret_scan"):
        with pytest.raises(L1ManifestError) as exc:
            L1Config._from_manifest_payload(_manifest(disabled_stages=[scan]), source="m")
        assert "disabled_stages" in str(exc.value.detail)


def test_an_unknown_stage_name_is_an_error_with_the_field_name():
    with pytest.raises(L1ManifestError) as exc:
        L1Config._from_manifest_payload(_manifest(disabled_stages=["tests"]), source="m")
    assert "disabled_stages" in str(exc.value.detail)


def test_the_wrong_shape_is_refused():
    with pytest.raises(L1ManifestError):
        L1Config._from_manifest_payload(_manifest(disabled_stages="test"), source="m")
    with pytest.raises(L1ManifestError):
        L1Config._from_manifest_payload(_manifest(disabled_stages=[1]), source="m")


# ---------------------------------------------------------------------------
# Comportamento dos gates
# ---------------------------------------------------------------------------

def test_a_disabled_stage_passes_saying_it_did_not_run():
    cfg = L1Config(test_cmd=["mvn", "test"], disabled_stages=frozenset({"test"}))
    finding = run_test_check(_MustNotRun(), cfg)
    assert finding.passed is True
    assert finding.status is GateStatus.PASS
    assert finding.summary == (
        "not run: disabled by the repository manifest (disabled_stages)"
    ), finding.summary


def test_every_repo_gate_honours_the_switch_without_executing():
    cfg = L1Config(
        lint_cmd=["l"], typecheck_cmd=["t"], test_cmd=["s"], build_cmd=["b"],
        disabled_stages=frozenset({"lint", "typecheck", "test", "build"}),
    )
    executor = _MustNotRun()  # .run estoura — provar que NADA executa
    for gate in (lint_check, typecheck_check, run_test_check, build_check):
        finding = gate(executor, cfg)
        assert finding.passed is True, finding.check
        assert "disabled by the repository manifest" in finding.summary


def test_a_stage_not_in_the_set_still_runs_normally():
    """Rede de segurança: desligar `test` não encosta no `lint`."""
    cfg = L1Config(
        lint_cmd=["ruff", "check", "."], disabled_stages=frozenset({"test"})
    )

    class _Green:
        def run(self, argv, cwd=None, timeout=300):  # noqa: ARG002
            from dse_validation.sandbox_exec import ExecResult
            return ExecResult(argv=list(argv), returncode=0, stdout="", stderr="")

    finding = lint_check(_Green(), cfg)
    assert finding.passed is True
    assert "disabled" not in finding.summary


def test_disabled_wins_over_not_configured():
    """Um estágio desligado E sem comando é desligado, não misconfigurado —
    a intenção declarada vence a ausência."""
    cfg = L1Config(disabled_stages=frozenset({"build"}))  # sem build_cmd
    finding = build_check(_MustNotRun(), cfg)
    assert finding.passed is True
    assert "disabled by the repository manifest" in finding.summary
