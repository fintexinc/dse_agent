"""Um gate que NÃO CONSEGUIU RODAR não é um veredito sobre o código.

Medido em produção (2026-08-11, wi_530a1f56, US$ 18,90): o `lint` do FE morreu
com `exit=134` — SIGABRT, o V8 abortando por heap. O gate voltou com
`status=ERROR`, que no desenho deste repositório significa exatamente "a
ferramenta não produziu veredito", e existe um classificador inteiro
(`_infra_failure`, em quality_checks.py) só para separar isso de "a ferramenta
discordou do código".

O laço ignorava a distinção. `failed_checks` é `[f for f in findings if not
f.passed]`, e um ERROR também tem `passed=False` — então o item consumiu
tentativa, o Coder recebeu "conserte o lint", e foi mexer no `package.json` do
cliente. Duas rodadas a ~US$ 3 cada perseguindo um OOM que nenhum turno de
Coder pode resolver: a causa era o próprio `.dse/validation.json` do repo fixar
`--max-old-space-size=1024`, abaixo dos 9216 MB que o sandbox oferecia.

O molde já existe e é do Tester (`tester-infra-outcome-escalates-v1`): fim de
infra escala com a razão nomeada, carregando as palavras do runtime, em vez de
comprar turnos que repetem a falha. Este teste pede a mesma coisa para o L1.

A fronteira que ele fixa nos dois sentidos:
  - ERROR (a ferramenta caiu) → escala nomeando o gate e a causa, sem gastar o
    teto de tentativas;
  - FAIL (a ferramenta rodou e reprovou) → continua sendo trabalho do Coder,
    exatamente como hoje. Confundir os dois na direção oposta seria pior:
    transformaria defeito real em "problema de infra" e encerraria o item.
"""
from __future__ import annotations

import uuid

import pytest
from dse_contracts import GateStatus
from temporalio.worker import Worker

from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

_OOM = "lint could not run: the process was killed (exit=134)"


def _start(state, work_item_id, env):
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    )
    handle = env.client.start_workflow(
        WorkItemLifecycleWorkflow.run,
        WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/fe", base_branch="main", acceptance_criteria="crit",
        ),
        id=work_item_id, task_queue=task_queue,
    )
    return worker, handle


@pytest.mark.asyncio
async def test_a_gate_that_could_not_run_escalates_instead_of_buying_coder_turns(
    time_skipping_env,
):
    work_item_id = new_work_item_id("l1infra")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=9,                      # nunca passaria sozinho
        l1_error_checks=["lint"],             # o gate CAIU, não reprovou
        l1_fail_detail=_OOM,
    )
    worker, handle_coro = _start(state, work_item_id, time_skipping_env)
    handle = await handle_coro

    async with worker:
        status = await wait_for_status(handle, {"escalated", "failed", "review_ready"})

        assert status == "escalated", (
            f"veio {status}. Um gate que não rodou tem que escalar com a causa, "
            "não morrer no teto depois de comprar turnos de Coder"
        )
        assert state.coder_turn_calls <= 1, (
            f"o Coder foi chamado {state.coder_turn_calls}x para consertar uma "
            "ferramenta que nem rodou — foi assim que o wi_530a1f56 gastou "
            "US$ 18,90 mexendo no package.json do cliente"
        )


@pytest.mark.asyncio
async def test_a_gate_that_ran_and_failed_is_still_the_coders_job(time_skipping_env):
    """PIN da direção oposta, e é o que impede esta correção de virar uma
    desculpa: `test` FAIL é veredito de verdade — a suíte rodou e reprovou — e
    continua comprando turnos do Coder até o teto, como sempre."""
    work_item_id = new_work_item_id("l1fail")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,                      # reprova 2x e depois passa
        l1_fail_detail="FAIL src/app/x.spec.ts\nexpect(3).toBe(4)",
    )
    worker, handle_coro = _start(state, work_item_id, time_skipping_env)
    handle = await handle_coro

    async with worker:
        status = await wait_for_status(
            handle, {"review_ready", "awaiting_human_review", "escalated", "failed"})

        assert status in {"review_ready", "awaiting_human_review"}, (
            f"veio {status}: uma reprovação legítima virou fim de infra, e o "
            "item parou de tentar consertar o que era consertável"
        )
        assert state.coder_turn_calls >= 2, "o Coder tem que ter tentado consertar"


def test_the_classifier_reads_the_gate_status_not_the_message():
    """A distinção vem do STATUS do gate, não de procurar 'killed' no texto:
    mensagem é do runtime e muda; `GateStatus.ERROR` é contrato."""
    from dse_orchestrator.workflows import _l1_infra_gates

    class _F:
        def __init__(self, check, status, passed, detail=""):
            self.check, self.status, self.passed, self.detail = check, status, passed, detail

    caiu = _F("lint", GateStatus.ERROR, False, _OOM)
    reprovou = _F("test", GateStatus.FAIL, False, "expect(3).toBe(4)")
    passou = _F("build", GateStatus.PASS, True)

    assert _l1_infra_gates([caiu, reprovou, passou]) == ["lint"]
    assert _l1_infra_gates([reprovou, passou]) == []
