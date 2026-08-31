"""Import de pacote inexistente falha em SEGUNDOS, não em US$ 10 de laço.

Rodadas 5 e 7 do glide-path: o Tester importou `supertest` (não é dependência
do repo), o tipo não resolveu, 22-47 erros de lint type-aware viraram
insolúveis — nenhuma edição conserta o tipo de um módulo ausente — e o item
escalou depois de turnos pagos de reparo. O prompt já proibia por nome e
perdeu duas vezes: regra em prompt não é freio.

O freio é determinístico e roda ANTES de typecheck/suite: os arquivos que o
turno acabou de escrever são varridos por import/require de pacote que não
está nas deps mescladas (raiz + workspace mais próximo + builtins + caminhos
relativos). Achou → o turno falha NOMEADO, e o feedback do retry diz o que o
modelo precisa ouvir.
"""
from __future__ import annotations

import os
import subprocess
import sys

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from sandbox_runtime.activities import _phantom_import_failure  # noqa: E402


def _real_pod(root):
    def pod_sh(script, *, timeout=None, input_text=None):
        return subprocess.run(
            ["sh", "-c", script.replace("cd /workspace &&", f"cd {root} &&", 1)],
            capture_output=True, text=True, errors="replace", input=input_text,
        )
    return pod_sh


def _repo(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "root"}\n')
    api = tmp_path / "apps" / "api"
    (api / "integration").mkdir(parents=True)
    (api / "package.json").write_text(
        '{"name": "@acme/api", "dependencies": {"pg": "^8"},'
        ' "devDependencies": {"vitest": "^2", "@nestjs/testing": "^10"}}\n'
    )
    return api / "integration"


def test_a_phantom_import_fails_named(tmp_path):
    d = _repo(tmp_path)
    (d / "health.integration.test.ts").write_text(
        'import request from "supertest";\nimport { Test } from "@nestjs/testing";\n'
    )
    msg = _phantom_import_failure(
        _real_pod(tmp_path), ["apps/api/integration/health.integration.test.ts"]
    )
    assert msg is not None
    assert "supertest" in msg, "o freio tem que NOMEAR o pacote fantasma"
    assert "dependency" in msg.lower()


def test_real_deps_builtins_and_relative_paths_pass(tmp_path):
    d = _repo(tmp_path)
    (d / "ok.integration.test.ts").write_text(
        'import { Test } from "@nestjs/testing";\n'
        'import { describe } from "vitest";\n'
        'import pg from "pg";\n'
        'import fs from "node:fs";\n'
        'import path from "path";\n'
        'import { AppModule } from "../src/app.module.js";\n'
        'import helper from "./helper";\n'
    )
    msg = _phantom_import_failure(
        _real_pod(tmp_path), ["apps/api/integration/ok.integration.test.ts"]
    )
    assert msg is None, f"falso positivo: {msg}"


def test_non_js_files_are_left_alone(tmp_path):
    _repo(tmp_path)
    (tmp_path / "test_x.py").write_text("import totally_absent_pkg\n")
    # Fora do alcance do freio v1 (JS/TS): nunca inventa veredito sobre o que
    # não sabe ler.
    assert _phantom_import_failure(_real_pod(tmp_path), ["test_x.py"]) is None


# ---------------------------------------------------------------------------
# O fio: o freio roda DENTRO do turno, antes de typecheck/suite
# ---------------------------------------------------------------------------
# O scanner acima é unidade; daqui para baixo é o comportamento que custou os
# US$ 20: o turno tem que (1) re-autorar UMA vez com o feedback nomeado e
# (2) se o fantasma sobreviver, falhar NOMEADO sem gastar a suíte — o
# deferral não pode empurrar um import inexistente para o L1 como se fosse
# asserção em desacordo.

from dse_contracts import RunTesterTurnInput  # noqa: E402
from sandbox_runtime import activities  # noqa: E402

_SPEC = "apps/api/integration/health.integration.test.ts"


def _done(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _fake_cluster(seen):
    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        joined = " ".join(argv)
        if "head -c" in joined:
            if "find ." in joined:
                return _done(argv, 0, stdout="./apps/api/integration/old.test.ts\n")
            if "cat package.json" in joined:
                return _done(argv, 0, stdout='{"name":"fixture","scripts":{"test":"vitest"}}')
            if "git show" in joined:
                return _done(argv, 0, stdout="diff --git a/x b/x\n")
            return _done(argv, 0, stdout="")
        if "--grep='^tester('" in joined:
            return _done(argv, 0, stdout="")  # nada reutilizado: autoria REAL
        if any(m in joined for m in ("npm test", "python3 -m pytest", "vitest")):
            return _done(argv, 0, stdout="Tests: 1 passed\n")
        return _done(argv, 0, stdout="deadbeef\n")

    return fake_run


def _author_stub(calls, *, content='import request from "supertest";\n'):
    def stub(inp, ctx, headers, virtual_key, *, error_feedback=""):
        calls.append({"error_feedback": error_feedback})
        return [{"tool": "write_file", "path": _SPEC, "content": content},
                {"tool": "run_tests"}], 0.05

    return stub


def _run_turn(monkeypatch, *, scan_results):
    seen: list = []
    calls: list = []
    rows: list[dict] = []
    resultados = list(scan_results)
    monkeypatch.setattr(subprocess, "run", _fake_cluster(seen))
    monkeypatch.setattr(activities, "_model_authored_test_script", _author_stub(calls))
    monkeypatch.setattr(activities, "audit_emit", lambda **kw: rows.append(kw))
    monkeypatch.setattr(
        activities, "_phantom_import_failure",
        lambda pod_sh, files: resultados.pop(0) if resultados else None,
    )
    result = activities._tester_pod_sync(
        RunTesterTurnInput(work_item_id="wi-ph", tenant_id="t", instruction="cover"),
        "dse-sbx-wi-ph", None, "vk", False,
    )
    return result, seen, calls, rows


def _suite_ran(seen):
    # "pytest" solto casaria o `.pytest_cache` do find do CONTEXTO; suíte de
    # verdade é a invocação do runner, não uma substring de exclusão.
    return any(
        any(isinstance(s, str) and ("npm test" in s or "python3 -m pytest" in s) for s in a)
        for a, _k in seen
    )


def test_a_surviving_phantom_fails_the_turn_named(monkeypatch):
    msg = 'PHANTOM IMPORT — health.integration.test.ts imports "supertest", which is NOT a dependency'
    result, seen, calls, rows = _run_turn(monkeypatch, scan_results=[msg, msg])
    assert len(calls) == 2, "uma re-autoria, com o feedback nomeado"
    assert "supertest" in calls[1]["error_feedback"]
    assert result.tests_passed is False
    assert result.suite_deferred is False, "import fantasma não é asserção: não defere"
    assert "supertest" in (result.failure_output or "")
    assert not _suite_ran(seen), "veredito determinístico já existe: a suíte não roda"
    assert any(r["action"] == "tester_phantom_import" for r in rows)


def test_a_phantom_fixed_by_the_retry_reaches_the_suite(monkeypatch):
    msg = 'PHANTOM IMPORT — "supertest" is NOT a dependency'
    result, seen, calls, rows = _run_turn(monkeypatch, scan_results=[msg, None])
    assert len(calls) == 2
    assert _suite_ran(seen), "fantasma resolvido: o turno segue normal"
    assert result.tests_passed is True
