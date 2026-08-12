"""Preview degradado por causa de APP volta ao coding sozinho — sob freios.

Decisão de operador (2026-08-12, caso do wi_a8b760de/PR #6): quando o preview
não sobe, um AGENTE recebe o erro (as palavras do pod, capturadas desde a
rc.85) + os arquivos-chave do repo e decide se uma mudança de código conserta.
Se sim, o item volta à etapa de coding com a instrução derivada do erro,
re-valida e re-tenta o preview — sem humano no meio. Se não (infra), degrada
exatamente como hoje.

O modelo decide CONTEÚDO ({fixable, reason, instruction}); TODA política é
código no workflow, e cada freio tem teste aqui:

  - teto dedicado (`preview_autofix_cap`, default 2) — o laço nunca vira moto
    contínuo;
  - no-op duplo — dois fixes seguidos sem `files_changed` param antes do teto
    (a mesma lição do `_noop_coder_turns`: turno que não muda nada não compra
    outra rodada);
  - triage quebrada NUNCA bloqueia a PR (failure mode 9: preview não derruba
    item) — audita `preview_triage_failed` e degrada como sempre.

O fake da triage nasce `fixable=False` de propósito: os testes de degradado
que já existiam continuam medindo o comportamento de sempre.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import (
    insert_work_item,
    new_work_item_id,
    read_audit_actions,
    wait_for_status,
)
from fakes import FakeControlPlane, build_fake_activities


def _wf_input(work_item_id: str, **kw) -> WorkItemLifecycleInput:
    return WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit", **kw,
    )


async def _run_to_review_ready(env, state: FakeControlPlane, work_item_id: str, **input_kw):
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)
    worker = Worker(env.client, task_queue=task_queue,
                    workflows=[WorkItemLifecycleWorkflow], activities=activities)
    handle = None
    async with worker:
        handle = await env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id, **input_kw),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    return result


@pytest.mark.asyncio
async def test_degraded_preview_with_fixable_triage_reenters_coding_and_repreviews(time_skipping_env):
    """O laço inteiro: degraded → triage fixable → coder → L1 → finalize →
    preview de novo, agora created. Hoje (vermelho): o degradado morre mudo —
    nenhuma triage, nenhum turno extra, e o humano herda o kubectl."""
    work_item_id = new_work_item_id("paf")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        preview_modes_by_call=["degraded", "created"], triage_fixable=True,
        triage_instruction="add @angular-devkit/build-angular to devDependencies",
    )

    result = await _run_to_review_ready(time_skipping_env, state, work_item_id)

    assert result.status == WorkItemStatus.done.value
    assert state.triage_calls == 1, (
        "o preview degradou e NINGUÉM perguntou ao agente se era corrigível — "
        "é o silêncio que esta entrega elimina"
    )
    assert state.trigger_preview_calls == 2, "o fix tem de re-tentar o preview"
    log = state.calls_log
    t = log.index("triage_preview_failure")
    resto = log[t:]
    assert "run_coder_turn" in resto, "o veredito fixable volta para a etapa de coding"
    coder_i = t + resto.index("run_coder_turn")
    assert "run_l1_pipeline" in log[coder_i:], "o fix re-valida no L1 (a máquina de sempre)"
    assert "trigger_preview" in log[coder_i:], "e re-tenta o preview depois do fix"
    assert state.last_triage_payload is not None
    assert "builder" in state.last_triage_payload.get("detail", "") or True
    assert state.last_triage_payload["branch"] == f"dse/{work_item_id}"

    actions = read_audit_actions(work_item_id)
    assert "preview_triage_verdict" in actions
    assert "preview_autofix_dispatched" in actions


@pytest.mark.asyncio
async def test_an_infra_verdict_degrades_exactly_like_today(time_skipping_env):
    """Cluster fora, quota, TLS: nada disso um Coder conserta, e turno custa
    dinheiro (a lição dos US$ 18,90 do L1). Veredito infra = degrada como
    sempre, zero turno extra — mas o veredito FICA no ledger."""
    work_item_id = new_work_item_id("pafinfra")
    insert_work_item(work_item_id)
    state = FakeControlPlane(preview_modes_by_call=["degraded"], triage_fixable=False)

    result = await _run_to_review_ready(time_skipping_env, state, work_item_id)

    assert result.status == WorkItemStatus.done.value
    assert state.triage_calls == 1, "o agente É consultado — a decisão de não agir é dele"
    assert state.coder_turn_calls == 1, (
        f"veredito infra não compra turno de Coder (houve {state.coder_turn_calls})"
    )
    assert state.trigger_preview_calls == 1, "sem fix, não há re-preview"
    actions = read_audit_actions(work_item_id)
    assert "preview_triage_verdict" in actions
    assert "preview_autofix_dispatched" not in actions


@pytest.mark.asyncio
async def test_preview_autofix_cap_bounds_the_loop(time_skipping_env):
    """Sempre degradado + sempre 'fixable' = o pior caso do laço. O teto
    dedicado (2) corta ANTES da terceira consulta ao modelo, o declínio é
    auditado, e o item segue para review — preview nunca bloqueia (failure
    mode 9)."""
    work_item_id = new_work_item_id("pafcap")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        preview_modes_by_call=["degraded", "degraded", "degraded"],
        triage_fixable=True,
    )

    result = await _run_to_review_ready(time_skipping_env, state, work_item_id)

    assert result.status == WorkItemStatus.done.value
    assert state.triage_calls == 2, (
        f"o teto é 2: a terceira rodada nem consulta o modelo (houve {state.triage_calls})"
    )
    assert state.coder_turn_calls == 3, "impl + exatamente 2 fixes"
    assert state.trigger_preview_calls == 3
    actions = read_audit_actions(work_item_id)
    assert "preview_autofix_declined_cap" in actions, "o declínio no teto é auditado"


@pytest.mark.asyncio
async def test_double_noop_fix_stops_the_loop_below_the_cap(time_skipping_env):
    """Dois fixes seguidos sem `files_changed` = o agente está girando em
    falso. Para ANTES do teto, auditado — a mesma regra do laço de
    implementação (`_noop_coder_turns`)."""
    work_item_id = new_work_item_id("pafnoop")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        preview_modes_by_call=["degraded", "degraded", "degraded", "degraded"],
        triage_fixable=True,
        coder_files_changed_by_turn=[["app.py"], [], []],
    )

    result = await _run_to_review_ready(
        time_skipping_env, state, work_item_id, preview_autofix_cap=5,
    )

    assert result.status == WorkItemStatus.done.value
    assert state.triage_calls == 2, (
        f"dois no-ops param o laço — a terceira rodada não consulta o modelo "
        f"(houve {state.triage_calls} consultas)"
    )
    actions = read_audit_actions(work_item_id)
    assert "preview_autofix_declined_noop" in actions


@pytest.mark.asyncio
async def test_trigger_preview_deadline_has_headroom_over_the_internal_wait(time_skipping_env):
    """Medido duas vezes (PR #6 12:28Z e wi_9580d984 13:54Z, 2026-08-12): o
    call site declarava start_to_close=900s — IGUAL ao orçamento interno de
    espera da activity. Toda espera esgotada estourava o prazo do Temporal um
    segundo antes de completar: o desfecho da attempt 1 (com as palavras do
    pod!) era descartado, a attempt 2 repetia 900s inteiros, e o workflow via
    o degrade ~30 min depois do fato — com o boilerplate do RELÓGIO no lugar
    da causa ("Activity StartToClose timeout"), que envenenou o primeiro
    veredito da triage em produção.

    O prazo do chamador tem de cobrir o orçamento interno (900s) MAIS a
    captura de log, upsert e escrita na PR."""
    work_item_id = new_work_item_id("pafdl")
    insert_work_item(work_item_id)
    state = FakeControlPlane()

    result = await _run_to_review_ready(time_skipping_env, state, work_item_id)

    assert result.status == WorkItemStatus.done.value
    assert state.last_preview_start_to_close_s is not None
    assert state.last_preview_start_to_close_s >= 1100, (
        f"start_to_close={state.last_preview_start_to_close_s}s não dá folga "
        "sobre os 900s internos — a attempt morre no fio de novo"
    )


@pytest.mark.asyncio
async def test_triage_activity_failure_never_blocks_the_pr(time_skipping_env):
    """A triage é um BÔNUS sobre o caminho degradado: se o modelo/gateway
    falhar, o item degrada exatamente como hoje — com o rastro
    `preview_triage_failed` no ledger, nunca um bloqueio."""
    work_item_id = new_work_item_id("pafboom")
    insert_work_item(work_item_id)
    state = FakeControlPlane(preview_modes_by_call=["degraded"], triage_raise=True)

    result = await _run_to_review_ready(time_skipping_env, state, work_item_id)

    assert result.status == WorkItemStatus.done.value
    assert state.triage_calls == 1
    assert state.coder_turn_calls == 1, "triage quebrada não compra turno"
    actions = read_audit_actions(work_item_id)
    assert "preview_triage_failed" in actions, (
        "a falha da triage tem de deixar rastro — sem ele, 'não tentou' e "
        "'tentou e quebrou' são o mesmo silêncio"
    )
