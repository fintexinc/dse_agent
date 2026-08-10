"""END-TO-END do MECANISMO: parque → veredito humano → reescrita → PR.

A pergunta do operador, literal: "consegue testar tudo aqui para ver se
finalizaria mesmo? end to end?". Isto responde a metade que é
DETERMINÍSTICA — se os atores produzirem código correto, o encanamento
entrega uma PR e chega a review. A outra metade (o modelo acertar o
TypeScript) nenhum teste local pode responder.

O caminho exercitado é o da rodada real de 2026-08-10, com os dois defeitos
que ela expôs corrigidos:
  1. o Tester escreve testes que falham → o Coder tenta consertá-los → a
     proteção de instrumento reverte → dois no-ops → PARQUE com dossiê;
  2. o humano clica REAUTHOR → o Tester reescreve NO CAMINHO ORDENADO
     (antes: cópia `-dse`, e o clique não tinha efeito);
  3. o orçamento de tentativas VOLTA com o veredito (antes: o item morria no
     L1 seguinte, com o teto já gasto);
  4. o L1 fica verde → finalize → PR → `review_ready`.
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

_SPEC = "src/app/x/fee.service.spec.ts"


@pytest.mark.asyncio
async def test_a_parked_item_reaches_review_ready_after_the_human_verdict(time_skipping_env):
    work_item_id = new_work_item_id("e2epark")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        tester_test_files=[_SPEC],
        # o L1 reprova UMA vez — a spec quebrada. Depois da reescrita ordenada
        # pelo humano, passa. (Turnos no-op pulam os gates, então este é o
        # único L1 antes do parque.)
        l1_fail_times=1,
        l1_fail_detail="[ERROR] /workspace/" + _SPEC + ": cannot find symbol\n",
        # turno 1 escreve produção; depois o Coder só encosta na spec do
        # Tester e a reversão de instrumento desfaz (no-ops)
        coder_files_changed_by_turn=[["src/app/x/fee.service.ts"], [], []],
        coder_reverted_test_paths_by_turn=[[], [_SPEC], [_SPEC]],
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
        # (1) o item PARA e pede humano — com dossiê, não morre no cap
        status = await wait_for_status(handle, {"spec_conflict", "failed", "escalated"})
        assert status == "spec_conflict", f"esperava parque, veio {status}"

        # (2) o humano clica REAUTHOR na thread
        await handle.signal("spec_conflict_resolution",
                            {"verdict": "reauthor", "actor": "usr_test",
                             "comment": "reescreva as specs"})

        # (3)+(4) a ordem chega ao Tester e o item CHEGA à revisão
        final = await wait_for_status(
            handle, {"review_ready", "awaiting_human_review", "failed", "escalated"})
        assert final in {"review_ready", "awaiting_human_review"}, (
            f"o veredito humano tem que levar o item até a revisão; veio {final}. "
            "Se for 'failed', o orçamento de tentativas não voltou com o "
            "veredito (wi_82254f59) — o parque vira teatro."
        )

        # a ordem foi ENTREGUE ao Tester, com os caminhos exatos
        assert state.tester_reauthor_orders, "o Tester nunca recebeu a ordem"
        assert _SPEC in state.tester_reauthor_orders[-1], (
            "a ordem tem que nomear o caminho ORDENADO (wi_8b083140: chegava "
            "como cópia -dse e o clique não tinha efeito)"
        )
        # e a PR existe de verdade no fim do caminho
        assert state.finalize_calls >= 1, "nenhuma PR foi aberta"
        assert state.pr_by_wi.get(work_item_id), "a PR nasceu sem numero"

        pr_number = state.pr_by_wi.get(work_item_id)
        assert pr_number, "a PR precisa ter numero para o merge humano"
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human",
                            {"merged_by": "usr_test", "pr_number": pr_number})
        result = await handle.result()
        assert result.status == "done", f"o merge humano fecha o item; veio {result.status}"
