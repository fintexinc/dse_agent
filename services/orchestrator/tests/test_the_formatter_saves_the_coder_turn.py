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

import asyncio
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


async def _espera_revalidacao(ledger: _Ledger, attempts: int = 400) -> None:
    """Espera o audit da SEGUNDA validação (`l1_completed` com
    after=lint_autofix). Esperar `lint_autofixed` e terminar era corrida: esse
    audit nasce ANTES do segundo run_l1_pipeline, e o terminate chegava com o
    calls_log em qualquer estado — 3/5 vermelho local, dois CIs seguidos."""
    for _ in range(attempts):
        if any(a == "l1_completed" and d.get("after") == "lint_autofix"
               for a, d in ledger.audit):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"a revalidação pós-autofix nunca apareceu; audit={[a for a, _ in ledger.audit]}"
    )


async def _roda(env, state: FakeControlPlane, ledger: _Ledger, espera):
    work_item_id = new_work_item_id("autofix")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    async with Worker(env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _gate_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        if callable(espera):
            await espera(ledger)
        else:
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
    await _roda(time_skipping_env, state, ledger, _espera_revalidacao)

    assert "lint_autofix" in state.calls_log
    # duas validações com o autofix entre elas e NENHUM turno de Coder no
    # meio — a revalidação de graça. Propriedade de ORDEM de propósito:
    # contagens globais ("== 1", ">= 2") corriam contra o terminate, porque o
    # workflow segue rodando (Coder → L1 da rodada 2...) até ele pousar.
    assert _revalidacoes_de_graca(state.calls_log) == [True], (
        f"esperava exatamente o par autofix→revalidação; log={state.calls_log}"
    )


def _revalidacoes_de_graca(calls: list[str]) -> list[bool]:
    """Para cada par consecutivo de run_l1_pipeline SEM run_coder_turn entre
    eles (uma revalidação que não custou modelo), diz se houve lint_autofix no
    meio. Estável em qualquer prefixo do log: um par só entra quando o segundo
    run já aconteceu, e o Coder de um par pago sempre vem antes dele."""
    idx = [i for i, c in enumerate(calls) if c == "run_l1_pipeline"]
    return [
        "lint_autofix" in calls[a + 1:b]
        for a, b in zip(idx, idx[1:])
        if "run_coder_turn" not in calls[a + 1:b]
    ]


@pytest.mark.asyncio
async def test_a_repo_without_the_command_pays_the_model_as_before(time_skipping_env):
    """Ausência declarada não muda nada: sem `commands.lint_fix`, o laço é o de
    sempre e nenhuma revalidação de graça acontece — toda validação além da
    primeira custou um turno de Coder."""
    state = FakeControlPlane(plan_risk_class="low",
                             l1_findings_by_call=[_lint_vermelho()])
    ledger = _Ledger()
    await _roda(time_skipping_env, state, ledger, "l1_failed_retrying")

    # A ACTIVITY roda mesmo assim — é ela que lê o manifesto e no-opa sem o
    # comando; o que não pode existir é efeito: nem audit, nem revalidação.
    assert "lint_autofixed" not in ledger.audit_actions
    assert _revalidacoes_de_graca(state.calls_log) == []


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
    assert _revalidacoes_de_graca(state.calls_log) == []
