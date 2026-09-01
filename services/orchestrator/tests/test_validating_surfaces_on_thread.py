"""A entrada em `validating` tem que EDITAR a superfície, como `implementing`.

Auditoria de 2026-08-13 (operador: "o fe parece que travou"): o item estava
saudável — `run_l1_pipeline` com heartbeat vivo — mas a mensagem do Slack
ficou parada em "implementing / Build" por toda a validação, porque
`_post_status_comment` é chamado em implementing, no gate e nos terminais,
e NUNCA na transição para validating (nem na revalidação do fix loop). Com
a barra de etapas da rc.90, esse silêncio virou mentira visível: Validate
nunca aparece como etapa atual; a barra pula de Build para PR.

Roda sem Postgres, como test_ci_wait_replay_guard: o seam é um fake de
`post_tracking_comment` que grava os statuses postados.
"""
from __future__ import annotations

import uuid
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

from conftest import new_work_item_id, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

_POLL_CAP = 4


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides conftest's autouse skip — see the module docstring."""
    yield


def _recording_activities(state: FakeControlPlane, posted: list[str]) -> list[Any]:
    async def emit_audit_event(payload: dict[str, Any]) -> None:
        return None

    async def update_work_item_status(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def record_plan_approval(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def post_tracking_comment(payload: dict[str, Any]) -> dict[str, Any]:
        posted.append(str(payload.get("status", "")))
        return {"ok": True}

    async def post_status_transition(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "target_status": None}

    async def record_run_episode(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def estimate_plan_cost(payload: dict[str, Any]) -> dict[str, Any]:
        # Registrar mesmo sem usar: activity não registrada = NotFoundError
        # retry storm no time-skipping (lição do harness do gate).
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
        return {"in_group": False, "holding": False, "abort": False, "reason": ""}

    return [
        activity.defn(name="check_group_plan_gate")(check_group_plan_gate),
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


@pytest.mark.asyncio
async def test_entering_validation_is_surfaced_on_the_thread(time_skipping_env):
    work_item_id = new_work_item_id("valsurf")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    posted: list[str] = []
    # CI nunca fica verde: o wait fecha no cap e (rc.130) PARA no parque de
    # review — o caminho até lá já atravessou coder -> L1, que é o que
    # interessa; o teste cancela de lá.
    state = FakeControlPlane(ci_sequence=["pending"] * (_POLL_CAP + 2))

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      workflow_runner=UnsandboxedWorkflowRunner(),
                      activities=_recording_activities(state, posted)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("cancel", "measured")
        await handle.result()

    assert "implementing" in posted, (
        f"pré-condição quebrada: nem o post de implementing saiu — {posted}"
    )
    idx_impl = posted.index("implementing")
    assert "validating" in posted[idx_impl:], (
        "a entrada no L1 não editou a superfície: a mensagem fica parada em "
        f"'implementing' durante toda a validação (statuses postados: {posted})"
    )
