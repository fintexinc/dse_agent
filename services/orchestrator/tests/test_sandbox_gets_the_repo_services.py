"""O provision recebe os serviços que o REPO declarou — pelo probe, sob patch.

O probe do manifesto já roda antes do Planner (rc.101); ele passa a devolver
também `services`/`prepare` validados pelo parser real, e o workflow os repassa
no payload do provision. Nenhuma chamada de API nova, nenhuma decisão de
modelo: o fio inteiro é determinístico.

O patch marker (`sandbox-services-from-manifest-v1`) é o que mantém histórias
em voo replayando byte-idênticas: chave nova no RESULTADO de uma activity é
segura (fica gravada na história), mas o payload do provision só muda sob o
marker.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_orchestrator import policy
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import new_work_item_id
from fakes import FakeControlPlane
from test_plan_approval_timeout import (
    _Ledger,
    _gate_input,
    _wait_for_audit,
    build_db_free_activities,
)

_SERVICES = {"postgres": {"image": "postgres:16-alpine", "port": 5432}}


async def _roda(env, state: FakeControlPlane, ledger: _Ledger):
    work_item_id = new_work_item_id("svcwire")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    async with Worker(env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _gate_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await _wait_for_audit(ledger, "sandbox_provisioned")
        await handle.terminate()


@pytest.mark.asyncio
async def test_the_provision_payload_carries_the_declared_services(time_skipping_env):
    state = FakeControlPlane(plan_risk_class="low",
                             repo_manifest_services=dict(_SERVICES),
                             repo_manifest_prepare=["sh", "-c", "migrate"])
    ledger = _Ledger()
    await _roda(time_skipping_env, state, ledger)

    assert state.provision_payloads, "o provision tem de ter sido chamado"
    payload = state.provision_payloads[-1]
    assert payload.get("services") == _SERVICES
    assert payload.get("prepare") == ["sh", "-c", "migrate"]


@pytest.mark.asyncio
async def test_a_repo_without_services_provisions_with_the_exact_payload_of_today(time_skipping_env):
    state = FakeControlPlane(plan_risk_class="low")
    ledger = _Ledger()
    await _roda(time_skipping_env, state, ledger)

    payload = state.provision_payloads[-1]
    assert "services" not in payload or payload["services"] in (None, {})
    assert "prepare" not in payload or payload["prepare"] in (None, [])
