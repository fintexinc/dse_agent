"""PlanArtifact — in Phase 2 it is produced by a dedicated read-only Planner
session (WSC-E3-T3). In Phase 1 (single Coder, no separate Planner) the Coder
fills in a minimal version of this artifact *before* writing any diff, because
the L1 diff-budget/forbidden-paths enforcement (WSE-E1-T3) depends on it
existing regardless of who produced it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PlanArtifact(BaseModel):
    work_item_id: str
    steps: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)  # declared blast radius
    # Explicit escape hatch for tasks that deliberately produce no patch. An
    # empty plan without this flag is invalid in the workflow; keeping the
    # default False makes historical payloads additive and safe.
    no_code_change: bool = False
    # LEGACY (rc.89): nunca foi dimensionado por ninguém — constante 400 desde a
    # Fase 1 ("access bundle may adjust" nunca aconteceu). Mantido SÓ para
    # revalidar payloads históricos (work_items.plan, histories do Temporal).
    # Não é exibido, não entra no contexto do L2 e não alimenta o classificador
    # de risco — ver plan_compliance.py (o gate L1 de diff é informativo).
    diff_budget_lines: int = 400
    # rc.89: estimativa de ORDEM DE GRANDEZA do diff, declarada pelo Planner
    # (linhas somadas add+remove, produção+teste). None = o Planner não estimou
    # (fixture, resposta sem o campo, valor não-numérico). É informação para o
    # aprovador e para o classificador de risco — não é teto.
    estimated_lines: int | None = None
    test_plan: str = ""
    risk_class: str = "low"  # Phase 1: informational only — the approval gate is Phase 2
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [".github/workflows/", "migrations/"]
    )
