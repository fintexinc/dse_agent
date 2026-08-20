"""A régua que o turno do Tester nunca aplicou.

Medido no testbed Angular (2026-08-07): as specs são transpiladas pelo jest com
`tsconfig.spec.json` (`strict: false`, `strictNullChecks: false`), o gate
`typecheck` do repo roda `tsc -p tsconfig.dse.json` — que EXCLUI `*.spec.ts` —
e o `build` também não olha spec. Resultado: um erro de tipo no que o Tester
acabou de escrever não é visto por ninguém dentro do turno, e o item só
descobre um round de L1 depois, gastando um turno de Coder para saber.

Este arquivo pina duas coisas:
  - o turno roda o typecheck DECLARADO PELO REPO (`.dse/validation.json`, a
    mesma régua do L1 — a do build, não a das specs) e uma falha entra no
    RESULTADO do Tester (vermelho antes do fix);
  - o typecheck reprovado não gasta a suíte inteira depois.

O `--coverage=false` era pinado aqui e SAIU na rc.106: a plataforma parou de
injetar flag de ferramenta. O fato (`collectCoverage: true` com piso global de
80% reprova qualquer subconjunto — medido em 9,83%) é do jest daquele
repositório, e agora vive no `commands.test_subset` do manifesto dele. Ver
test_repo_declares_how_it_tests.py.
"""
from __future__ import annotations

import json
import subprocess

from dse_contracts import GateStatus, RunTesterTurnInput
from sandbox_runtime import activities

_MANIFEST = {
    "version": 1,
    "timeouts": {"typecheck": 300},
    "commands": {
        "typecheck": ["sh", "-c", "npx tsc --noEmit -p tsconfig.dse.json"],
        "test": ["sh", "-c", "npx jest --ci"],
    },
}

_TSC_ERROR = (
    "src/app/components/homepage/components/dashboard-list/"
    "dashboard-list.component-dse.spec.ts(42,7): error TS2322: "
    "Type 'string' is not assignable to type 'TagSeverity'.\n"
)


def _done(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _cluster(*, typecheck, manifest=_MANIFEST, suite=None, seen=None):
    """Fake do cluster: manifesto, typecheck e suíte respondidos por argv."""

    def fake_run(argv, **kwargs):
        if seen is not None:
            seen.append((argv, kwargs))
        joined = " ".join(argv)
        if "head -c" in joined:
            if "find ." in joined:
                return _done(argv, 0, stdout="./src/app.spec.ts\n")
            if "cat package.json" in joined:
                return _done(argv, 0, stdout='{"name":"fe","scripts":{"test":"jest"}}')
            return _done(argv, 0, stdout="")
        if ".dse/validation.json" in joined:
            if manifest is None:
                return _done(argv, 1, stdout="")
            return _done(argv, 0, stdout=json.dumps(manifest))
        if "tsc --noEmit" in joined:
            return typecheck
        if "--grep='^tester('" in joined:
            return _done(argv, 0, stdout="tests/app-dse.spec.ts\n")
        if "git log --format=%s" in joined:
            return _done(argv, 0, stdout="tester(wi): authored\n")
        if any(m in joined for m in ("npx jest", "npm test", "python3 -m pytest")):
            return suite if suite is not None else _done(argv, 0, stdout="Tests: 1 passed, 1 total\n")
        return _done(argv, 0, stdout="deadbeef\n")

    return fake_run


def _run(monkeypatch, **cluster):
    seen: list = []
    monkeypatch.setattr(subprocess, "run", _cluster(seen=seen, **cluster))
    monkeypatch.setattr(activities, "audit_emit", lambda **kw: None)
    result = activities._tester_pod_sync(
        RunTesterTurnInput(work_item_id="wi-tc", tenant_id="t", instruction="cover"),
        "dse-sbx-wi-tc", None, "vk", False,
    )
    return result, seen


def _suite_ran(seen) -> bool:
    # rc.106: a suíte é o `commands.test` DECLARADO no manifesto do fake
    # (`npx jest --ci`); sem manifesto, a escada de fallback ainda responde
    # `npm test`. Os dois contam como "a suíte rodou" — o que estes testes
    # observam é o typecheck deixar (ou não) a suíte acontecer.
    return any(("npx jest --ci" in " ".join(a)) or ("npm test" in " ".join(a))
               for a, _k in seen)


def test_a_type_error_in_the_authored_spec_fails_the_turn(monkeypatch):
    """Vermelho hoje: o turno nunca roda typecheck, então devolve PASS sobre uma
    spec que não compila com a régua do repo."""
    result, seen = _run(monkeypatch, typecheck=_done([], 2, stdout=_TSC_ERROR))

    assert result.status is GateStatus.FAIL
    assert result.tests_passed is False
    assert result.suite_deferred is False, "erro de tipo não é veredito de suíte para deferir"
    assert "TS2322" in result.failure_output
    assert not _suite_ran(seen), "typecheck reprovado: não gasta a suíte inteira depois"


def test_the_typecheck_installs_dependencies_before_running(monkeypatch):
    """Regressão medida em produção (wi_aa119e7c, rc.44): o typecheck roda ANTES
    da suíte, e é a suíte que instala `node_modules`. Sem dependência, `npx tsc`
    não acha o compilador local, baixa um pacote homônimo do npm e responde

        This is not the tsc command you are looking for

    com exit != 0 — que o turno reportou como erro de tipo. Todo item de repo
    npm morreu no teto do Tester por uma dependência ausente."""
    _result, seen = _run(monkeypatch, typecheck=_done([], 0))

    tc = [" ".join(a) for a, _k in seen if "tsc --noEmit" in " ".join(a)]
    assert tc, "o typecheck do manifesto tem de rodar"
    assert "npm install" in tc[0] and "node_modules" in tc[0], (
        "o typecheck precisa das dependências instaladas antes de julgar tipos"
    )


def test_a_clean_typecheck_lets_the_suite_decide(monkeypatch):
    result, seen = _run(monkeypatch, typecheck=_done([], 0, stdout=""))

    assert _suite_ran(seen)
    assert result.status is GateStatus.PASS


def test_a_repo_without_a_declared_typecheck_is_unchanged(monkeypatch):
    """Ausência declarada não vira veredito: sem manifesto, o turno é o de sempre."""
    result, seen = _run(monkeypatch, typecheck=_done([], 0), manifest=None)

    assert _suite_ran(seen)
    assert result.status is GateStatus.PASS
