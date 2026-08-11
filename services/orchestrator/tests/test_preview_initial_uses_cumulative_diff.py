"""O preview INICIAL classifica a PR pelo diff do último turno — e perde a PR.

Medido em produção (2026-08-10), com a cadeia inteira no ledger:

    20:21:22  coder_turn_completed  files_changed: []
    20:25:21  coder_turn_completed  files_changed: [4 arquivos .java/.sql]
    20:27:59  coder_turn_completed  files_changed: []
    20:29:18  coder_turn_completed  files_changed: []      ← o último turno
    20:36:59  pr_finalized          PR #8
    20:37:01  preview_skipped_backend_only  files_changed: []

A PR #8 do BE tinha quatro arquivos `.java`/`.sql`, e `**/*.java` está nos
`deployable_globs` registrados no próprio evento. Com o diff acumulado o
paths-filter teria devolvido `deployable` e o preview teria sido tentado. Com o
diff do último turno — um no-op — ele devolve `none` e o item vai para a PR sem
preview nenhum, em silêncio.

**Isto é a mesma lição, no mesmo arquivo, pela segunda vez.** O commit e841542
("Refresh usa o diff acumulado") corrigiu exatamente este defeito — mas só num
dos quatro call sites, o do `human_request`. Os outros três continuaram
passando `files_changed` do turno. E a prova de que o acumulado estava à mão:
`finalize_pr`, SESSENTA LINHAS ACIMA na mesma função, já passa
`cumulative_files_changed`.

Honestidade sobre o alcance: isto NÃO explica todas as PRs sem preview. A PR 18
(wi_f1d2d66d) tem o acumulado vazio de verdade — o Coder não escreveu arquivo
nenhum e a PR nasceu só com uma spec do Tester. Aquilo é outro defeito, a
montante, e este teste não o cobre.
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

#: Verbatim da PR #8 (wi_a47c490a): o turno que produziu a mudança, e depois
#: turnos que não moveram arquivo nenhum.
_REAL_WORK = [
    "src/main/java/com/fintex/bmofeecalculatorbe/controller/rest/ReportOptionsController.java",
    "src/main/java/com/fintex/bmofeecalculatorbe/domain/PayoutLevel.java",
    "src/main/resources/db/migration/V20250101000010__add_retired_to_payout_level.sql",
]


@pytest.mark.asyncio
async def test_the_initial_preview_sees_the_whole_pr_not_the_last_turn(time_skipping_env):
    work_item_id = new_work_item_id("previni")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        # Turno 1 faz o trabalho de produção; o turno de conserto mexe em outra
        # coisa. O ÚLTIMO turno antes da PR é o que o preview enxerga hoje — e
        # sozinho ele não classifica como nada.
        #
        # Um turno literalmente VAZIO seria mais fiel ao run real, mas o freio
        # de no-op mata o item antes da PR (dois no-ops seguidos escalam, e é
        # assim que tem de ser). Este cenário isola a mesma assimetria sem
        # depender do freio: a diferença entre "o último turno" e "a PR".
        coder_files_changed_by_turn=[_REAL_WORK, ["README.md"]],
        l1_fail_times=1,
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
        await wait_for_status(
            handle, {"review_ready", "awaiting_human_review", "failed", "escalated"})

        assert state.trigger_preview_calls >= 1, "o preview nem foi tentado"
        sent = list(state.last_preview_payload.get("files_changed") or [])
        for path in _REAL_WORK:
            assert path in sent, (
                f"{path} não chegou ao paths-filter. O preview recebeu {sent!r}: "
                "o diff do ÚLTIMO turno, não o da PR. É essa assimetria que fez "
                "a PR #8 virar `skipped_backend_only` com quatro arquivos .java "
                "dentro dela"
            )
