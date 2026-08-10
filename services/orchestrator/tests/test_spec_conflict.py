"""Porta 1 do deadlock de posse de spec (medido em wi_1a5f9e3d, 2026-08-07):
o badge alterou o DOM, a spec PRÉ-EXISTENTE do repo pinava `td:nth-child(3)`,
403 asserções quebraram — e nenhum ator do laço podia tocá-la (o Coder tem
edição de teste revertida; o Tester só repara spec própria). O item queimou o
teto inteiro repetindo a mesma falha.

A porta: escalar, não editar. Quando o L1 reprova numa spec pré-existente cujo
SUJEITO está no diff, o item para em `spec_conflict` aguardando humano — a
mesma primitiva de espera durável do plan approval — em vez de comprar mais um
turno de Coder que não pode agir. Spec do próprio Tester segue no fluxo normal
(ele pode repará-la). Vermelho antes do fix.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import DSN, insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

_PREEXISTING_SPEC = "src/app/components/dashboard-list/dashboard-list.component.spec.ts"
_SUBJECT_TS = "src/app/components/dashboard-list/dashboard-list.component.ts"
_SUBJECT_HTML = "src/app/components/dashboard-list/dashboard-list.component.html"

#: O shape que quality_checks emite: as linhas FAIL contadas + o tail cru.
_L1_DETAIL = (
    "summary: 403 errors\n"
    "--- the 2 line(s) this gate counted ---\n"
    f"FAIL {_PREEXISTING_SPEC} (6.612 s)\n"
    "FAIL test/dashboard-badge-dse.spec.ts\n"
    "--- raw output (tail) ---\n"
    "expect(programTags?.length).toBe(0)\n"
    "Received: 1\n"
)


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


# ---------------------------------------------------------------------------
# O detector puro (unit): o mapeamento spec -> sujeito é determinístico e a
# posse do Tester exclui ANTES de olhar o diff.
# ---------------------------------------------------------------------------


def test_detector_matches_angular_spec_to_touched_subject():
    from dse_orchestrator.workflows import preexisting_spec_conflicts

    specs = preexisting_spec_conflicts(
        _L1_DETAIL,
        tester_owned=["test/dashboard-badge-dse.spec.ts"],
        diff_files=[_SUBJECT_TS, _SUBJECT_HTML],
    )
    assert specs == [_PREEXISTING_SPEC]


def test_detector_ignores_specs_whose_subject_is_not_in_the_diff():
    from dse_orchestrator.workflows import preexisting_spec_conflicts

    specs = preexisting_spec_conflicts(
        f"FAIL {_PREEXISTING_SPEC}\n",
        tester_owned=[],
        diff_files=["src/app/other/other.component.ts"],
    )
    assert specs == []


def test_detector_maps_java_surefire_mirror_tree():
    from dse_orchestrator.workflows import preexisting_spec_conflicts

    specs = preexisting_spec_conflicts(
        "FAIL src/test/java/com/acme/PayoutServiceTest.java\n",
        tester_owned=[],
        diff_files=["src/main/java/com/acme/PayoutService.java"],
    )
    assert specs == ["src/test/java/com/acme/PayoutServiceTest.java"]


# ---------------------------------------------------------------------------
# Control-plane: a decisão do workflow.
# ---------------------------------------------------------------------------


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
    )
    handle = await env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
    return worker, handle


@pytest.mark.asyncio
async def test_preexisting_spec_failure_is_worked_not_parked(time_skipping_env):
    """EVOLUIU em 2026-08-10 (F2). Nasceu como DoD 1+3 da porta 1: a
    reincidência PARAVA o item e entregava ao humano qual spec, quais asserções
    e o diff que a invalidou.

    A decisão de operador removeu a parada — o Coder já podia corrigir spec de
    cliente desde 10/08 e a supervisão é o diff da PR. O que este teste
    continua defendendo, e é o que importava: a primeira falha dá ao Coder UMA
    rodada com as asserções, e a reincidência entrega EXATAMENTE a mesma
    evidência (qual spec, quais asserções, qual diff) — agora no ledger, para
    quem revisa a PR, em vez de numa espera sem prazo."""
    work_item_id = new_work_item_id("specconf")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_L1_DETAIL,
        coder_files_changed=[_SUBJECT_TS, _SUBJECT_HTML],
        tester_test_files=["test/dashboard-badge-dse.spec.ts"],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"review_ready"})

        actions = read_audit_actions(work_item_id)
        assert "spec_conflict_deferred_to_coder" in actions, "a chance é auditável"
        assert "spec_conflict_detected" not in actions, "spec de cliente não parqueia"

        detected = _audit_details(work_item_id, "client_spec_conflict_autofixing")[0]
        assert detected["specs"] == [_PREEXISTING_SPEC], "qual spec"
        assert _SUBJECT_TS in detected["diff_files"], "o diff que a invalidou"
        assert detected["expected_vs_received"], "as asserções que reprovaram"

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_conflict_reflags_after_a_retry_that_touches_another_file(time_skipping_env):
    """Porta 1 v2 — o falso-negativo medido no wi_8edaef39 (2026-08-07): depois
    do retry o Coder tocou SÓ o badge (consertando o próprio import), o sujeito
    da spec pré-existente saiu do diff DO TURNO — mas continua no diff
    ACUMULADO base..HEAD, e a spec continua na lista FAIL. O detector tem que
    comparar contra o acumulado e re-parkear; hoje o item volta ao laço de
    retry e queima o teto. (v3: a primeira falha é a chance do Coder, então o
    primeiro parque vem na 2ª falha e o re-flag na 3ª — o invariante do
    acumulado é o mesmo.)"""
    badge = "src/app/shared/components/report-status-badge/report-status-badge.component.ts"
    work_item_id = new_work_item_id("speccum")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=3,
        l1_fail_detail=_L1_DETAIL,
        coder_files_changed_by_turn=[
            [_SUBJECT_TS, _SUBJECT_HTML, badge],  # turno 1: implementação completa
            [badge],                              # turno da chance (v3): só o badge
            [badge],                              # turno pós-retry humano: só o badge
        ],
        tester_test_files=["test/dashboard-badge-dse.spec.ts"],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        # EVOLUIU em 2026-08-10 (F2): sem parque, o re-flag não é mais um
        # segundo `spec_conflict_detected` e sim a segunda linha de
        # `client_spec_conflict_autofixing`. O INVARIANTE do wi_8edaef39 é o
        # mesmo e é o que este teste existe para defender: a detecção compara
        # contra o diff ACUMULADO, então o sujeito continua sendo visto depois
        # de um turno que tocou só o badge.
        # A espera é pela LINHA DE AUDITORIA, não pelo status — a lição que este
        # teste já carregava: sem parque o item roda várias rodadas completas e
        # o status só assenta no fim.
        import asyncio as _asyncio
        for _ in range(240):
            if len(_audit_details(work_item_id, "client_spec_conflict_autofixing")) >= 2:
                break
            await _asyncio.sleep(0.25)
        detected = _audit_details(work_item_id, "client_spec_conflict_autofixing")
        assert len(detected) >= 2, (
            "o conflito tem que re-flagar enquanto a spec segue FAIL"
        )
        assert _SUBJECT_TS in detected[1]["diff_files"], "diff acumulado, não o do turno"
        result = await handle.result()
    # Sem parque, uma spec que nunca é consertada encerra pelo breaker de
    # não-convergência — a mesma queixa duas vezes depois de edições reais.
    # É o freio que substitui a espera humana, e ele existia antes desta
    # mudança: o item não fica girando nem espera para sempre.
    assert result.status == WorkItemStatus.escalated.value
    assert "coder_not_converging" in (result.detail or ""), result.detail


@pytest.mark.asyncio
async def test_tester_owned_spec_failure_stays_in_the_normal_flow(time_skipping_env):
    """DoD 4: a spec do PRÓPRIO Tester falhando segue no fluxo de sempre
    (l1_failed_retrying -> novo turno de Coder), nunca em spec_conflict — o
    Tester pode repará-la; escalar aqui seria regressão."""
    work_item_id = new_work_item_id("specown")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=1,
        l1_fail_detail="FAIL test_app.py\nexpect boom\n",
        coder_files_changed=["app.py"],
        tester_test_files=["test_app.py"],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        actions = read_audit_actions(work_item_id)
        assert "l1_failed_retrying" in actions
        assert "spec_conflict_detected" not in actions
        assert state.coder_turn_calls == 2
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value
