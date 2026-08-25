"""O guia "How to test" viaja do turno do deep link até o trigger do preview.

Mesmo harness db-free do deep link. O que se pina:

  - o payload do resolvedor ganha o grounding novo (`test_plan` do plano,
    `branch` da tarefa) SOB o marker `preview-test-guide-v1` — história antiga
    reproduz byte a byte;
  - o guia validado (`steps`/`login`) viaja no payload do trigger_preview como
    `test_guide` e o contrato REAL o decodifica (o fake faz o decode);
  - resolvedor sem guia → `test_guide` vazio — o preview de hoje, intacto.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator import policy
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import new_work_item_id, wait_for_status
from fakes import FakeControlPlane

from test_plan_approval_timeout import _Ledger, _gate_input, build_db_free_activities

_GUIA = {"steps": ["Abra /planos", "Clique em Nova Simulação"],
         "login": "demo@acme.com / demo123 (supabase/seed.sql)"}


@pytest.fixture(autouse=True)
def _reset_codeowners():
    policy.set_codeowners_reader(None)
    yield
    policy.set_codeowners_reader(None)


async def _run_to_done(time_skipping_env, state: FakeControlPlane, ledger: _Ledger):
    work_item_id = new_work_item_id("guia")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id), id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        return await handle.result()


@pytest.mark.asyncio
async def test_the_grounding_and_the_guide_travel_end_to_end(time_skipping_env):
    state = FakeControlPlane(
        plan_expected_files=["src/app/page.ts"],
        deep_link_result={"path": "/planos", "note": "the new plans page",
                          "cost_usd": 0.002, **_GUIA},
    )
    ledger = _Ledger()
    result = await _run_to_done(time_skipping_env, state, ledger)
    assert result.status == WorkItemStatus.done.value

    pedido = state.last_deep_link_payload or {}
    assert pedido.get("test_plan") == "covers the happy path", (
        "o test_plan do plano não chegou ao resolvedor — o guia fica cego ao "
        "como-testar que o Planner já escreveu"
    )
    assert pedido.get("branch"), "sem branch não há seeds nem manifesto no grounding"

    assert (state.last_preview_payload or {}).get("test_guide") == _GUIA, (
        "o guia não viajou até o trigger — o clique no botão vai achar um preview mudo"
    )


@pytest.mark.asyncio
async def test_a_resolver_without_a_guide_leaves_the_trigger_empty(time_skipping_env):
    state = FakeControlPlane(plan_expected_files=["src/app/page.ts"])
    ledger = _Ledger()
    await _run_to_done(time_skipping_env, state, ledger)
    assert (state.last_preview_payload or {}).get("test_guide") == {}, (
        "sem guia o payload carrega objeto vazio — nunca lixo, nunca ausência "
        "de decode no contrato real"
    )
