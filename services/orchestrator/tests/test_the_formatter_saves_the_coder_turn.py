"""O laço chama o formatador do repo antes de comprar um turno de modelo.

Medido no calculation-engine: quatro turnos de Coder (~US$ 4, ~14 minutos) sem
convergir numa ordem de imports que `spotless:apply` conserta em 7 segundos.

O passo vive ENTRE a chamada do L1 e a leitura do veredito, de propósito: o
conserto e a revalidação ficam contidos ali, sem tocar na forma do laço, e o
GATE continua sendo quem diz se acabou — nunca o comando que acabou de editar
os arquivos.

Nada disso é a plataforma conhecendo `spotless`. É o repositório declarando
`commands.lint_fix`, do mesmo jeito que declara `preview.start`, `install`,
`commands.test_subset` e `reports.junit` — e toda linguagem tem um formatador
com modo de escrita, então a chave é a mesma para todas."""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_orchestrator import policy
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import new_work_item_id
from dse_contracts import L1Finding
from fakes import FakeControlPlane
from test_plan_approval_timeout import (
    _Ledger,
    _gate_input,
    _wait_for_audit,
    build_db_free_activities,
)


def _lint_vermelho() -> list:
    """Uma rodada reprovada pelo LINT — o único gate que convida o conserto."""
    return [L1Finding(check="lint", passed=False,
                      detail="1 lint issue(s) in the files this change touched",
                      summary="1 lint issue(s)")]


async def _roda(env, state: FakeControlPlane, ledger: _Ledger, espera: str):
    work_item_id = new_work_item_id("autofix")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    async with Worker(env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _gate_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await _wait_for_audit(ledger, espera)
        await handle.terminate()


@pytest.mark.asyncio
async def test_a_declared_formatter_is_run_and_the_gate_decides_again(time_skipping_env):
    """O ganho inteiro numa asserção: o L1 roda DUAS vezes na mesma rodada — a
    segunda sobre o que o formatador escreveu — e nenhum turno de Coder foi
    comprado no meio."""
    state = FakeControlPlane(plan_risk_class="low", lint_autofix_changes=True,
                             l1_findings_by_call=[_lint_vermelho()])
    ledger = _Ledger()
    await _roda(time_skipping_env, state, ledger, "lint_autofixed")

    assert "lint_autofix" in state.calls_log
    # duas validações, um único turno de Coder antes delas
    assert state.calls_log.count("run_l1_pipeline") >= 2
    assert state.calls_log.index("lint_autofix") < len(state.calls_log) - 1


@pytest.mark.asyncio
async def test_a_repo_without_the_command_pays_the_model_as_before(time_skipping_env):
    """Ausência declarada não muda nada: sem `commands.lint_fix`, o laço é o de
    sempre e nenhuma revalidação extra acontece."""
    state = FakeControlPlane(plan_risk_class="low",
                             l1_findings_by_call=[_lint_vermelho()])
    ledger = _Ledger()
    await _roda(time_skipping_env, state, ledger, "l1_failed_retrying")

    assert "lint_autofixed" not in ledger.audit_actions
    assert state.calls_log.count("run_l1_pipeline") == 1


@pytest.mark.asyncio
async def test_a_formatter_that_changes_nothing_hands_the_turn_back(time_skipping_env):
    """Formatador que roda e não altera arquivo significa que a reprovação NÃO
    era de formatação. Revalidar de novo seria um laço infinito barato em vez
    de um caro."""
    state = FakeControlPlane(plan_risk_class="low", lint_autofix_changes=False,
                             l1_findings_by_call=[_lint_vermelho()])
    ledger = _Ledger()
    await _roda(time_skipping_env, state, ledger, "l1_failed_retrying")

    assert "lint_autofix" in state.calls_log
    assert "lint_autofixed" not in ledger.audit_actions
    assert state.calls_log.count("run_l1_pipeline") == 1
