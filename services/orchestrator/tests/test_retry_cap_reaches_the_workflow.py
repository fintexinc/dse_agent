"""O teto de retentativas publicado pelo operador tem que CHEGAR ao workflow.

Medido em 2026-08-10 (wi_82254f59): o operador publicou
`DSE_CODER_RETRY_CAP=8` no configmap, o env apareceu no pod do orchestrator, e
o item morreu com `last_error = "l1_failed_after_3_retries"` — quatro turnos de
Coder, não oito. O mesmo em wi_fadd43185 e wi_176dfa72.

A causa é estrutural e o próprio repositório já a documentava sem consequência
(`models.py`: *"apply_to_input/env only applies to callers that pass the full
input"*): o dispatcher inicia o workflow com `start_workflow(WORKFLOW_TYPE,
work_item_id, ...)` — uma STRING —, então `_coerce_input` monta o dataclass a
partir dos DEFAULTS, e `coder_retry_cap` vale 3 para sempre. `apply_to_input`,
a função que carregaria o env, não tem um único call site de produção.

O conserto segue o precedente que já existe no arquivo para exatamente o mesmo
problema: `resolve_budget_cap` é uma local Activity criada "purely so the env
read stays outside the workflow". Os tetos passam pelo mesmo caminho.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


def _seed_task_content(work_item_id: str) -> None:
    """O caminho da string nua carrega TUDO do banco (load_work_item). Sem o
    evento de ingestao o item para em needs_clarification e nunca chega ao
    laco — que e onde o teto vive."""
    import json as _json

    import psycopg2
    conn = psycopg2.connect("postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingest_events (work_item_id, event_id, kind, payload, processed) "
                "VALUES (%s, %s, 'task_request', %s::jsonb, true) ON CONFLICT DO NOTHING",
                (work_item_id, f"ev-{work_item_id}",
                 _json.dumps({"content_snapshot":
                              "Add a retire flag to payout levels.\n\n"
                              "Acceptance criteria: retired levels stop feeding fee calculations."})),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_the_published_cap_reaches_a_workflow_started_with_a_bare_id(
    time_skipping_env, monkeypatch,
):
    """O caminho REAL do dispatcher: start_workflow com o id nu. O item tem que
    gastar o teto PUBLICADO, não o default do dataclass."""
    monkeypatch.setenv("DSE_CODER_RETRY_CAP", "6")

    work_item_id = new_work_item_id("capenv")
    insert_work_item(work_item_id)
    _seed_task_content(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=99,  # nunca fica verde: o item so morre no teto
        coder_files_changed_by_turn=[["src/app.py"]],
    )
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    )
    # A STRING, como o dispatcher faz (ingest_gateway/dispatcher.py:286).
    handle = await time_skipping_env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, work_item_id,
        id=work_item_id, task_queue=task_queue,
    )

    async with worker:
        await wait_for_status(handle, {"failed", "escalated"})
        result = await handle.result()

    assert state.coder_turn_calls >= 7, (
        "o item gastou o default de 3 (1 inicial + 3 retries = 4 turnos) em vez "
        f"do teto publicado 6 (=7 turnos): {state.coder_turn_calls} turnos. "
        "Foi isto que matou wi_82254f59 com o operador acreditando ter 8."
    )
    assert "3_retries" not in (result.detail or ""), (
        f"o terminal ainda cita 3 retries: {result.detail!r}"
    )


@pytest.mark.asyncio
async def test_an_explicit_input_still_wins_over_the_deployment_default(
    time_skipping_env, monkeypatch,
):
    """PIN: quem passa o input COMPLETO (testes, scripts, o worker) continua
    mandando — o default de deploy só preenche quem não decidiu."""
    monkeypatch.setenv("DSE_CODER_RETRY_CAP", "6")

    work_item_id = new_work_item_id("capexplicit")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=99,
        coder_files_changed_by_turn=[["src/app.py"]],
    )
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    )
    wf_input = WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/x", base_branch="main", acceptance_criteria="crit",
        coder_retry_cap=1,
    )
    handle = await time_skipping_env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)

    async with worker:
        await wait_for_status(handle, {"failed", "escalated"})

    assert state.coder_turn_calls <= 3, (
        f"o teto explicito (1) foi ignorado: {state.coder_turn_calls} turnos"
    )
