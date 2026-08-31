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
