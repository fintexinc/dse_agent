"""Porta 1 v3 (medida no wi_c9c7b200, 2026-08-07): o parque de
`preexisting_spec_broken_by_diff` disparava na PRIMEIRA falha — e o caso vivo
mostrou um conflito que o Coder resolveria sozinho: a spec do cliente pina
`max-w-[200px]` na célula do nome, o diff do badge tirou a classe do lugar, e
preservá-la é uma mudança no PRÓPRIO HTML do Coder. Parquear aí compra um
julgamento humano para uma pergunta que uma rodada de Coder responde.

A regra v3, sem classificador — o baseline é fato binário já medido pelo gate
(rc.43): spec do cliente FAIL + sujeito no diff acumulado + VERDE no base →
primeira ocorrência vai ao Coder como `l1_failed_retrying` normal, com as
asserções no fix_context; a MESMA spec falhando de novo parqueia como hoje.
Spec vermelha no base continua no fluxo NOT_OUR_FAILURE e nunca parqueia.
O julgamento "obsoleta vs quebrada" continua humano — depois de o Coder ter
tido UMA chance com a informação exata. Vermelho antes do fix.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import (
    WorkItemLifecycleWorkflow,
)

from conftest import DSN, insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

_CLIENT_SPEC = "src/app/components/homepage/components/dashboard-list/dashboard-list.component.spec.ts"
_SUBJECT_HTML = "src/app/components/homepage/components/dashboard-list/dashboard-list.component.html"
_DSE_SPEC = "src/app/components/homepage/components/dashboard-list/dashboard-list.component-dse.spec.ts"

#: wi_c9c7b200, verbatim (abreviado): a asserção que o Coder pode satisfazer
#: preservando a classe no próprio HTML.
_MAXW_DETAIL = f"""summary: 1 errors
--- the 1 line(s) this gate counted ---
FAIL {_CLIENT_SPEC}
--- raw output (tail) ---
  ● DashboardListComponent › should apply correct styling to report name cell

    expect(reportNameCell.classList.contains('max-w-[200px]')).toBeTruthy();

    Received: false

      at src/app/components/homepage/components/dashboard-list/dashboard-list.component.spec.ts:516:66
"""

#: Mistura: a spec do cliente (que estará HERDADA no base) + a spec própria.
_MIXED_INHERITED_DETAIL = f"""summary: 7 errors
--- the 2 line(s) this gate counted ---
FAIL {_CLIENT_SPEC}
FAIL {_DSE_SPEC}
--- raw output (tail) ---
    expect(received).toBe(expected)
"""


def _audit_details(work_item_id: str, action: str) -> list[dict]:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT details FROM audit_log WHERE work_item_id = %s AND action = %s ORDER BY id",
                (work_item_id, action),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


async def _start(state: FakeControlPlane, work_item_id: str, env):
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)
    worker = Worker(
        env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    )
    wf_input = WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        # F3 (2026-08-10): as duas primeiras ordens de reescrita saem sozinhas.
        # O canal de direcionamento HUMANO que este arquivo pina é o segundo
        # estágio — declarar o orçamento gasto o põe onde ele existe.
    )
    handle = await env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
    return worker, handle


@pytest.mark.asyncio
async def test_first_conflict_green_at_base_goes_to_the_coder_with_the_assertions(time_skipping_env):
    """DoD 1a: verde no base + primeira ocorrência → retry automático com as
    asserções no fix_context, nunca parque. O Coder converge e o item fecha."""
    work_item_id = new_work_item_id("v3-first")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=1,
        l1_fail_detail=_MAXW_DETAIL,
        coder_files_changed=[_SUBJECT_HTML],
        tester_test_files=[_DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        assert state.coder_turn_calls == 2, "a primeira chance é UMA rodada de Coder"
        actions = read_audit_actions(work_item_id)
        assert "spec_conflict_detected" not in actions, "primeira ocorrência não parqueia"
        assert "l1_failed_retrying" in actions
        deferred = _audit_details(work_item_id, "spec_conflict_deferred_to_coder")
        assert deferred and deferred[0]["specs"] == [_CLIENT_SPEC]
        assert "max-w-[200px]" in state.coder_instructions[-1], (
            "a asserção exata chega ao Coder no fix_context"
        )
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_the_same_spec_failing_again_keeps_working_instead_of_parking(time_skipping_env):
    """EVOLUIU em 2026-08-10 (F2). Este teste nasceu afirmando "a reincidência
    parqueia, com o dossiê de hoje" — a porta 1 v3.

    A decisão de operador removeu essa parada: spec de CLIENTE quebrada pelo
    diff é trabalho que o Coder já tem permissão de fazer, e a supervisão é o
    diff da PR. O invariante que este teste continua defendendo é o que
    importava: a reincidência é DETECTADA (a chance do Coder foi consumida uma
    vez, e a segunda falha deixa rastro auditável) — só não vira espera humana."""
    work_item_id = new_work_item_id("v3-repeat")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_MAXW_DETAIL,
        coder_files_changed=[_SUBJECT_HTML],
        tester_test_files=[_DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        # sem parque: o item segue sozinho até a revisão
        await wait_for_status(handle, {"review_ready"})
        assert len(_audit_details(work_item_id, "spec_conflict_deferred_to_coder")) == 1, (
            "a PRIMEIRA falha continua sendo a chance do Coder, consumida uma vez"
        )
        autofixing = _audit_details(work_item_id, "client_spec_conflict_autofixing")
        assert autofixing and autofixing[0]["specs"] == [_CLIENT_SPEC], (
            "a REINCIDÊNCIA continua detectada e auditável — só não para o item"
        )
        assert not _audit_details(work_item_id, "spec_conflict_detected"), (
            "spec de cliente não parqueia mais"
        )
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_inherited_red_spec_never_parks_nor_defers(time_skipping_env):
    """DoD 1c: spec vermelha no base é achado HERDADO (NOT_OUR_FAILURE) mesmo
    com o sujeito no diff — não parqueia nem ganha "primeira chance"; a falha
    restante (spec própria) segue o fluxo normal de retry."""
    work_item_id = new_work_item_id("v3-inher")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=1,
        l1_fail_detail=_MIXED_INHERITED_DETAIL,
        l1_inherited_failures=[_CLIENT_SPEC],
        coder_files_changed=[_SUBJECT_HTML],
        tester_test_files=[_DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        actions = read_audit_actions(work_item_id)
        assert "spec_conflict_detected" not in actions, "herdada nunca parqueia"
        assert "spec_conflict_deferred_to_coder" not in actions, (
            "herdada não é conflito — não há chance a dar"
        )
        assert "l1_failed_retrying" in actions
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


# `test_the_retry_comment_reaches_the_coder_context` vivia aqui e saiu em
# 2026-08-10 com o último parque. Ele pinava o canal pelo qual o
# direcionamento humano de um veredito de retry chegava ao Coder — canal que
# existia porque havia um parque onde o humano opinava no MEIO do laço. Sem
# parque não há veredito, e a direção humana volta a entrar por onde sempre
# entrou fora do laço: a descrição do item e a revisão da PR.
