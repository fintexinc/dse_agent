"""O contexto do Tester enxerga o WORKSPACE da mudança, não só a raiz.

Medido nas rodadas 5 e 7 do glide-path (supertest 2x): o prompt proibia
importar pacote fora do "package.json shown" — e mostrava o package.json da
RAIZ do monorepo, onde as deps do apps/api nem estão. E o exemplo vizinho era
UM arquivo (parava no primeiro legível), quando os testes de
apps/api/integration/ com a convenção certa estavam todos do lado.
"""
from __future__ import annotations

import os
import subprocess
import sys

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from sandbox_runtime.activities import _pod_tester_context  # noqa: E402


def _real_pod(root):
    def pod_sh(script, *, timeout=None, input_text=None):
        return subprocess.run(
            ["sh", "-c", script.replace("cd /workspace &&", f"cd {root} &&", 1)],
            capture_output=True, text=True, errors="replace",
        )
    return pod_sh


def _monorepo(tmp_path):
    """Monorepo mínimo com um commit tocando apps/api."""
    (tmp_path / "package.json").write_text('{"name": "root-workspace"}\n')
    api = tmp_path / "apps" / "api"
    (api / "src").mkdir(parents=True)
    (api / "integration").mkdir(parents=True)
    (api / "package.json").write_text(
        '{"name": "@acme/api", "devDependencies": {"vitest": "^2.0.0"}}\n'
    )
    (api / "integration" / "alpha.integration.test.ts").write_text(
        "// CONVENTION-ALPHA: app.inject style\nimport x from 'vitest';\n"
    )
    (api / "integration" / "beta.integration.test.ts").write_text(
        "// CONVENTION-BETA: also inject\nimport y from 'vitest';\n"
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, capture_output=True)
    (api / "src" / "health.ts").write_text("export const h = 1;\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "touch apps/api"],
        check=True, capture_output=True,
    )
    return tmp_path


def test_package_json_comes_from_the_workspace_the_change_touches(tmp_path):
    _monorepo(tmp_path)
    ctx = _pod_tester_context(_real_pod(tmp_path))
    assert "vitest" in ctx.package_json, (
        "o Tester recebeu só o package.json da raiz — a regra 'não importe o "
        "que não está nas deps' aponta para um arquivo que não lista as deps"
    )


def test_up_to_two_neighbours_teach_the_convention(tmp_path):
    _monorepo(tmp_path)
    ctx = _pod_tester_context(_real_pod(tmp_path))
    assert "CONVENTION-ALPHA" in ctx.example_test
    assert "CONVENTION-BETA" in ctx.example_test, (
        "um vizinho só: o segundo exemplo — que reforça a convenção — nunca "
        "chega, e o prior do ecossistema vence"
    )
