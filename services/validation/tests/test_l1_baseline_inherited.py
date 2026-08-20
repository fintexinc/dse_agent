"""Becos 2 e 3 do mapa de alcançabilidade (tests/test_ownership_reachability.py):
uma suite que JÁ FALHAVA no `base_sha` não tem ator autorizado (não é do item)
nem parque desenhado (a porta 1 exige o sujeito no diff) — o item herdava o
vermelho do repo e morria no teto por algo que não quebrou.

O baseline check roda as suites contra o base UMA vez (cacheado no Pod) e
classifica as que já falhavam como `NOT_OUR_FAILURE`. Regras que este arquivo
pina, além do caso feliz:
  - falha NOVA continua reprovando normalmente (nada de anistia por vizinhança);
  - mistura (herdada + própria) reprova pela PRÓPRIA;
  - a regra de evidência do defeito B vence: sem execução, sem PASS;
  - o cache evita o segundo run na rodada seguinte;
  - sem `base_sha` nada muda (compatibilidade com todo o resto da suite).
Vermelho antes do fix.
"""
from __future__ import annotations

import json

from dse_contracts import GateStatus

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import test_check as run_test_check
from dse_validation.sandbox_exec import ExecResult

_BASE = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
_SPEC_A = "src/app/legacy/report-table.component.spec.ts"
_SPEC_B = "src/app/badge/badge.component.spec.ts"

_NOW_ONLY_INHERITED = f"""
FAIL {_SPEC_A}
  ● table › renders rows
    expect(received).toBe(expected)

Tests: 1 failed, 5 passed, 6 total
"""

_NOW_MIXED = f"""
FAIL {_SPEC_A}
  ● table › renders rows
FAIL {_SPEC_B}
  ● badge › shows finished
Tests: 2 failed, 5 passed, 7 total
"""

_NOW_ONLY_OURS = f"""
FAIL {_SPEC_B}
  ● badge › shows finished
Tests: 1 failed, 5 passed, 6 total
"""

#: Zero veredito: nada executou (a regra do defeito B tem que vencer).
_NOW_NO_EVIDENCE = f"""
FAIL {_SPEC_A}
  ● Test suite failed to run
    Cannot find module 'x'
"""

_BASE_RED_A = f"""
FAIL {_SPEC_A}
  ● table › renders rows
Tests: 1 failed, 5 passed, 6 total
"""

_BASE_GREEN = "PASS everything\nTests: 6 passed, 6 total\n"

#: Surefire: o mesmo mecanismo, dialeto do outro testbed.
_NOW_JAVA = """
[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0 -- in com.fintex.LegacyFeeTest
[INFO] Tests run: 9, Failures: 1, Errors: 0, Skipped: 0
"""
_BASE_JAVA_RED = """
[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0 -- in com.fintex.LegacyFeeTest
[INFO] Tests run: 9, Failures: 1, Errors: 0, Skipped: 0
"""


def _res(rc: int, out: str = "") -> ExecResult:
    return ExecResult(argv=["x"], returncode=rc, stdout=out, stderr="")


class _Executor:
    """Despacha por argv: comando do gate, leitura de cache, run do baseline,
    escrita de cache."""

    def __init__(self, *, now: str, base: str | None = None, cached: list[str] | None = None):
        self._now, self._base, self._cached = now, base, cached
        self.baseline_runs = 0
        self.cache_writes: list[str] = []

    def run(self, argv, cwd=None, timeout=300):  # noqa: ARG002 - paridade de assinatura
        joined = " ".join(argv)
        if "cat /tmp/.dse-baseline" in joined:
            if self._cached is None:
                return _res(1, "")
            return _res(0, json.dumps({"v": 1, "suites": self._cached}))
        if "worktree add" in joined:
            self.baseline_runs += 1
            assert self._base is not None, "o baseline não deveria ter rodado"
            return _res(1 if "FAIL" in self._base or "[ERROR]" in self._base else 0, self._base)
        if "> /tmp/.dse-baseline" in joined:
            self.cache_writes.append(joined)
            return _res(0, "")
        return _res(1 if ("FAIL" in self._now or "[ERROR]" in self._now) else 0, self._now)


def _cfg() -> L1Config:
    return L1Config(test_cmd=["npm", "test"])


def test_suite_that_already_failed_at_base_is_not_our_failure():
    ex = _Executor(now=_NOW_ONLY_INHERITED, base=_BASE_RED_A)
    finding = run_test_check(ex, _cfg(), None, base_sha=_BASE)

    assert finding.passed is True, "o item não quebrou esta suite"
    assert finding.status == GateStatus.PASS
    assert "NOT_OUR_FAILURE" in finding.summary
    # `summary` CONTA, `detail` NOMEIA: summary vai para o audit_log
    # append-only (retenção não limpa), caminho de arquivo fica no detail.
    assert "1 suite" in finding.summary
    assert _SPEC_A not in finding.summary, "caminho não entra no ledger imutável"
    assert _SPEC_A in finding.detail, "o herdado tem que ser nomeado, nunca silencioso"
    assert finding.inherited_failures == [_SPEC_A]


def test_suite_green_at_base_and_failing_now_is_a_normal_rejection():
    ex = _Executor(now=_NOW_ONLY_OURS, base=_BASE_GREEN)
    finding = run_test_check(ex, _cfg(), None, base_sha=_BASE)

    assert finding.passed is False
    assert finding.status == GateStatus.FAIL
    assert "NOT_OUR_FAILURE" not in finding.summary
    assert finding.inherited_failures == []


def test_a_mix_still_fails_for_the_items_own_suite():
    ex = _Executor(now=_NOW_MIXED, base=_BASE_RED_A)
    finding = run_test_check(ex, _cfg(), None, base_sha=_BASE)

    assert finding.passed is False, "a falha PRÓPRIA continua reprovando"
    assert finding.inherited_failures == [_SPEC_A], "o herdado é registrado mesmo reprovando"
    assert _SPEC_B in finding.detail


def test_the_evidence_rule_still_wins_over_inheritance():
    """Defeito B intacto: herdado ou não, sem execução não há PASS.

    O status virou ERROR na rc.107 (saída ilegível é problema de configuração,
    não acusação ao diff); o que este teste guarda — NOT_OUR_FAILURE nunca
    alcança uma rodada sem evidência — não mudou."""
    ex = _Executor(now=_NOW_NO_EVIDENCE, base=_BASE_RED_A)
    finding = run_test_check(ex, _cfg(), None, base_sha=_BASE)

    assert finding.passed is False
    assert finding.status == GateStatus.ERROR
    assert "NOT_OUR_FAILURE" not in finding.summary


def test_the_baseline_is_cached_and_does_not_re_run():
    ex = _Executor(now=_NOW_ONLY_INHERITED, base=None, cached=[_SPEC_A])
    finding = run_test_check(ex, _cfg(), None, base_sha=_BASE)

    assert finding.passed is True
    assert ex.baseline_runs == 0, "a rodada seguinte lê o cache do Pod"


def test_the_first_run_writes_the_cache():
    ex = _Executor(now=_NOW_ONLY_INHERITED, base=_BASE_RED_A)
    run_test_check(ex, _cfg(), None, base_sha=_BASE)

    assert ex.baseline_runs == 1
    assert ex.cache_writes, "sem escrita de cache o custo volta a cada rodada"


def test_surefire_dialect_is_compared_by_class():
    ex = _Executor(now=_NOW_JAVA, base=_BASE_JAVA_RED)
    finding = run_test_check(ex, L1Config(test_cmd=["./mvnw", "test"]), None, base_sha=_BASE)

    assert finding.passed is True
    assert finding.inherited_failures == ["com.fintex.LegacyFeeTest"]


def test_without_base_sha_nothing_changes():
    """Compatibilidade: todo o resto da suite chama sem base_sha."""
    ex = _Executor(now=_NOW_ONLY_INHERITED, base=None)
    finding = run_test_check(ex, _cfg(), None)

    assert finding.passed is False
    assert ex.baseline_runs == 0
