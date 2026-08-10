"""Um item com diff de produção VAZIO nunca vira review_ready.

Medido no wi_f1d2d66d (FE, 2026-08-10): o turno do Coder produziu ZERO
arquivos; o Tester escreveu a spec, ela FALHOU (returncode 1), mas o deferral
converteu em PASS confiando no re-julgamento do L1 — e o L1 passou VACUAMENTE
(a spec vivia em tests/, fora do que o comando de teste do repo inclui). O
resultado foi um review_ready de fachada: PR #18 com um único arquivo, a spec
do Tester, e nenhuma linha de produção.

O invariante: L1 verde + diff acumulado vazio = ninguém implementou nada — o
laço volta ao Coder com a instrução dizendo isso; se o cap estourar ainda
vazio, o item falha NOMEANDO o motivo. `cumulative_files_changed` acumula só
turnos do Coder (a spec do Tester não conta como produção), então o cenário
medido cai exatamente no gate.
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


@pytest.mark.asyncio
async def test_an_empty_diff_with_a_green_l1_never_reaches_review_ready(time_skipping_env):
    work_item_id = new_work_item_id("emptydiff")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        coder_files_changed_by_turn=[[]],  # o Coder nunca escreve nada
        tester_test_files=["tests/retire-dse.spec.ts"],
    )
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
        status = await wait_for_status(handle, {"review_ready", "failed", "escalated"})
        assert status != "review_ready", (
            "L1 verde + diff vazio virou review_ready — o PR de fachada do "
            "wi_f1d2d66d (um arquivo, a spec do Tester, zero produção)"
        )
        result = await handle.result()
        assert "empty" in (result.detail or "").lower() or "diff" in (result.detail or "").lower(), (
            f"o terminal tem que NOMEAR o diff vazio, não um genérico: {result.detail!r}"
        )
        # e o Coder ganhou instrução explícita sobre o vazio antes de morrer
        assert any("no code changes" in i.lower() or "produced no" in i.lower()
                   for i in state.coder_instructions[1:]), (
            "o retry precisa DIZER ao Coder que os turnos vieram vazios"
        )
