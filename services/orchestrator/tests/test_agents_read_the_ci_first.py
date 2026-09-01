"""A instrução do Coder manda ler como o CI do repositório roda os testes.

O par do `test_tester_reads_the_ci_first.py` do sandbox-runtime: o Tester
recebe o setup do CI no contexto; o Coder, que escreve e altera testes no mesmo
turno (não existe posse de teste), recebe a instrução de LER `.github/workflows`
antes. Medido duas vezes no glide-path: testes verdes no sandbox, vermelhos
nas lanes do CI. A frase é constante (P1) — nenhum modelo decide o conteúdo.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import new_work_item_id, wait_for_status
from fakes import FakeControlPlane
from test_plan_approval_timeout import _Ledger, _gate_input, build_db_free_activities


@pytest.mark.asyncio
async def test_the_coder_is_told_to_read_the_ci_before_touching_tests(time_skipping_env):
    wi = new_work_item_id("coder-reads-ci")
    state = FakeControlPlane(ci_sequence=["green"])
    ledger = _Ledger()
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        time_skipping_env.client, task_queue=task_queue, workflows=[WorkItemLifecycleWorkflow],
        activities=build_db_free_activities(ledger, state),
    )
    async with worker:
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _gate_input(wi), id=wi, task_queue=task_queue,
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("cancel", "test over")
        await handle.result()

    assert state.coder_instructions, "o Coder rodou"
    assert ".github/workflows" in state.coder_instructions[0], (
        "o Coder tem que ler como o CI roda os testes antes de escrever os seus"
    )
