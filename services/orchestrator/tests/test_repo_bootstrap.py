"""Repo sem `.dse/validation.json`: o DSE abre a PR de bootstrap ANTES de gastar.

Hoje a ausência só aparece no L1 — com Planner, sandbox, um turno de Coder e um
de Tester já pagos (o freio `l1-manifest-escalates-v1` foi escrito depois de ~$8
queimados nesse beco). A interceptação nova roda antes de `_run_planner_and_gate`,
custa uma chamada de API, e termina com a PR de bootstrap aberta e a tarefa
encerrada com instrução clara — "revise, merge e reenvie".

Roda sem Postgres, com o aparato de `test_plan_approval_timeout` (o harness
db-free canônico; cada activity nova ganha fake em `fakes.py`, a lição paga do
NotFoundError retry storm).
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
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


@pytest.fixture(autouse=True)
def _require_postgres():
    yield


@pytest.fixture(autouse=True)
def _reset_codeowners():
    policy.set_codeowners_reader(None)
    yield
    policy.set_codeowners_reader(None)


@pytest.mark.asyncio
async def test_a_repo_without_a_manifest_gets_a_bootstrap_pr_before_any_spend(time_skipping_env):
    work_item_id = new_work_item_id("bootstrap")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(
        repo_manifest_present=False,
        bootstrap_result={"ok": True, "pr_number": 7, "existing": False,
                          "url": "https://github.com/acme/repo/pull/7", "reason": ""},
    )
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id), id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    # ANTES de gastar: nem Planner, nem sandbox, nem Coder, nem Tester.
    assert state.planner_calls == 0, "o Planner rodou num repo sem manifesto"
    assert state.provision_calls == 0 and state.coder_turn_calls == 0

    assert "repo_bootstrap_pr_opened" in ledger.audit_actions
    detalhe = ledger.audit_details("repo_bootstrap_pr_opened")
    assert detalhe["pr_number"] == 7

    assert result.status == WorkItemStatus.escalated.value
    assert "#7" in (result.detail or ""), result.detail
    assert "resend" in (result.detail or ""), (
        "a instrução ao humano (review, merge, resend) tem que estar na reason "
        "— é ela que vira o comentário terminal no canal"
    )


@pytest.mark.asyncio
async def test_a_repo_with_a_manifest_flows_exactly_as_today(time_skipping_env):
    work_item_id = new_work_item_id("bootstrap-none")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane()  # repo_manifest_present default: True
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id), id=work_item_id, task_queue=task_queue)
        await _wait_for_audit(ledger, "plan_auto_approved")
        await handle.terminate()

    assert "repo_bootstrap_pr_opened" not in ledger.audit_actions
    assert state.planner_calls == 1


@pytest.mark.asyncio
async def test_an_unreachable_api_fails_open_to_todays_flow(time_skipping_env):
    """API fora do ar não é 'manifesto ausente': o item segue e o L1 continua
    sendo quem dá a notícia dura — parar aqui transformaria um soluço de API
    em tarefa morta."""
    work_item_id = new_work_item_id("bootstrap-unreach")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(repo_manifest_reachable=False, repo_manifest_present=False)
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id), id=work_item_id, task_queue=task_queue)
        await _wait_for_audit(ledger, "plan_auto_approved")
        await handle.terminate()

    assert "repo_bootstrap_pr_opened" not in ledger.audit_actions
    assert state.planner_calls == 1


@pytest.mark.asyncio
async def test_a_failed_draft_escalates_pointing_at_the_doc(time_skipping_env):
    work_item_id = new_work_item_id("bootstrap-fail")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(
        repo_manifest_present=False,
        bootstrap_result={"ok": False, "pr_number": None, "existing": False,
                          "url": None, "reason": "model output rejected by the parser"},
    )
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id), id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    assert "repo_bootstrap_generation_failed" in ledger.audit_actions
    assert result.status == WorkItemStatus.escalated.value
    assert "DSE-VALIDATION-MANIFEST" in (result.detail or ""), (
        "sem PR o humano escreve o manifesto à mão — a reason aponta o doc"
    )
    assert state.planner_calls == 0
