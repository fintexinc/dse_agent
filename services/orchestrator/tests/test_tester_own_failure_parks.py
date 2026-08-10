"""Quando os testes do PRÓPRIO Tester esgotam o cap, o item parqueia com
botões — nunca morre mudo.

Medido 2026-08-10 (wi_cb6656b6 e wi_c911c197, e a família toda do dia): o
Tester escreve testes que NÃO COMPILAM (`typecheck_failed`, returncode 2). O
defer não se aplica — isso já está certo: um erro de compilação é veredito
sobre o CÓDIGO, não desacordo entre teste e código. O laço então gasta turnos
do Coder tentando consertar teste alheio (que a proteção de instrumento
reverte) até `tester_retry_cap_exhausted` → `failed`, sem dossiê e sem saída
para o humano.

O parque de exaustão de spec própria do Tester já existe (é onde o veredito
`reauthor` vive) e é exatamente esta situação: o dono do defeito é o Tester, e
só um humano pode mandá-lo reescrever. Este teste exige que o cap chegue LÁ em
vez de morrer.
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

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
_TESTER_SPEC = "src/test/java/com/acme/AdvisorFeeCalculationServiceTest.java"


@pytest.mark.asyncio
async def test_the_testers_own_uncompilable_specs_park_instead_of_dying(time_skipping_env):
    work_item_id = new_work_item_id("testercompile")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        tester_test_files=[_TESTER_SPEC],
        # o Tester falha SEMPRE: o que ele escreveu não compila (rc=2), então
        # `tests_passed=False` e o defer não se aplica (já é o comportamento
        # correto hoje — o erro é do CÓDIGO, não desacordo teste-vs-código).
        tester_tests_passed=False,
        tester_returncode=2,
        tester_failure_output=(
            "[ERROR] /workspace/src/test/java/com/acme/"
            "AdvisorFeeCalculationServiceTest.java:[42,9] cannot find symbol\n"
            "  symbol:   method setRetired(boolean)\n"
        ),
        coder_files_changed_by_turn=[["src/main/App.java"]],
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
        status = await wait_for_status(handle, {"spec_conflict", "failed", "escalated"})
        assert status == "spec_conflict", (
            "testes do Tester que não compilam esgotando o cap têm que PARQUEAR "
            "com botões (o Reauthor manda o DONO reescrever) — morrer em "
            "tester_retry_cap_exhausted deixa o humano sem saída e sem dossiê"
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
        assert details.get("reason") == "tester_spec_exhaustion", (
            "é o parque de spec PRÓPRIA do Tester — o único onde reauthor existe"
        )
        assert _TESTER_SPEC in (details.get("specs") or []), (
            "o dossiê nomeia o que o Tester escreveu e não compila"
        )

        await handle.signal("spec_conflict_resolution",
                            {"verdict": "escalate", "actor": "usr_test"})
        await handle.result()
