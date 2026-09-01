"""O preview pode subir SEM uma PR — o smoke da rc.131.

Medido (2026-09-01): 34/34 previews degradaram por plataforma/receita, cada um
descoberto em produção dentro de um item pago. `trigger_preview` era chamado
só com o branch `dse/<work_item_id>` e o `pr_number` da PR do item; o smoke
passa `branch` e `kind` explícitos e nenhuma PR. Tudo abaixo (receita,
credencial, `for_kind`, `_ready_timeout`) já era parametrizado por
branch/kind — o que faltava era a entrada.

O que este arquivo pina: sem PR, a linha da descrição da PR NÃO é escrita e
isso NÃO é falha (é desenho — nada de `preview_line_in_pr_body_failed`); a
linha de `wse_previews` aceita `pr_number` nulo (migração 0049); o branch e o
kind explícitos vencem os defaults do item.
"""
from __future__ import annotations

import pytest

from dse_contracts.activities import TriggerPreviewInput

from dse_validation import db
from dse_validation.preview import argocd


def _smoke(**kw) -> TriggerPreviewInput:
    base = dict(work_item_id="wi_smoke", tenant_id="t", repo="acme/app",
                pr_number=None, branch="main", kind="ui", ttl_seconds=1800)
    base.update(kw)
    return TriggerPreviewInput(**base)


def test_without_a_pr_the_body_line_is_skipped_by_design_not_as_a_failure(monkeypatch):
    import dse_validation.github.client as gc

    def _never(cfg=None):
        raise AssertionError("sem PR não há cliente do GitHub a construir")

    monkeypatch.setattr(gc, "build_github_client", _never)
    audits: list[dict] = []
    monkeypatch.setattr(argocd, "audit_emit", lambda **kw: audits.append(kw))

    argocd._put_preview_in_pr_body(_smoke(), argocd.preview_body_line("created", url="http://p"), actor="test")

    assert not [a for a in audits if a.get("action") == "preview_line_in_pr_body_failed"], (
        "não ter PR é o desenho do smoke, não uma escrita que falhou"
    )


def test_a_missing_repo_is_still_a_failure_worth_a_trace(monkeypatch):
    """A rede continua: PR que existe e não pôde ser escrita deixa rastro."""
    audits: list[dict] = []
    monkeypatch.setattr(argocd, "audit_emit", lambda **kw: audits.append(kw))
    inp = TriggerPreviewInput(work_item_id="wi_x", tenant_id="t", repo="", pr_number=7)
    argocd._put_preview_in_pr_body(inp, "line", actor="test")
    assert [a for a in audits if a.get("action") == "preview_line_in_pr_body_failed"]


def test_the_explicit_branch_and_kind_win_over_the_items_defaults():
    assert argocd.preview_branch(_smoke(branch="feature/x")) == "feature/x"
    assert argocd.preview_branch(_smoke(branch=None)) == "dse/wi_smoke"
    assert argocd.preview_kind(_smoke(kind="deployable")) == "deployable"
    # sem kind explícito: o paths-filter de sempre (docs → none)
    assert argocd.preview_kind(_smoke(kind=None, files_changed=["README.md"])) == "none"


def test_the_preview_row_accepts_no_pr(work_item_id, tenant_id):
    db.upsert_preview(
        work_item_id=work_item_id, tenant_id=tenant_id, pr_number=None, repo="acme/app",
        status="created", namespace=f"preview-{work_item_id}", url="http://p", ttl_seconds=1800,
    )
    row = db.get_preview(work_item_id)
    assert row is not None and row["status"] == "created" and row["pr_number"] is None
