"""`preview check ui|deployable` — o preview provado ANTES de custar um item.

Medido (2026-09-01): 34 previews `created` na vida, 34 degradados, e as 34
causas eram plataforma/receita/ambiente (CA na imagem slim, apk×apt, kind
errado, OOM, RBAC) — cada uma descoberta EM PRODUÇÃO, dentro de um item pago,
a 15-17 min por tentativa. Não existia entrada para subir um preview fora de
um item: `argocd.py` hardcoda `dse/<work_item_id>`.

O smoke é um ITEM DEGENERADO, não um workflow novo: o `WorkItemLifecycleWorkflow`
reconhece a frase (função pura, mensagem inteira), pula router, Planner,
sandbox, Coder, Tester, L1 e PR, e executa SÓ a activity `trigger_preview` que já
existe. O card volta pela mesma máquina: `done` "Preview smoke passed — <url>"
ou `failed` com as palavras do container do app. Sem id sintético: namespace
`preview-<work_item_id>` como sempre.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow, parse_preview_smoke

from conftest import new_work_item_id
from fakes import FakeControlPlane
from test_plan_approval_timeout import _Ledger, _gate_input, build_db_free_activities
from test_post_pr_reports_not_fights import _post_pr_activities

_POD_WORDS = "the pod said: Error: Cannot find module '/srv/app/apps/api/dist/main.js'"


# ---------------------------------------------------------------------------
# A frase — pura, mensagem inteira
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("texto, esperado", [
    ("preview check ui repo=acme/app branch=main",
     {"kind": "ui", "repo": "acme/app", "branch": "main"}),
    ("@dse preview check deployable", {"kind": "deployable", "repo": None, "branch": None}),
    ("  Preview Check UI  ", {"kind": "ui", "repo": None, "branch": None}),
    ("preview check ui branch=feature/x", {"kind": "ui", "repo": None, "branch": "feature/x"}),
])
def test_the_smoke_phrase_is_recognised(texto, esperado):
    assert parse_preview_smoke(texto) == esperado


@pytest.mark.parametrize("texto", [
    "add a preview check to settings so users can preview their profile",
    "preview check",               # sem kind
    "preview check mobile",        # kind que não existe
    "please run preview check ui and then implement the gauge",
    "",
])
def test_a_task_that_merely_mentions_a_preview_is_not_a_smoke(texto):
    assert parse_preview_smoke(texto) is None


# ---------------------------------------------------------------------------
# O item degenerado
# ---------------------------------------------------------------------------

async def _run(env, ledger, state, wi, **kw):
    tq = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        env.client, task_queue=tq, workflows=[WorkItemLifecycleWorkflow],
        activities=build_db_free_activities(ledger, state) + _post_pr_activities(ledger),
    )
    async with worker:
        handle = await env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _gate_input(wi, **kw), id=wi, task_queue=tq,
        )
        return await handle.result()


@pytest.mark.asyncio
async def test_the_smoke_runs_only_the_preview_and_ends_done_with_the_url(time_skipping_env):
    wi = new_work_item_id("smoke-ui")
    state = FakeControlPlane(preview_mode="created")
    ledger = _Ledger()
    result = await _run(
        time_skipping_env, ledger, state, wi,
        task_content="preview check ui branch=main", acceptance_criteria="",
    )

    assert result.status == WorkItemStatus.done.value
    assert state.trigger_preview_calls == 1
    assert state.planner_calls == 0 and state.coder_turn_calls == 0 and state.provision_calls == 0, (
        "um item degenerado: só o preview"
    )
    payload = state.last_preview_payload
    assert payload["pr_number"] is None
    assert payload["branch"] == "main" and payload["kind"] == "ui"
    assert payload.get("files_changed", []) == [], "nunca um files_changed sintético — viraria mentira em preview_created.files"
    assert "preview_smoke_passed" in ledger.audit_actions
    card = [b for s, b in ledger.comments if s == "done"][-1]
    assert "Preview smoke passed" in card and f"http://preview-{wi}.local" in card


@pytest.mark.asyncio
async def test_a_degraded_smoke_fails_with_the_containers_words(time_skipping_env):
    wi = new_work_item_id("smoke-degraded")
    state = FakeControlPlane(preview_mode="degraded")
    state.preview_degraded_detail = _POD_WORDS
    ledger = _Ledger()
    result = await _run(
        time_skipping_env, ledger, state, wi,
        task_content="preview check deployable", acceptance_criteria="",
    )

    assert result.status == WorkItemStatus.failed.value
    assert "Cannot find module" in (result.detail or "")
    assert "preview_smoke_failed" in ledger.audit_actions
    assert state.coder_turn_calls == 0
    card = [b for s, b in ledger.comments if s == "failed"][-1]
    assert "Cannot find module" in card
