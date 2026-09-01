"""Barreira de plano do grupo: irmão não implementa com plano do grupo no gate.

Auditoria 2026-08-14 (wi_d8294fed BE high + wi_e15f4991 FE low): o FE nasceu
32s depois do primário, classificou `low` sozinho (decisão de 08-13: irmão
não herda cross_repo), não teve gate nenhum e abriu a PR #26 enquanto o
plano do BE ainda esperava aprovação humana — o gate de risco do grupo era
contornável pelo membro de menor risco. Decisão do operador (08-14):
BARREIRA DE GRUPO — nenhum membro entra em implementing enquanto houver
plano do grupo aguardando aprovação; rejeição/queda de um membro aborta os
outros.

O seam é a local activity `check_group_plan_gate` (scriptável aqui): o
workflow pergunta, ela responde {holding, abort, reason}. Roda sem
Postgres, como test_ci_wait_replay_guard.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from dse_contracts.activities import ACTIVITY_EMIT_AUDIT, ACTIVITY_POST_TRACKING_COMMENT
from dse_orchestrator.local_activities import (
    LOCAL_ACTIVITY_ESTIMATE_PLAN_COST,
    LOCAL_ACTIVITY_POST_STATUS_TRANSITION,
    LOCAL_ACTIVITY_RECORD_GATE,
    LOCAL_ACTIVITY_RECORD_RUN_EPISODE,
    LOCAL_ACTIVITY_UPDATE_STATUS,
    check_clarification_completeness,
    emit_history_metric,
    emit_pr_quality_metric,
    resolve_budget_cap,
    resolve_retry_caps,
)
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import new_work_item_id
from fakes import FakeControlPlane, build_fake_activities

_POLL_CAP = 4


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides conftest's autouse skip — see the module docstring."""
    yield


def _activities(state: FakeControlPlane, posted: list[tuple[str, str]],
                gate_script: list[dict[str, Any]],
                gate_calls: list[dict[str, Any]]) -> list[Any]:
    async def emit_audit_event(payload: dict[str, Any]) -> None:
        return None

    async def update_work_item_status(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def record_plan_approval(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def post_tracking_comment(payload: dict[str, Any]) -> dict[str, Any]:
        posted.append((str(payload.get("status", "")),
                       str(payload.get("body") or payload.get("detail") or "")))
        return {"ok": True}

    async def post_status_transition(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "target_status": None}

    async def record_run_episode(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def estimate_plan_cost(payload: dict[str, Any]) -> dict[str, Any]:
        return {"available": False}

    async def record_evidence_state(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def record_skill_episode(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def preview_enabled_for_repo(payload: dict[str, Any]) -> dict[str, Any]:
        return {"enabled": False, "reason": "db_free_test"}

    async def fan_out_sibling_work_items(payload: dict[str, Any]) -> dict[str, Any]:
        return {"created": [], "group_id": payload.get("work_item_id", "")}

    async def resolve_plan_approver(payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def route_repos(payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def load_work_item(payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def check_group_plan_gate(payload: dict[str, Any]) -> dict[str, Any]:
        gate_calls.append(dict(payload))
        # roteiro: consome uma resposta por chamada; a última fica valendo
        resp = gate_script.pop(0) if len(gate_script) > 1 else gate_script[0]
        return dict(resp)

    return [
        activity.defn(name=ACTIVITY_EMIT_AUDIT)(emit_audit_event),
        activity.defn(name=ACTIVITY_POST_TRACKING_COMMENT)(post_tracking_comment),
        activity.defn(name=LOCAL_ACTIVITY_UPDATE_STATUS)(update_work_item_status),
        activity.defn(name=LOCAL_ACTIVITY_RECORD_GATE)(record_plan_approval),
        activity.defn(name=LOCAL_ACTIVITY_POST_STATUS_TRANSITION)(post_status_transition),
        activity.defn(name=LOCAL_ACTIVITY_RECORD_RUN_EPISODE)(record_run_episode),
        activity.defn(name=LOCAL_ACTIVITY_ESTIMATE_PLAN_COST)(estimate_plan_cost),
        activity.defn(name="record_evidence_state")(record_evidence_state),
        activity.defn(name="record_skill_episode")(record_skill_episode),
        activity.defn(name="preview_enabled_for_repo")(preview_enabled_for_repo),
        activity.defn(name="fan_out_sibling_work_items")(fan_out_sibling_work_items),
        activity.defn(name="resolve_plan_approver")(resolve_plan_approver),
        activity.defn(name="route_repos")(route_repos),
        activity.defn(name="load_work_item")(load_work_item),
        activity.defn(name="check_group_plan_gate")(check_group_plan_gate),
        # Reais: puros, env-only ou OTel-only.
        check_clarification_completeness,
        resolve_budget_cap,
        resolve_retry_caps,
        emit_history_metric,
        emit_pr_quality_metric,
    ] + build_fake_activities(state)


def _wf_input(work_item_id: str) -> WorkItemLifecycleInput:
    return WorkItemLifecycleInput(
        work_item_id=work_item_id,
        tenant_id="test-tenant",
        requester="usr_test",
        repo="acme/repo",
        base_branch="main",
        acceptance_criteria="crit",
        ci_poll_interval_seconds=0.01,
        ci_pending_poll_cap=_POLL_CAP,
        ci_wait_deadline_hours=0,
    )


async def _run(time_skipping_env, gate_script: list[dict[str, Any]],
               posted: list[tuple[str, str]], gate_calls: list[dict[str, Any]],
               prefix: str):
    work_item_id = new_work_item_id(prefix)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(ci_sequence=["pending"] * (_POLL_CAP + 2))
    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      workflow_runner=UnsandboxedWorkflowRunner(),
                      activities=_activities(state, posted, gate_script, gate_calls)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
        # rc.130: the poll cap PARKS the item for review instead of escalating
        # it, so the run no longer closes by itself: the human answers once the
        # item is at the park. Two runner facts shape the loop below — a signal
        # sent up front is lost at the intake→implementation continue_as_new,
        # and polling queries starve the time-skipping clock the barrier's own
        # timers need (250 ms polls left the item `queued` for a minute), so the
        # clock is advanced EXPLICITLY between looks. A member of a dead group
        # escalates before ever reaching the park.
        status = None
        for _ in range(400):
            try:
                status = (await handle.query(WorkItemLifecycleWorkflow.get_state)).get("status")
            except Exception:  # noqa: BLE001 — a run mid-continue_as_new refuses queries
                status = None
            if status in ("review_ready", "escalated", "failed", "done", "blocked", "cancelled"):
                break
            await time_skipping_env.sleep(timedelta(minutes=5))
        if status == "review_ready":
            await handle.signal("review_comment", {"verdict": "approved"})
            await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        return await handle.result()


@pytest.mark.asyncio
async def test_a_sibling_waits_for_the_groups_gate(time_skipping_env):
    """Enquanto a activity diz holding, NADA de implementing; a superfície
    avisa por que o item está parado; liberou → o fluxo segue normal."""
    posted: list[tuple[str, str]] = []
    gate_calls: list[dict[str, Any]] = []
    script = [
        {"in_group": True, "holding": True, "abort": False,
         "reason": "primary awaiting_plan_approval"},
        {"in_group": True, "holding": True, "abort": False,
         "reason": "primary awaiting_plan_approval"},
        {"in_group": True, "holding": False, "abort": False, "reason": ""},
    ]
    await _run(time_skipping_env, script, posted, gate_calls, "grpwait")

    assert len(gate_calls) >= 3, (
        f"a barreira tem que PERGUNTAR de novo enquanto holding — {gate_calls}"
    )
    statuses = [s for s, _ in posted]
    assert "implementing" in statuses, f"pré-condição: o fluxo nem chegou ao coder — {posted}"
    barrier_idx = [i for i, (_, body) in enumerate(posted) if "group" in body.lower()]
    assert barrier_idx, (
        f"a espera pelo grupo não apareceu na superfície (posts: {posted})"
    )
    assert barrier_idx[0] < statuses.index("implementing"), (
        "o aviso da barreira tem que vir ANTES do implementing"
    )


@pytest.mark.asyncio
async def test_a_dead_group_plan_aborts_the_member(time_skipping_env):
    """Plano do grupo rejeitado (cancel → failed) ou primário escalado: o
    membro NÃO implementa — termina escalated com o motivo."""
    posted: list[tuple[str, str]] = []
    gate_calls: list[dict[str, Any]] = []
    script = [
        {"in_group": True, "holding": False, "abort": True,
         "reason": "primary wi_d8294fed is failed (plan_rejected_cancel)"},
    ]
    result = await _run(time_skipping_env, script, posted, gate_calls, "grpdead")

    assert result.status == "escalated", (
        f"membro de grupo morto tem que escalar, não {result.status!r}"
    )
    statuses = [s for s, _ in posted]
    assert "implementing" not in statuses, (
        f"o membro implementou mesmo com o grupo morto — {posted}"
    )
