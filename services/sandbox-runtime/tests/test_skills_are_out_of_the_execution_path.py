"""As skills da plataforma saem do caminho de execução (decisão do operador).

Medido em 2026-08-31: as 25 skills servidas eram TODAS globais
(repo_scope=["*"]/NULL) — incluindo convenções de OUTROS clientes
(acme-naming, reviewing-aviso-code, writing-aviso-*) — materializadas em todo
pod com a nota "MANDATORY guidance — read each SKILL.md below". Para um Coder
agêntico de 8 turnos num monorepo TS, isso é latência e poluição de contexto;
entre clientes, é risco de sangria de convenção. E o vazamento
`.claude/.dse-materialized` na PR #792 é filho da máquina de materialização.

Nenhuma skill causou o bug do supertest (nenhuma o cita) — a remoção não é
culpa, é simplificação: o substrato claude-agent já carrega NATIVAMENTE o
`.claude/` que o PRÓPRIO repositório commita (`setting_sources=["project"]`),
que é onde convenção de repo deve morar. O registry e a promoção ficam
dormentes no banco; o que sai é o SERVING: materialização no pod, nota do
Coder, slots do Tester e skills do render do Planner.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

import sandbox_runtime.activities as acts  # noqa: E402 — depois do sys.path do runner
import sandbox_runtime.sessions as sessions  # noqa: E402

_ACTIVITIES_SRC = Path(acts.__file__).read_text(encoding="utf-8")


def test_the_planner_context_never_reads_the_registry(monkeypatch):
    """O serving do Planner não toca skill_registry — se tocar, explode."""
    def _explode(*a, **k):
        raise AssertionError("o serving do Planner ainda lê o skill_registry")

    monkeypatch.setattr(sessions, "read_approved_skills", _explode, raising=False)
    ctx = sessions.hydrate_planner_context(
        work_item_id="wi_x", tenant_id="t", repo="acme/app",
        instruction="add a health endpoint",
        agents_md="conventions here", codeowners="",
    )
    assert ctx.skills == [], "o contexto do Planner ainda carrega skills"


def test_the_tester_context_has_no_skills_fields():
    """Os campos são ANEXADOS ao prompt, não slots — o pino certo é a fonte."""
    assert "skills_note" not in _ACTIVITIES_SRC, (
        "o Tester ainda anexa a nota de skills"
    )
    assert "reference_spec" not in _ACTIVITIES_SRC, (
        "o Tester ainda injeta spec de referência de skill"
    )


def test_provision_does_not_materialize_skills():
    """Pino de fonte (molde dos testes do chart): o caminho de provisão não
    importa a máquina de materialização nem lê o registry."""
    assert "materialize_skills" not in _ACTIVITIES_SRC, (
        "a provisão ainda materializa skills no workspace"
    )
    assert "read_approved_skills" not in _ACTIVITIES_SRC, (
        "o caminho de execução ainda lê o skill_registry"
    )


def test_the_coder_instruction_gets_no_skills_note():
    assert "workspace_skills_note" not in _ACTIVITIES_SRC, (
        "a instrução do Coder ainda anexa a nota de skills"
    )


def test_the_repo_committed_claude_dir_still_reaches_the_agent():
    """O substituto nativo, pinado: o substrato carrega o `.claude/` do REPO
    (`setting_sources=[\"project\"]`) — convenção de repositório mora no
    repositório, não num registry paralelo da plataforma."""
    import sandbox_runtime.substrate as _sub
    src = Path(_sub.__file__).read_text(encoding="utf-8")
    assert 'setting_sources=["project"]' in src


