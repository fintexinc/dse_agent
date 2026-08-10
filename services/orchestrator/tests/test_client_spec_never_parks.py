"""Spec de cliente quebrada pelo diff NUNCA para o item para pedir licença.

Decisão de operador (2026-08-10, reafirmada nesta sessão): *"eu não quero ter essa
trava, eu quero que o sistema simplesmente consiga editar"*. A supervisão dessa edição
é o **diff da PR** — o revisor humano vê o que foi mudado —, não um clique no meio do
laço.

O que o parque custava, medido: o item ficava em `spec_conflict` **sem prazo nenhum**
(`_park_spec_conflict` espera indefinidamente) por 3 testes falhando de 4.980, num
repositório onde o Coder já tinha permissão de corrigir. Três vezes no mesmo dia.

O que NÃO muda:
  - spec já vermelha no base (`inherited_failures`) continua fora — nunca parqueou,
    nunca ganhou chance, e segue assim;
  - o parque de spec do PRÓPRIO Tester (`tester_spec_exhaustion`) é outro caminho e
    não é tocado aqui;
  - os freios que encerram o item continuam: `coder_not_converging` (mesma queixa 2x),
    o teto de tentativas, a pinça de no-op e o gate de diff vazio.

O que precisa continuar existindo: a EVIDÊNCIA. Sem parque não há mensagem no Slack,
então o conflito tem de permanecer auditável — é o que o segundo teste exige.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest
from temporalio.worker import Worker

from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
_CLIENT_SPEC = "src/app/admin/grid-payout/grid-payouts.reducer.spec.ts"
_SUBJECT = "src/app/admin/grid-payout/grid-payouts.reducer.ts"


def _state(**over) -> FakeControlPlane:
    """L1 reprova SEMPRE no gate `test`, sempre com a MESMA spec de cliente —
    a reincidência que hoje parqueia."""
    detail = f"FAIL {_CLIENT_SPEC}\n  ● retire › marks the level\n    Expected: true\n"
    base = dict(
        plan_risk_class="low",
        l1_fail_times=99,
        l1_fail_detail=detail,
        tester_test_files=["tests/retire-dse.spec.ts"],  # o Tester tem OUTRA spec
        coder_files_changed_by_turn=[[_SUBJECT]],
    )
    base.update(over)
    return FakeControlPlane(**base)


@pytest.mark.asyncio
async def test_a_recurring_client_spec_failure_never_parks(time_skipping_env):
    work_item_id = new_work_item_id("clientspec")
    insert_work_item(work_item_id)
    state = _state()
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    )
    wf_input = WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/fe", base_branch="main", acceptance_criteria="crit",
    )
    handle = await time_skipping_env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)

    async with worker:
        status = await wait_for_status(
            handle, {"spec_conflict", "failed", "escalated", "review_ready"})
        assert status != "spec_conflict", (
            "spec de CLIENTE quebrada pelo diff não pode mais parar o item para "
            "pedir licença — o Coder tem permissão de corrigi-la desde 2026-08-10 e "
            "a supervisão é o diff da PR"
        )
        result = await handle.result()

    # o item termina pelos freios que JÁ existem, não por espera humana
    assert result.status in {"failed", "escalated"}
    assert state.coder_turn_calls >= 2, (
        "o laço tem que ter continuado trabalhando na spec, não parado na 1ª "
        f"reincidência: {state.coder_turn_calls} turnos"
    )


@pytest.mark.asyncio
async def test_the_conflict_stays_auditable_without_the_park(time_skipping_env):
    """Sem parque não há mensagem no Slack. O conflito tem que continuar no
    ledger, senão a decisão do sistema fica invisível para quem revisa a PR."""
    work_item_id = new_work_item_id("clientaudit")
    insert_work_item(work_item_id)
    state = _state()
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    )
    wf_input = WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/fe", base_branch="main", acceptance_criteria="crit",
    )
    handle = await time_skipping_env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)

    async with worker:
        await wait_for_status(handle, {"failed", "escalated", "review_ready"})
        await handle.result()

    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT details::text FROM audit_log WHERE work_item_id=%s AND "
                "action='client_spec_conflict_autofixing' ORDER BY id DESC LIMIT 1",
                (work_item_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row, (
        "o conflito de spec de cliente tem que deixar rastro no ledger mesmo sem "
        "parque — é o que torna a decisão do sistema auditável na revisão da PR"
    )
    assert _CLIENT_SPEC in row[0], "o rastro nomeia a spec afetada"
