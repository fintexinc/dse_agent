"""No-ops do Coder CAUSADOS por reversão de instrumento parqueiam — nunca
escalam cegos.

Medido no wi_36acfb3d (2026-08-10): o Tester autorou
AdvisorFeeCalculationServiceTest.java (sem convenção -dse no nome) e os testes
dele NÃO COMPILAM. O L1 reprova, o laço manda o CODER, o Coder edita os
arquivos do Tester, a reversão de instrumento (correta) desfaz — dois turnos
"vazios" e a escalação diz `coder_made_no_change`, culpando o ator errado e
sem dossiê.

A pinça de exaustão já existia (noop-pincer-parks-v1) mas só armava com specs
do Tester COM veredito — testes que nem compilam são zero-veredito (porta 5) e
ficavam fora. O sinal que faltava: o post_turn JÁ calcula `reverted_tests`;
agora ele viaja no CoderTurnResult e dois no-ops causados por reversão armam a
MESMA pinça — parque tester_spec_exhaustion com o dossiê, botões da rc.59
(Retry/Reauthor/Escalar) na thread, humano decide.
"""
from __future__ import annotations

import json
import uuid

import psycopg2
import pytest
from temporalio.worker import Worker

from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

_TESTER_FILE = "src/test/java/com/acme/AdvisorFeeCalculationServiceTest.java"
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


@pytest.mark.asyncio
async def test_noops_caused_by_instrument_reverts_park_with_the_dossier(time_skipping_env):
    work_item_id = new_work_item_id("revpincer")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=3,
        l1_fail_detail="[ERROR] /workspace/" + _TESTER_FILE + ": cannot find symbol\n",
        coder_files_changed_by_turn=[["src/main/App.java"], [], []],
        coder_reverted_test_paths_by_turn=[[], [_TESTER_FILE], [_TESTER_FILE]],
        tester_test_files=[_TESTER_FILE],
    )
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    )
    wf_input = WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/be", base_branch="main", acceptance_criteria="crit",
    )
    handle = await time_skipping_env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)

    async with worker:
        status = await wait_for_status(handle, {"spec_conflict", "escalated", "failed"})
        assert status == "spec_conflict", (
            "no-ops causados por reversão de instrumento têm que PARQUEAR com "
            "dossiê e botões — 'coder_made_no_change' culpa o ator errado "
            "(wi_36acfb3d: o defeito era do TESTER, testes que nem compilam)"
        )
        conn = psycopg2.connect(DSN)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT details FROM audit_log WHERE work_item_id=%s AND "
                    "action='spec_conflict_detected' ORDER BY id DESC LIMIT 1",
                    (work_item_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        details = row[0] if row and isinstance(row[0], dict) else json.loads(row[0]) if row else {}
        assert _TESTER_FILE in (details.get("specs") or []), (
            "o dossiê nomeia o arquivo do Tester que causou as reversões"
        )
        assert details.get("reason") == "tester_spec_exhaustion", (
            "o parque é o de spec própria do Tester — é onde o Reauthor existe"
        )

        # encerra o teste: humano decide escalate (qualquer veredito serve aqui)
        await handle.signal("spec_conflict_resolution", {"verdict": "escalate",
                                                        "actor": "usr_test"})
        await handle.result()
