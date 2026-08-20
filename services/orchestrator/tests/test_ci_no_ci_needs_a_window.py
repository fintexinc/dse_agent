"""\"Este repositório não tem CI\" não pode ser decidido no primeiro segundo.

Auditoria de 2026-08-20. A primeira consulta de CI roda SEM sleep e sem piso de
tempo, e o ramo `no_ci` é terminal na mesma passada: audita, escreve na PR
"⚠️ **This repository has no CI** … You are the only gate" e libera para review.

A corrida é concreta: workflows de `on: pull_request` são criados pelo GitHub no
instante em que a PR abre — o DSE consulta antes de eles existirem. Os de
`on: push` já existiam (o push do Coder foi rodadas antes), por isso o defeito
não aparece em todo repo.

O contraste está no mesmo arquivo: o estado `pending` tem teto de 1440 polls e
deadline de 6h. A paciência existe para "ainda não terminou" e não existe para
"ainda não apareceu" — e a segunda é a que produz uma afirmação FALSA na PR do
cliente.

Regra que estes testes fixam: a PRIMEIRA observação de `no_ci` conta como
`pending`; só depois de a janela passar (~90s) a ausência vira veredito. Se o
repo realmente não tem CI, custa um poll.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator import policy
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import new_work_item_id, wait_for_status
from fakes import FakeControlPlane

from test_plan_approval_timeout import _Ledger, _gate_input, build_db_free_activities


@pytest.fixture(autouse=True)
def _require_postgres():
    yield


@pytest.fixture(autouse=True)
def _reset_codeowners():
    policy.set_codeowners_reader(None)
    yield
    policy.set_codeowners_reader(None)


async def _corre(env, state: FakeControlPlane, ledger: _Ledger):
    work_item_id = new_work_item_id("nocijanela")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    async with Worker(env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id), id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        return await handle.result()


@pytest.mark.asyncio
async def test_ci_that_appears_a_little_late_is_not_declared_absent(time_skipping_env):
    """O caso real: a primeira leitura não vê check run nenhum; a segunda vê o
    workflow de `on: pull_request` já registrado e verde."""
    state = FakeControlPlane(ci_sequence=["no_ci", "green"])
    ledger = _Ledger()
    result = await _corre(time_skipping_env, state, ledger)

    assert result.status == WorkItemStatus.done.value
    assert "ci_no_ci_detected" not in ledger.audit_actions, (
        "o DSE afirmou na PR que o repositório não tem CI porque perguntou cedo "
        "demais — e o CI apareceu na leitura seguinte"
    )
    corpos = " ".join(b for _s, b in ledger.comments)
    assert "has no CI" not in corpos


@pytest.mark.asyncio
async def test_a_repo_that_really_has_no_ci_still_gets_the_notice(time_skipping_env):
    """A rede: silêncio persistente continua virando veredito — só que depois
    da janela, não no primeiro segundo."""
    state = FakeControlPlane(ci_sequence=["no_ci", "no_ci", "no_ci"])
    ledger = _Ledger()
    result = await _corre(time_skipping_env, state, ledger)

    assert result.status == WorkItemStatus.done.value
    assert "ci_no_ci_detected" in ledger.audit_actions
    corpos = " ".join(b for _s, b in ledger.comments)
    assert "has no CI" in corpos
