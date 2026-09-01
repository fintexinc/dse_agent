"""Depois da PR, o DSE REPORTA — não luta.

Medido no banco de produção (2026-09-01, 185 itens): 58 abriram PR, 46
chegaram a `awaiting_human_review` — o sucesso segundo a CLAUDE.md — e 39
foram relabelados para vermelho DEPOIS. Os dois laços automáticos pós-PR têm
0% de sucesso na vida inteira: autofix de preview 0/8 despachos viraram
`created` (34/34 degradações são plataforma/receita, que um turno de Coder não
conserta); fix de CI vermelho 0/3 itens saíram verdes, e num único dia US$ 19
em 8 rodadas com `files_changed: []` porque a instrução era o literal
"ci red: fix the pipeline". O card de escalação dizia
`ci_red_after_retry_cap_exhausted` e nada mais.

A regra nova é uma frase: a PR é o entregável; o que acontece depois dela é
INFORMAÇÃO para o humano (preview, CI, com nomes e palavras), e um ciclo de
fix só roda por pedido humano — com a evidência dentro da instrução.

Harness DB-free de `test_plan_approval_timeout.py`: os relógios de 24 h/72 h
são observados em milissegundos pelo time-skipping.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from temporalio import activity
from temporalio.worker import Worker

from dse_contracts import ACTIVITY_TRIAGE_PREVIEW_FAILURE
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import new_work_item_id, wait_for_status
from fakes import FakeControlPlane
from test_plan_approval_timeout import (
    _Ledger,
    _gate_input,
    _wait_for_audit,
    _wait_for_comment,
    build_db_free_activities,
)

_CHECKS = [
    {"name": "unit (API)", "conclusion": "failure",
     "url": "https://github.com/acme/repo/actions/runs/1/job/2"},
    {"name": "openapi contract gates", "conclusion": "failure",
     "url": "https://github.com/acme/repo/actions/runs/1/job/3"},
]
_POD_WORDS = "the pod said: Error: Cannot find module '/srv/app/apps/api/dist/main.js'"
_LOG_TAIL = "FAIL src/contract/contract-drift.test.ts > regenerates byte-identically\nAssertionError: contract/openapi.json is stale"


def _post_pr_activities(ledger: _Ledger) -> list[Any]:
    """Os fakes que só o pós-PR alcança. `fetch_ci_failure_evidence` mora aqui,
    não em fakes.py: é a activity nova desta rc, e o que este arquivo mede é
    que a instrução do fix CARREGA o que ela devolve. `record_evidence_state`
    é a projeção do pipeline de evidência, que o harness do gate de plano
    nunca alcançava."""
    async def record_evidence_state(payload: dict[str, Any]) -> dict[str, Any]:
        ledger.audit.append(("fake_evidence_recorded", dict(payload)))
        return {"persisted": False}

    async def fetch_ci_failure_evidence(payload: dict[str, Any]) -> dict[str, Any]:
        ledger.audit.append(("fake_ci_evidence_fetched", dict(payload)))
        return {
            "work_item_id": payload["work_item_id"],
            "checks": [
                {"name": c["name"], "conclusion": c["conclusion"], "url": c["url"],
                 "log_tail": _LOG_TAIL, "source": "job_log"}
                for c in _CHECKS
            ],
        }
    return [
        activity.defn(name="record_evidence_state")(record_evidence_state),
        activity.defn(name="fetch_ci_failure_evidence")(fetch_ci_failure_evidence),
    ]


async def _run(env, ledger: _Ledger, state: FakeControlPlane, work_item_id: str, **input_kw):
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        env.client, task_queue=task_queue, workflows=[WorkItemLifecycleWorkflow],
        activities=build_db_free_activities(ledger, state) + _post_pr_activities(ledger),
    )
    handle = await env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, _gate_input(work_item_id, **input_kw),
        id=work_item_id, task_queue=task_queue,
    )
    return worker, handle


def _scheduled(history_events: list[dict[str, Any]]) -> set[str]:
    return {
        (e.get("activityTaskScheduledEventAttributes") or {}).get("activityType", {}).get("name")
        for e in history_events
        if "activityTaskScheduledEventAttributes" in e
    }


async def _history(client, work_item_id: str) -> list[dict[str, Any]]:
    from google.protobuf.json_format import MessageToDict
    history = await client.get_workflow_handle(work_item_id).fetch_history()
    return [MessageToDict(e) for e in history.events]


def _card(ledger: _Ledger, status: str) -> str:
    bodies = [b for s, b in ledger.comments if s == status]
    assert bodies, f"nenhum card {status!r}; vistos={ledger.comment_statuses}"
    return bodies[-1]


# ---------------------------------------------------------------------------
# 1-4: a PR nasce, o que houver DEPOIS dela vira informação — e o parque
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_red_ci_parks_for_review_with_the_failing_checks_and_no_coder_turn(time_skipping_env):
    wi = new_work_item_id("ci-red-reports")
    state = FakeControlPlane(ci_sequence=["red"])
    state.ci_failing_checks = list(_CHECKS)
    ledger = _Ledger()
    worker, handle = await _run(time_skipping_env, ledger, state, wi)
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        card = _card(ledger, "review_ready")
        await handle.signal("cancel", "test over")
        await handle.result()

    assert state.coder_turn_calls == 1, "CI vermelho não compra turno de Coder sozinho"
    assert "ci_red_retrying" not in ledger.audit_actions
    assert "unit (API)" in card and "openapi contract gates" in card
    assert "https://github.com/acme/repo/actions/runs/1/job/2" in card


@pytest.mark.asyncio
async def test_a_degraded_preview_parks_for_review_with_the_containers_words_and_no_triage(time_skipping_env):
    wi = new_work_item_id("preview-degraded-reports")
    state = FakeControlPlane(preview_mode="degraded", ci_sequence=["green"])
    state.preview_degraded_detail = _POD_WORDS
    ledger = _Ledger()
    worker, handle = await _run(time_skipping_env, ledger, state, wi)
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        card = _card(ledger, "review_ready")
        events = await _history(time_skipping_env.client, wi)
        await handle.signal("cancel", "test over")
        await handle.result()

    assert ACTIVITY_TRIAGE_PREVIEW_FAILURE not in _scheduled(events), (
        "34/34 degradações são plataforma: a triage nunca teve alvo"
    )
    assert "preview_autofix_dispatched" not in ledger.audit_actions
    assert "Cannot find module" in card, "as palavras do container, no card"
    assert state.coder_turn_calls == 1


@pytest.mark.asyncio
async def test_ci_pending_exhaustion_parks_for_review_instead_of_escalating(time_skipping_env):
    wi = new_work_item_id("ci-pending-exhausted")
    state = FakeControlPlane(ci_sequence=["pending"] * 6)
    ledger = _Ledger()
    worker, handle = await _run(
        time_skipping_env, ledger, state, wi,
        ci_pending_poll_cap=3, ci_wait_deadline_hours=0.0, ci_poll_interval_seconds=0.01,
    )
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        card = _card(ledger, "review_ready")
        await handle.signal("cancel", "test over")
        await handle.result()

    assert "escalated" not in ledger.audit_actions
    assert "ci_wait_exhausted" in ledger.audit_actions, "a linha greppável continua"
    assert "pending" in card.lower()


@pytest.mark.asyncio
async def test_the_no_ci_case_is_one_card_not_two(time_skipping_env):
    wi = new_work_item_id("no-ci-one-card")
    state = FakeControlPlane(ci_sequence=["no_ci"] * 50)
    ledger = _Ledger()
    worker, handle = await _run(
        time_skipping_env, ledger, state, wi, ci_poll_interval_seconds=0.01,
    )
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        await _wait_for_comment(ledger, "review_ready")
        await handle.signal("cancel", "test over")
        await handle.result()

    assert "pr_ready" not in ledger.comment_statuses, "o aviso separado de no-CI morreu"
    assert "no ci" in _card(ledger, "review_ready").lower()


# ---------------------------------------------------------------------------
# 5-6: o parque tem relógio, e o relógio nunca pinta de vermelho
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_reminder_keeps_the_status_and_reposts_the_card(time_skipping_env):
    wi = new_work_item_id("review-reminder")
    state = FakeControlPlane(ci_sequence=["green"])
    ledger = _Ledger()
    worker, handle = await _run(
        time_skipping_env, ledger, state, wi,
        review_reminder_hours=0.01, review_timeout_hours=100.0,
    )
    async with worker:
        await _wait_for_audit(ledger, "review_reminder_sent")
        await handle.signal("cancel", "test over")
        await handle.result()

    cards = [b for s, b in ledger.comments if s == "review_ready"]
    assert len(cards) >= 2 and "Reminder" in cards[-1]
    assert not [s for s in ledger.comment_statuses if "reminder" in s], (
        "pseudo-status mataria os botões — a lição do gate de plano"
    )


@pytest.mark.asyncio
async def test_review_deadline_never_escalates_and_the_merge_still_completes_the_item(time_skipping_env):
    wi = new_work_item_id("review-deadline")
    state = FakeControlPlane(ci_sequence=["green"])
    ledger = _Ledger()
    worker, handle = await _run(
        time_skipping_env, ledger, state, wi,
        review_reminder_hours=0.01, review_timeout_hours=0.02,
    )
    async with worker:
        await _wait_for_audit(ledger, "review_deadline_elapsed")
        assert "escalated" not in ledger.audit_actions
        assert state.teardown_calls == 1, "o prazo libera o único recurso que custa"
        await handle.signal("review_comment", {"verdict": "approved"})
        await wait_for_status(handle, {"merge_pending"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert "Still awaiting your review" in _card(ledger, "review_ready")


# ---------------------------------------------------------------------------
# 7-9: o ciclo de fix é humano, único por pedido, e carrega evidência
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_item_that_spent_its_coder_retries_before_the_pr_still_gets_one_human_fix_cycle(time_skipping_env):
    wi = new_work_item_id("retries-spent")
    state = FakeControlPlane(ci_sequence=["green"] * 4, l1_fail_times=0)
    ledger = _Ledger()
    worker, handle = await _run(
        time_skipping_env, ledger, state, wi, coder_retry_count=3, coder_retry_cap=3,
    )
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        state.l1_fail_times = 1  # a revalidação do fix reprova UMA vez
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "rename it"})
        await _wait_for_audit(ledger, "review_fix_l1_failed")
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("cancel", "test over")
        await handle.result()

    assert "l1_revalidation_failed_after_retry_cap" not in ledger.audit_actions
    assert state.coder_turn_calls == 2, "um ciclo por pedido, nunca recursão até o teto"
    assert "did not pass L1" in _card(ledger, "review_ready")


@pytest.mark.asyncio
async def test_a_fix_ci_request_runs_one_cycle_whose_instruction_carries_the_evidence(time_skipping_env):
    wi = new_work_item_id("fix-ci")
    state = FakeControlPlane(ci_sequence=["red", "green"])
    state.ci_failing_checks = list(_CHECKS)
    ledger = _Ledger()
    worker, handle = await _run(time_skipping_env, ledger, state, wi)
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {
            "verdict": "changes_requested", "comment": "@dse fix ci", "fix_target": "ci",
        })
        await _wait_for_audit(ledger, "pr_refinalized")
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("cancel", "test over")
        await handle.result()

    assert state.coder_turn_calls == 2
    instrucao = state.coder_instructions[-1]
    assert "unit (API)" in instrucao and "contract/openapi.json is stale" in instrucao, (
        "é o que faltou nas 8 rodadas de US$ 19: nome do check e o tail do log"
    )


@pytest.mark.asyncio
async def test_a_fix_preview_request_carries_the_containers_words(time_skipping_env):
    wi = new_work_item_id("fix-preview")
    state = FakeControlPlane(preview_mode="degraded", ci_sequence=["green"] * 3)
    state.preview_degraded_detail = _POD_WORDS
    ledger = _Ledger()
    worker, handle = await _run(time_skipping_env, ledger, state, wi)
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {
            "verdict": "changes_requested", "comment": "@dse fix preview", "fix_target": "preview",
        })
        await _wait_for_audit(ledger, "pr_refinalized")
        await handle.signal("cancel", "test over")
        await handle.result()

    assert "Cannot find module" in state.coder_instructions[-1]


# ---------------------------------------------------------------------------
# 10: quando escala DEPOIS da PR, o card nomeia PR, preview e CI
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_escalation_after_the_pr_names_the_pr_the_preview_and_the_ci(time_skipping_env):
    wi = new_work_item_id("escalate-after-pr")
    state = FakeControlPlane(ci_sequence=["red"])
    state.ci_failing_checks = list(_CHECKS)
    ledger = _Ledger()
    worker, handle = await _run(time_skipping_env, ledger, state, wi)
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("escalate", "operator wants a human look")
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    card = _card(ledger, "escalated")
    assert "PR #" in card
    assert "unit (API)" in card
    assert "preview" in card.lower()
