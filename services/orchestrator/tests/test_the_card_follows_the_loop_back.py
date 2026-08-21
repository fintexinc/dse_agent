"""O card entra em "Validate" e não volta.

Medido em wi_c2686b91b84 (2026-08-21). O item passou 29 minutos assim:

    17:15:14  card: implementing     <- a única vez que disse "Build"
    17:20:38  card: validating
    17:26:00  card: validating
    17:31:16  card: validating
    17:39:42  card: validating

...enquanto o banco dizia `implementing` em três desses intervalos, porque o L1
reprovou e o laço voltou ao Coder. `_set_status` grava a coluna e AUDITA; quem
fala com a superfície é `_post_status_comment`, uma chamada separada — e na
volta do laço ela não existe.

O custo não é cosmético. O operador olhou o card, viu "Validate" parado por meia
hora e concluiu que a validação era o gargalo — quando o L1 daquela tarefa
custou 5m21s dos 24 minutos, e o tempo estava nos turnos de Coder e Tester. Um
mostrador que só anda para a frente faz diagnosticar pelo lugar errado.

O `implementing` da volta não é o mesmo da ida: ele carrega quantos gates
reprovaram e qual tentativa é — é isso que transforma "Build" numa etapa que
CONTA alguma coisa em vez de piscar."""
from __future__ import annotations

import uuid

import pytest
from dse_contracts import WorkItemStatus
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


@pytest.mark.asyncio
async def test_a_failed_l1_puts_the_card_back_on_build(time_skipping_env):
    work_item_id = new_work_item_id("cardback")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    # L1 reprova uma vez e passa na segunda: o laço volta ao Coder no meio.
    state = FakeControlPlane(plan_risk_class="low", l1_fail_times=1)
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _gate_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await _wait_for_audit(ledger, "l1_failed_retrying")
        await _wait_for_comment_status(ledger, WorkItemStatus.implementing.value, minimo=2)
        await handle.terminate()

    postados = [status for status, _ in ledger.comments]
    # A ida existia; o que faltava era a VOLTA.
    assert postados.count("validating") >= 1
    assert postados.count("implementing") >= 2, (
        f"o card ficou preso em Validate; sequência vista: {postados}"
    )
    # E a volta vem DEPOIS da ida — é o movimento que estava faltando.
    assert postados.index("validating") < len(postados) - 1


async def _wait_for_comment_status(ledger, status: str, *, minimo: int, tentativas: int = 400):
    import asyncio

    for _ in range(tentativas):
        if len([s for s, _ in ledger.comments if s == status]) >= minimo:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"o card nunca voltou para {status!r}; vistos={[s for s, _ in ledger.comments]}")
