"""A contradição do plano vira uma PERGUNTA, e quem responde é o humano.

Todo plano deste sistema carrega duas listas que ninguém correlaciona:
`expected_files` ("vou escrever aqui", do Planner) e `forbidden_paths` ("aqui
não se escreve", da plataforma). Quando elas se cruzam, a tarefa é impossível
por construção — e foi exatamente isso que aconteceu com um pedido banal de
CI: o Planner declarou `.github/workflows/ci.yml`, a plataforma anexou
`.github/workflows/` ao mesmo plano, e o único diff que passaria no gate L1
era o diff que não entrega o pedido. ~US$ 4 e 40 min até
`coder_not_converging`.

A interseção já era calculada em DOIS lugares antes de gastar um centavo
(`sessions.classify_risk_class` e `policy.classify_risk`) — e as duas viravam
um rótulo ("high"), nunca uma pergunta. Pior: o humano vê as duas listas lado
a lado no modal, sem nenhuma correlação, e o `Risk: high` não diz por quê — o
que faz a contradição PARECER justificativa para aprovar.

Decisão do operador (2026-08-19): a colisão FORÇA o gate humano — qualquer que
seja a classe de risco — e a mensagem nomeia os arquivos. Não escala: escalar
tornaria qualquer tarefa de CI impossível, que é o defeito de hoje com outro
nome.

O caso que este arquivo prova é o que a classificação de risco NÃO pega: um
caminho protegido declarado pelo REPO (`.dse/validation.json`), que não está
entre os marcadores fixos de `policy._HIGH_RISK_PATH_MARKERS`. Sem o patch, o
item auto-aprova e o humano nunca é perguntado.

Roda SEM Postgres, pelo mesmo motivo (e com o mesmo aparato) de
`test_plan_approval_timeout`: o caminho provado aqui é decisão de workflow, não
a escrita da WS-B. O Temporal é o de verdade — servidor time-skipping.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator import policy
from dse_orchestrator.workflows import (
    STATUS_AWAITING_PLAN_APPROVAL,
    WorkItemLifecycleWorkflow,
)

from conftest import new_work_item_id
from fakes import FakeControlPlane

# O harness db-free (ledger + fakes das activities de Postgres) mora em
# test_plan_approval_timeout: são ~80 linhas de fakes que já existem e que
# precisam ficar em UM lugar só — cada activity nova exige um fake em CADA
# harness, e a lição de não duplicá-los está paga (NotFoundError retry storm).
from test_plan_approval_timeout import (
    _Ledger,
    _gate_input,
    _wait_for_audit,
    build_db_free_activities,
)

#: Um caminho protegido que só o REPO declara: não está em
#: `policy._HIGH_RISK_PATH_MARKERS`, então a classificação de risco continua
#: "low" e o item auto-aprovaria.
_PROTEGIDO = "config/production/"
_ARQUIVO = "config/production/app.yaml"


@pytest.fixture(autouse=True)
def _require_postgres():
    """Sobrescreve o skip do conftest: aqui não há Postgres nenhum a exigir."""
    yield


@pytest.fixture(autouse=True)
def _reset_codeowners():
    policy.set_codeowners_reader(None)
    yield
    policy.set_codeowners_reader(None)


@pytest.mark.asyncio
async def test_a_repo_declared_protected_path_parks_a_low_risk_plan(time_skipping_env):
    """Risco baixo, aprovação automática pela política — e mesmo assim o item
    PARA, porque o plano precisa escrever onde o repo proibiu."""
    work_item_id = new_work_item_id("protected-gate")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    state = FakeControlPlane(
        plan_risk_class="low",
        plan_expected_files=[_ARQUIVO],
        plan_forbidden_paths=[_PROTEGIDO],
    )
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id,
                        plan_approval_reminder_hours=1.0,
                        plan_approval_timeout_hours=2.0),
            id=work_item_id, task_queue=task_queue)
        # Ninguém responde: o item expira sozinho e o teste lê o ledger inteiro.
        result = await handle.result()

    actions = ledger.audit_actions
    assert "plan_auto_approved" not in actions, (
        "risco baixo auto-aprovou um plano que precisa escrever em caminho "
        "protegido — o humano nunca foi perguntado"
    )
    assert "plan_requires_protected_paths" in actions, actions
    detalhe = ledger.audit_details("plan_requires_protected_paths")
    assert detalhe["files"] == [_ARQUIVO]
    assert detalhe["forbidden_paths"] == [_PROTEGIDO]
    assert detalhe["risk_class"] == "low", (
        "o veredito não é sobre risco: é sobre autorização"
    )

    # E o item de fato PAROU no gate (não seguiu para o Coder).
    assert STATUS_AWAITING_PLAN_APPROVAL in ledger.comment_statuses
    assert state.provision_calls == 0 and state.coder_turn_calls == 0
    assert result.status == WorkItemStatus.escalated.value


@pytest.mark.asyncio
async def test_the_gate_message_names_the_files_it_is_authorising(time_skipping_env):
    """O humano não pode aprovar sem ler QUAIS arquivos ele está autorizando —
    é essa aprovação que o L1 passa a honrar depois (e só ela)."""
    work_item_id = new_work_item_id("protected-msg")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    state = FakeControlPlane(
        plan_risk_class="low",
        plan_expected_files=[_ARQUIVO, "src/app.py"],
        plan_forbidden_paths=[_PROTEGIDO],
    )
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id,
                        plan_approval_reminder_hours=1.0,
                        plan_approval_timeout_hours=2.0),
            id=work_item_id, task_queue=task_queue)
        await handle.result()

    gate = [b for s, b in ledger.comments if s == STATUS_AWAITING_PLAN_APPROVAL]
    assert gate, ledger.comments
    assert _ARQUIVO in gate[0], f"a mensagem não nomeia o arquivo: {gate[0]!r}"
    assert "protected" in gate[0].lower(), gate[0]
    assert "src/app.py" not in gate[0], (
        "só os arquivos PROTEGIDOS entram na linha — o resto do plano já está "
        "no modal, e uma lista longa aqui apaga o aviso"
    )
    # Os botões continuam vivos: nada de pseudo-status novo.
    assert set(ledger.comment_statuses) <= {STATUS_AWAITING_PLAN_APPROVAL, "escalated"}


@pytest.mark.asyncio
async def test_without_a_collision_a_low_risk_plan_still_auto_approves(time_skipping_env):
    """Rede de segurança: sem contradição, nada muda — o item de risco baixo
    auto-aprova pela política e ninguém é interrompido.

    Para no instante em que a decisão do gate está tomada: o resto do ciclo de
    vida (Coder, L1, L2, PR, preview) não é o que este arquivo prova, e
    arrastá-lo até `done` só acrescentaria dependências de fake que já quebram
    por conta própria neste harness.
    """
    work_item_id = new_work_item_id("protected-none")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    state = FakeControlPlane(plan_risk_class="low")  # expected_files default: app.py
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id), id=work_item_id, task_queue=task_queue)
        await _wait_for_audit(ledger, "plan_auto_approved")
        await handle.terminate()

    actions = ledger.audit_actions
    assert "plan_requires_protected_paths" not in actions
    assert STATUS_AWAITING_PLAN_APPROVAL not in ledger.comment_statuses
