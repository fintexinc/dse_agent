"""O Tester lê como o CI do repositório roda os testes ANTES de escrever os seus.

Medido duas vezes (wi_b95a1d0b e wi_f1f27266, glide-path, 2026-08-31): testes
que passaram no sandbox reprovaram nas lanes do CI do repo — a lane `unit (API)`
é DB-free e AUTH-free, a lane de leak injeta DATABASE_URL por testcontainers e
nunca AUTH_*; `integration/` fica fora de `src/` de propósito. Nada disso está
no package.json nem no exemplo de teste que o prompt já carrega: está nos
workflows (`run:`, `working-directory:`, `env:`, `services:`) e no config do
runner. O contexto do Tester ganha UMA leitura bounded (≤ 1,5 kB) com isso,
sob a seção "How the repository's CI runs its tests — mirror it". Repositório
sem workflows não ganha seção nenhuma — um cabeçalho vazio ensinaria a
procurar o que não existe.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import sandbox_runtime.activities as activities

_WORKFLOW = """name: ci
on: [pull_request]
jobs:
  unit:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: apps/api
    env:
      NODE_ENV: test
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npx vitest run --coverage=false
  leak:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
    steps:
      - run: DATABASE_URL=postgres://ci@localhost/ci npx vitest run integration
"""
_VITEST = """import { defineConfig } from 'vitest/config'
export default defineConfig({ test: { include: ['src/**/*.test.ts'], environment: 'node' } })
"""


def _repo(tmp_path, *, with_ci: bool = True) -> str:
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "devDependencies": {"vitest": "^2"}}))
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.test.ts").write_text("import { test } from 'vitest'\ntest('a', () => {})\n")
    if with_ci:
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(_WORKFLOW)
        (tmp_path / "vitest.config.ts").write_text(_VITEST)
    return str(tmp_path)


def test_the_repo_context_carries_how_the_ci_runs_the_tests(tmp_path):
    _pkg, _example, _existing, ci_setup = activities._tester_repo_context(_repo(tmp_path), diff_files=["src/a.ts"])

    assert "npx vitest run --coverage=false" in ci_setup, "a linha `run:` que o CI executa"
    assert "working-directory: apps/api" in ci_setup
    assert "DATABASE_URL=" in ci_setup, "a lane que injeta o banco — e só ela"
    assert "defineConfig" in ci_setup, "o config do runner, em cabeçalho"
    assert len(ci_setup) <= 1500, "bounded: +7% de prompt, não um workflow inteiro"


def test_a_repo_without_workflows_has_nothing_to_mirror(tmp_path):
    *_rest, ci_setup = activities._tester_repo_context(_repo(tmp_path, with_ci=False))
    assert ci_setup == ""


def test_the_local_context_exposes_it_as_a_field(tmp_path):
    ctx = activities._local_tester_context(_repo(tmp_path))
    assert "vitest run" in ctx.ci_test_setup


def test_the_authoring_prompt_has_the_section_only_when_there_is_something(monkeypatch, tmp_path):
    seen: list[str] = []

    def fake_chat_completion(**kwargs):
        seen.append(json.dumps(kwargs, default=str))
        return SimpleNamespace(content="not json", model="anthropic/claude",
                               cost_usd=0.0, tokens_in=1, tokens_out=1, raw={})

    import model_gateway_client.gateway_call as gc
    monkeypatch.setattr(gc, "chat_completion", fake_chat_completion)
    inp = SimpleNamespace(instruction="add a gauge", plan={}, work_item_id="wi-x", tenant_id="t")

    activities._model_authored_test_script(
        inp, activities._local_tester_context(_repo(tmp_path)), headers=None, virtual_key="vk")
    assert seen and "How the repository's CI runs its tests" in seen[-1]
    assert "npx vitest run --coverage=false" in seen[-1]

    seen.clear()
    activities._model_authored_test_script(
        inp, activities._local_tester_context(_repo(tmp_path / "bare", with_ci=False) if os.makedirs(tmp_path / "bare") is None else ""),
        headers=None, virtual_key="vk")
    assert seen and "How the repository's CI runs its tests" not in seen[-1], (
        "sem workflows, sem seção: um cabeçalho vazio ensina a procurar o que não existe"
    )
