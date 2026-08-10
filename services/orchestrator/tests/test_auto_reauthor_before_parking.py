"""Quando o impasse é na spec do PRÓPRIO Tester, o sistema emite a ordem sozinho.

Decisão de operador (2026-08-10): o instrumento do Tester continua protegido do
Coder — se ele pudesse reescrever o teste que julga o próprio código, o verde
deixaria de significar alguma coisa. Mas o operador não quer ser chamado para
resolver o impasse: *"quando o laço travar nisso, o sistema deve emitir a ordem de
reescrita sozinho"*.

Hoje o único gatilho da ordem é o clique em **Reauthor**. A ordem em si já é
inteiramente executável sem humano: o workflow arma `reauthor_specs`/`reauthor_context`,
a rodada seguinte é do Tester, e a execução no Pod tem as próprias guardas de posse
(`_pod_reauthor_partition` recusa qualquer caminho que não seja autoria da plataforma —
R2 vale contra ordem também).

O que estes testes exigem:
  1. o impasse de spec própria vira ordem automática, sem esperar signal;
  2. isso NÃO pode virar ciclo — depois de um número pequeno de ordens automáticas
     sem convergir, o item volta a parquear com dossiê e botões, que é a situação em
     que a decisão realmente pertence a um humano.
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
_OWN_SPEC = "test/grid-payout-retire-dse.spec.ts"


def _detail() -> str:
    return (
        "summary: 1 failed, 200 passed\n"
        "--- the 1 line(s) this gate counted ---\n"
        f"FAIL {_OWN_SPEC}\n"
        "--- raw output (tail) ---\n"
        "  ● retire › excludes retired levels\n"
        "    Expected: 0\n    Received: 3\n"
    )


def _audit_actions(work_item_id: str) -> list[str]:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM audit_log WHERE work_item_id=%s ORDER BY id",
                (work_item_id,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


async def _run(state: FakeControlPlane, work_item_id: str, env, until: set[str]) -> str:
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    )
    wf_input = WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/fe", base_branch="main", acceptance_criteria="crit",
    )
    handle = await env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
    async with worker:
        return await wait_for_status(handle, until)


@pytest.mark.asyncio
async def test_the_own_spec_deadlock_orders_a_rewrite_without_a_human(time_skipping_env):
    """A spec do próprio Tester reprovando duas vezes é o impasse clássico. Em
    vez de esperar o clique, o sistema manda o Tester reescrever."""
    work_item_id = new_work_item_id("autoreauth")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=3,
        l1_fail_detail=_detail(),
        tester_test_files=[_OWN_SPEC],
        coder_files_changed_by_turn=[["src/app/x.ts"]],
    )
    await _run(state, work_item_id, time_skipping_env,
               {"spec_conflict", "review_ready", "failed", "escalated"})

    actions = _audit_actions(work_item_id)
    assert "tester_reauthor_ordered" in actions, (
        "o impasse de spec PRÓPRIA tem que virar ordem automática — hoje só o "
        "clique em Reauthor dispara isso, e o operador não quer ser chamado"
    )
    assert state.tester_reauthor_orders and _OWN_SPEC in state.tester_reauthor_orders[-1], (
        "a ordem chega ao Tester nomeando a spec exata"
    )


@pytest.mark.asyncio
async def test_the_automatic_order_is_bounded_and_then_asks_the_human(time_skipping_env):
    """A ordem automática não pode virar ciclo: o Tester reescreve, falha de
    novo, reescreve de novo… Depois do limite, o item parqueia com dossiê e
    botões — que é quando a decisão realmente pertence a um humano."""
    work_item_id = new_work_item_id("autobound")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=99,  # nunca converge, por mais que reescreva
        l1_fail_detail=_detail(),
        tester_test_files=[_OWN_SPEC],
        coder_files_changed_by_turn=[["src/app/x.ts"]],
    )
    status = await _run(state, work_item_id, time_skipping_env,
                        {"spec_conflict", "failed", "escalated"})

    actions = _audit_actions(work_item_id)
    orders = actions.count("tester_reauthor_ordered")
    assert orders <= 2, (
        f"{orders} ordens automáticas — sem limite isto vira ciclo infinito de "
        "reescrita paga"
    )
    assert status == "spec_conflict", (
        "esgotadas as ordens automáticas, a decisão volta para o humano com "
        f"dossiê e botões; veio {status}"
    )
