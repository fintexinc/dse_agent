"""O refresh de evidência classificava a PR pelo diff do ÚLTIMO TURNO.

Medido na aceitação de 2026-08-09 (wi_cc72b204, PR #17): a PR do FE tocou
`.ts` + `.html` + `.scss`, mas o último turno do Coder mexeu só no
`grid-payout.component.ts` (o fix do comparador). O refresh passou
`last_files_changed` ao paths-filter → nenhum glob de UI casou → `kind=deployable`
→ receita Java num repo Angular → `npm: not found`, CrashLoop, degraded.

É a MESMA lição da porta 1 v2, agora no pipeline de evidência: o diff-por-turno
não representa a mudança; o acumulado `base..HEAD` sim (e já existe no input,
sobrevivendo a continue_as_new). Este vermelho reproduz o run verbatim e passa
SEM depender da correção dos globs (o `.html` do primeiro turno é o que
classifica como UI) — a prova de que a causa é a fonte do diff, não o glob.
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

#: A PR do FE, turno a turno (verbatim do wi_cc72b204).
_TURN_1 = [
    "src/app/admin/grid-payout/grid-payout.component.ts",
    "src/app/admin/grid-payout/grid-payout.component.html",
    "src/app/admin/grid-payout/grid-payout.component.scss",
]
_TURN_2 = ["src/app/admin/grid-payout/grid-payout.component.ts"]


@pytest.mark.asyncio
async def test_the_refresh_classifies_by_the_cumulative_diff_not_the_last_turn(time_skipping_env):
    work_item_id = new_work_item_id("prevcum")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=1,
        l1_fail_detail="FAIL src/app/x.spec.ts\nexpect boom\n",
        coder_files_changed_by_turn=[_TURN_1, _TURN_2],
        tester_test_files=["src/app/x-dse.spec.ts"],
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
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("refresh_evidence", {})
        # o refresh é servido dentro do laço de review; espera a 2ª chamada
        import asyncio
        for _ in range(120):
            if state.trigger_preview_calls >= 2:
                break
            await asyncio.sleep(0.25)

        files = list((state.last_preview_payload or {}).get("files_changed") or [])
        assert any(f.endswith(".html") for f in files), (
            "o refresh tem que classificar pelo diff ACUMULADO da PR — com o "
            "diff do último turno (só .ts) o FE Angular vira 'deployable' e "
            "roda a receita Java (npm: not found, medido no wi_cc72b204)"
        )
        assert set(_TURN_1).issubset(set(files))

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        await handle.result()
