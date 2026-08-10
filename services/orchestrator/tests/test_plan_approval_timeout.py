"""Deadline of the `awaiting_plan_approval` gate.

Proves the behaviours the gate has to have once it stops being an unbounded
park:
  - nobody answers -> a reminder goes out FIRST, then the item lands on the
    `escalated` terminal state with an audit row naming the timeout, and it
    NEVER self-approves;
  - the reminder keeps the REAL `awaiting_plan_approval` status, because that is
    the only string for which adapter-slack attaches the Approve/Reject buttons;
  - the verdict arrives after the reminder but BEFORE expiry -> the deadline is
    abandoned and implementation proceeds normally;
  - a verdict that lands in the same instant the deadline fires is HONOURED, not
    discarded;
  - `plan_approval_timeout_hours <= 0` -> the deadline is disabled and the gate
    stays parked (escape hatch for a tenant on a slower approval cycle);
  - a pre-patch history replays ONLY because of the `plan-approval-timeout-v1`
    guard;
  - the window arithmetic, including the reminder/timeout misconfiguration.

Runs WITHOUT Postgres ON PURPOSE. The DB-backed local Activities are WS-B's own
write path, not a boundary this test is proving, so they are faked here and the
timeout path is exercisable on a laptop (and in a CI job) with no foundation
infra. `test_plan_approval_gate.py` already covers the same gate against the
real Postgres projection. Temporal itself is NEVER mocked: it is the real
time-skipping test server, which is exactly what makes a 72h business timer
observable in milliseconds.
"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import temporalio.workflow
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from dse_contracts.activities import ACTIVITY_EMIT_AUDIT, ACTIVITY_POST_TRACKING_COMMENT
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator import policy
from dse_orchestrator.config import OrchestratorConfig, apply_to_input
from dse_orchestrator.local_activities import (
    LOCAL_ACTIVITY_POST_STATUS_TRANSITION,
    LOCAL_ACTIVITY_RECORD_GATE,
    LOCAL_ACTIVITY_UPDATE_STATUS,
    _STATUS_BODIES,
    check_clarification_completeness,
    emit_history_metric,
    resolve_budget_cap,
    resolve_plan_approver,
    resolve_retry_caps,
)
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import (
    STATUS_AWAITING_PLAN_APPROVAL,
    WorkItemLifecycleWorkflow,
    _approval_windows,
)

from conftest import new_work_item_id, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

#: The marker that keeps runs already parked at the gate replayable.
_GATE_PATCH_ID = "plan-approval-timeout-v1"


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides conftest's autouse skip. Every Activity in this module that
    would touch Postgres is faked below, so the suite must RUN where the
    foundation infra is absent instead of being silently skipped — the timeout
    logic is the kind of thing that only ever breaks when nobody can run its
    test."""
    yield


@pytest.fixture(autouse=True)
def _reset_codeowners():
    policy.set_codeowners_reader(None)
    yield
    policy.set_codeowners_reader(None)


# ---------------------------------------------------------------------------
# DB-free stand-ins for the WS-B local Activities that write to Postgres.
# ---------------------------------------------------------------------------
@dataclass
class _Ledger:
    """Captures what the workflow WOULD have persisted, so assertions can read
    it the same way `conftest.read_audit_actions`/`read_gate_row` read Postgres."""

    audit: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    gate_writes: list[dict[str, Any]] = field(default_factory=list)
    # (status, rendered text) per outbound call. `status` is what adapter-slack
    # keys the Approve/Reject Block Kit off, so the tests below assert on it.
    comments: list[tuple[str, str]] = field(default_factory=list)

    @property
    def audit_actions(self) -> list[str]:
        return [action for action, _ in self.audit]

    def audit_details(self, action: str) -> dict[str, Any]:
        for recorded, details in self.audit:
            if recorded == action:
                return details
        raise AssertionError(f"audit action {action!r} never emitted; seen={self.audit_actions}")

    @property
    def comment_statuses(self) -> list[str]:
        return [status for status, _ in self.comments]


def build_db_free_activities(ledger: _Ledger, state: FakeControlPlane) -> list[Any]:
    async def emit_audit_event(payload: dict[str, Any]) -> None:
        ledger.audit.append((payload["action"], payload.get("details") or {}))

    async def update_work_item_status(payload: dict[str, Any]) -> dict[str, Any]:
        # Mirrors the real Activity's no-Postgres return shape: the workflow
        # only reads state_version/plan_hash out of it.
        return {"persisted": False}

    async def record_plan_approval(payload: dict[str, Any]) -> dict[str, Any]:
        ledger.gate_writes.append(dict(payload))
        return {"persisted": False}

    async def post_tracking_comment(payload: dict[str, Any]) -> dict[str, Any]:
        # Mirrors the real Activity's precedence: an explicit `body` wins over the
        # `_STATUS_BODIES` template rendered from `detail`.
        ledger.comments.append((
            payload.get("status", ""),
            str(payload.get("body") or payload.get("detail") or ""),
        ))
        return {"ok": True}

    async def post_status_transition(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "target_status": None}

    return [
        activity.defn(name=ACTIVITY_EMIT_AUDIT)(emit_audit_event),
        activity.defn(name=ACTIVITY_POST_TRACKING_COMMENT)(post_tracking_comment),
        activity.defn(name=LOCAL_ACTIVITY_UPDATE_STATUS)(update_work_item_status),
        activity.defn(name=LOCAL_ACTIVITY_RECORD_GATE)(record_plan_approval),
        activity.defn(name=LOCAL_ACTIVITY_POST_STATUS_TRANSITION)(post_status_transition),
        # Real ones: the checklist is pure, resolve_plan_approver short-circuits
        # on the CODEOWNERS reader before touching the DB, and the history
        # metric is OTel-only.
        check_clarification_completeness,
        resolve_plan_approver,
        emit_history_metric,
        # As duas pontes de env → workflow, também puras (só leem os. environ).
        # Faltavam aqui, e o custo NÃO era zero: o workflow chama as duas, elas
        # morriam em NotFoundError, e cada morte é uma tarefa de activity
        # entregue, falhada e reagendada dentro de um ambiente time-skipping
        # cujo relógio só anda em janela ociosa. Foi assim que
        # test_verdict_landing_* caiu no CI de afb9616 sem reproduzir local.
        # `resolve_budget_cap` já faltava antes de `resolve_retry_caps` existir.
        resolve_budget_cap,
        resolve_retry_caps,
    ] + build_fake_activities(state)


def _gate_input(work_item_id: str, **overrides: Any) -> WorkItemLifecycleInput:
    base = dict(
        work_item_id=work_item_id,
        tenant_id="test-tenant",
        requester="usr_test",
        repo="acme/repo",
        base_branch="main",
        acceptance_criteria="crit",
        # keeps the clarification gate's own timers far away from the assertions
        clarification_reminder_hours=1000.0,
        clarification_escalation_days=1000.0,
    )
    base.update(overrides)
    return WorkItemLifecycleInput(**base)


async def _wait_for_audit(ledger: _Ledger, action: str, attempts: int = 400) -> None:
    for _ in range(attempts):
        if action in ledger.audit_actions:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"audit action {action!r} never appeared; seen={ledger.audit_actions}")


# ---------------------------------------------------------------------------
# History readers — the replay-safety assertions below inspect the command
# sequence the workflow actually issued, not just its outcome.
# ---------------------------------------------------------------------------
async def _history_events(client, work_item_id: str) -> list[dict[str, Any]]:
    from google.protobuf.json_format import MessageToDict

    history = await client.get_workflow_handle(work_item_id).fetch_history()
    return [MessageToDict(event) for event in history.events]


def _patch_marker(events: list[dict[str, Any]], patch_id: str) -> dict[str, Any] | None:
    """The `workflow.patched()` marker as recorded in history: markerName
    `core_patch`, with the patch id inside a base64 JSON payload."""
    for event in events:
        attrs = event.get("markerRecordedEventAttributes") or {}
        if attrs.get("markerName") != "core_patch":
            continue
        payloads = (attrs.get("details") or {}).get("patch-data", {}).get("payloads") or []
        for payload in payloads:
            decoded = json.loads(base64.b64decode(payload["data"]))
            if decoded.get("id") == patch_id:
                return event
    return None


def _timers_started(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("eventType") == "EVENT_TYPE_TIMER_STARTED"]


# ---------------------------------------------------------------------------
# Workflow behaviour (real Temporal, time-skipping)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_timeout_reminds_then_escalates_without_self_approving(time_skipping_env):
    work_item_id = new_work_item_id("gate-timeout")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice @bob")
    state = FakeControlPlane(plan_risk_class="high")
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
        # Nobody ever signals. Awaiting the result is what lets the test server
        # skip to the next timer, so the 1h reminder and the 2h deadline both
        # fire in milliseconds of real time.
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    assert "plan_approval_timeout_after_2h" in (result.detail or "")

    actions = ledger.audit_actions
    # order matters: the human is pinged BEFORE the item is taken away from them
    assert actions.index("plan_approval_reminder_sent") < actions.index("plan_approval_timed_out")
    assert actions.index("plan_approval_timed_out") < actions.index("escalated")
    # never guesses consent out of silence
    assert "plan_approved" not in actions
    assert "plan_auto_approved" not in actions
    assert state.provision_calls == 0 and state.coder_turn_calls == 0

    details = ledger.audit_details("plan_approval_timed_out")
    assert details["timeout_hours"] == 2.0
    assert details["reminder_hours"] == 1.0
    assert details["reminder_hours_effective"] == 1.0
    assert details["reminder_hours_overridden"] is False
    assert details["approvers"] == ["@alice", "@bob"]
    assert details["risk_class"] == "high"

    # ONE row per real transition — a stuck gate must not tick out audit rows.
    assert actions.count("plan_approval_reminder_sent") == 1
    assert actions.count("plan_approval_timed_out") == 1

    # the gate projection stops claiming to be pending, and names the timeout
    last_gate = ledger.gate_writes[-1]
    assert last_gate["status"] == "blocked"
    assert last_gate["decided_by"] is None
    assert "plan_approval_timeout_after_2h" in last_gate["justification"]

    # The reminder went out on the originating surface as a NEW BODY under the
    # UNCHANGED status. Both halves matter: a different body is what a human
    # notices, and `awaiting_plan_approval` is the only status for which
    # adapter-slack re-attaches approval_blocks() — under anything else
    # chat_update rewrites the message with no Block Kit and the Approve/Reject
    # buttons, the sole way a Slack approver can answer, disappear.
    gate_comments = [b for s, b in ledger.comments if s == STATUS_AWAITING_PLAN_APPROVAL]
    assert len(gate_comments) == 2, ledger.comments
    assert gate_comments[0] != gate_comments[1]
    assert "Reminder" in gate_comments[1]
    assert "@alice" in gate_comments[1] and "escalated" in gate_comments[1]
    # and NOTHING was ever posted under a reminder pseudo-status
    assert not [s for s in ledger.comment_statuses if "reminder" in s]


@pytest.mark.asyncio
async def test_approval_just_before_expiry_proceeds_normally(time_skipping_env):
    """The reminder has already fired and the deadline is seconds away when the
    verdict lands: the timer must be abandoned, not raced."""
    work_item_id = new_work_item_id("gate-justintime")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    state = FakeControlPlane(plan_risk_class="high")
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id,
                        plan_approval_reminder_hours=1.0,
                        plan_approval_timeout_hours=3.0),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"awaiting_plan_approval"})

        # Explicit clock control (queries do not auto-skip): advance past the
        # reminder only, leaving ~1h55m of the deadline unspent.
        await time_skipping_env.sleep(timedelta(hours=1, minutes=5))
        await _wait_for_audit(ledger, "plan_approval_reminder_sent")

        await handle.signal("plan_approval", {"verdict": "approved", "actor": "usr_alice"})
        await wait_for_status(handle, {"review_ready"})
        assert state.coder_turn_calls == 1
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = ledger.audit_actions
    assert "plan_approval_reminder_sent" in actions  # the reminder is not terminal
    assert "plan_approved" in actions
    assert "plan_approval_timed_out" not in actions
    assert "escalated" not in actions
    approved_gate = [g for g in ledger.gate_writes if g["status"] == "approved"]
    assert approved_gate and approved_gate[-1]["decided_by"] == "usr_alice"


@pytest.mark.asyncio
async def test_verdict_landing_with_the_deadline_is_not_discarded(
    time_skipping_env, monkeypatch
):
    """The race the escalation path used to lose.

    `wait_condition(timeout=...)` reports the timeout even when the signal that
    answers the gate is delivered in the very workflow task that carries the fired
    timer, and more signals can arrive while the coroutine is scheduled. The first
    version escalated on `decided is False` without looking again, so a verdict
    that beat the deadline by milliseconds was DISCARDED and the append-only
    ledger held both "approval delivered" and "timed out, no verdict" for one
    item.

    The seam is SIMULATED rather than raced: a real millisecond overlap cannot be
    scheduled against a time-skipping clock, so `_wait_with_reminder` is replaced
    by exactly the state that overlap produces — the verdict flag set, and False
    returned. Everything after the seam (the re-check, the audit row, the verdict
    handling) is the real code. Deleting the re-check in
    `_expire_plan_approval` turns this test red: the run escalates instead of
    implementing.
    """
    work_item_id = new_work_item_id("gate-photofinish")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    state = FakeControlPlane(plan_risk_class="high")
    ledger = _Ledger()

    async def timer_fires_together_with_the_verdict(self, **kwargs):
        self._plan_approval_received = True
        self._plan_approval_payload = {"verdict": "approved", "actor": "usr_alice"}
        return False  # "the whole deadline elapsed" — as the SDK reports it

    monkeypatch.setattr(
        WorkItemLifecycleWorkflow, "_wait_with_reminder",
        timer_fires_together_with_the_verdict,
    )

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      # the monkeypatch above only reaches the workflow class when
                      # the module is not re-imported inside a sandbox
                      workflow_runner=UnsandboxedWorkflowRunner(),
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id,
                        plan_approval_reminder_hours=1.0,
                        plan_approval_timeout_hours=2.0),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = ledger.audit_actions
    assert "plan_approved" in actions
    # the ledger must NOT claim both things about the same gate
    assert "plan_approval_timed_out" not in actions
    assert "escalated" not in actions
    # and the near-miss is recorded once, so an operator can see it happened
    assert actions.count("plan_approval_verdict_raced_deadline") == 1
    approved_gate = [g for g in ledger.gate_writes if g["status"] == "approved"]
    assert approved_gate and approved_gate[-1]["decided_by"] == "usr_alice"
    assert not [g for g in ledger.gate_writes if g["status"] == "blocked"]


@pytest.mark.asyncio
async def test_verdict_landing_with_the_reminder_is_not_reminded_at(
    time_skipping_env, monkeypatch
):
    """The same race, one timer earlier — at the REMINDER instead of the deadline.

    The re-check was added to `_expire_plan_approval` and not to the reminder, so
    a verdict delivered in the workflow task that carried the fired reminder timer
    still got "this plan is still waiting for a decision" written over it. On
    Slack that body replaces the plan text INSIDE the Block Kit whose
    Approve/Reject buttons are still attached (the reminder keeps
    `awaiting_plan_approval` on purpose), so the approver is invited to decide
    again an item that is already being implemented — and the append-only ledger
    gets a `plan_approval_reminder_sent` row for a reminder nothing needed.

    Same simulated seam as the deadline test above, for the same reason (a real
    millisecond overlap cannot be scheduled against a time-skipping clock):
    `workflow.wait_condition` is replaced ONLY for the call carrying the reminder
    window, and it reproduces exactly what the SDK produces in that overlap — the
    verdict already applied, and TimeoutError raised anyway. Everything else,
    including the reminder path itself, is the real code.
    """
    work_item_id = new_work_item_id("gate-remindrace")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    state = FakeControlPlane(plan_risk_class="high")
    ledger = _Ledger()

    real_wait_condition = temporalio.workflow.wait_condition
    # 1h reminder inside a 3h deadline: the remaining window is 2h, so this
    # interception can only be the reminder wait and never the one after it.
    reminder_window = timedelta(hours=1)

    async def verdict_arrives_with_the_reminder_timer(fn, *, timeout=None, **kwargs):
        if timeout == reminder_window:
            gate = temporalio.workflow.instance()
            gate._plan_approval_received = True
            gate._plan_approval_payload = {"verdict": "approved", "actor": "usr_alice"}
            raise asyncio.TimeoutError  # "nobody answered" — as the SDK reports it
        return await real_wait_condition(fn, timeout=timeout, **kwargs)

    monkeypatch.setattr(
        temporalio.workflow, "wait_condition", verdict_arrives_with_the_reminder_timer
    )

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      # as above: the patch only reaches the workflow when the
                      # module is not re-imported inside a sandbox
                      workflow_runner=UnsandboxedWorkflowRunner(),
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id,
                        plan_approval_reminder_hours=1.0,
                        plan_approval_timeout_hours=3.0),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = ledger.audit_actions
    assert "plan_approved" in actions
    # No reminder happened, so the ledger must not say one did.
    assert "plan_approval_reminder_sent" not in actions
    assert "plan_approval_timed_out" not in actions
    # And nothing was written over the gate message: the ONLY body posted under
    # `awaiting_plan_approval` is the original approval request, so the buttons a
    # Slack approver already used are not re-offered under reminder text.
    gate_comments = [b for s, b in ledger.comments if s == STATUS_AWAITING_PLAN_APPROVAL]
    assert len(gate_comments) == 1, ledger.comments
    assert "Reminder" not in gate_comments[0]
    approved_gate = [g for g in ledger.gate_writes if g["status"] == "approved"]
    assert approved_gate and approved_gate[-1]["decided_by"] == "usr_alice"


@pytest.mark.asyncio
async def test_timeout_zero_disables_the_deadline(time_skipping_env):
    """Escape hatch: an operator who sets the timeout to 0 gets the old
    unbounded park back, not an instant escalation."""
    work_item_id = new_work_item_id("gate-nodeadline")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    state = FakeControlPlane(plan_risk_class="high")
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id,
                        plan_approval_reminder_hours=1.0,
                        plan_approval_timeout_hours=0.0),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"awaiting_plan_approval"})

        # Two weeks of workflow time later it is still parked.
        await time_skipping_env.sleep(timedelta(days=14))
        assert await handle.query(WorkItemLifecycleWorkflow.get_status) == "awaiting_plan_approval"
        assert "plan_approval_timed_out" not in ledger.audit_actions
        assert "plan_approval_reminder_sent" not in ledger.audit_actions

        await handle.signal("cancel", "end of test")
        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value  # cancelled, not escalated
    assert state.coder_turn_calls == 0
    # The disabled branch must issue the SAME commands the pre-timeout code did:
    # no timer at all. This is the shape an in-flight history has.
    events = await _history_events(time_skipping_env.client, work_item_id)
    assert _timers_started(events) == []


# ---------------------------------------------------------------------------
# Replay safety
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate_timer_is_recorded_behind_its_patch_marker(time_skipping_env):
    """The reason a live execution parked at this gate survives the deploy.

    Asserts on the emitted COMMAND SEQUENCE, which is what replay compares:
      - the `plan-approval-timeout-v1` marker exists;
      - it precedes the first timer AND belongs to the same workflow task, so
        marker and timer are one atomic fork. A history that lacks the marker
        (every run parked here before the deploy) therefore also lacks the
        timer, `patched()` returns False on replay and the untimed branch
        reproduces the original commands exactly.

    On its own this only verifies the guard's PLACEMENT plus determinism of the
    new path — a new run always records the marker, so it cannot show what happens
    to a history that lacks one.
    `test_pre_patch_history_replays_only_because_of_the_guard` does that, and is
    the test that fails if the guard is deleted.
    """
    work_item_id = new_work_item_id("gate-patchguard")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(
                          ledger, FakeControlPlane(plan_risk_class="high"))):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id,
                        plan_approval_reminder_hours=1.0,
                        plan_approval_timeout_hours=2.0),
            id=work_item_id, task_queue=task_queue)
        assert result.status == WorkItemStatus.escalated.value

    events = await _history_events(time_skipping_env.client, work_item_id)
    marker = _patch_marker(events, _GATE_PATCH_ID)
    assert marker is not None, "the gate timer is NOT behind a patch marker"

    timers = _timers_started(events)
    assert timers, "the deadline did not start a timer"
    assert int(marker["eventId"]) < int(timers[0]["eventId"])
    assert (marker["markerRecordedEventAttributes"]["workflowTaskCompletedEventId"]
            == timers[0]["timerStartedEventAttributes"]["workflowTaskCompletedEventId"])

    # And the new path itself replays (it would not if the deadline were computed
    # from a wall clock instead of Temporal's timers).
    await Replayer(
        workflows=[WorkItemLifecycleWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(WorkflowHistory.from_json(work_item_id, {"events": events}))


@pytest.mark.asyncio
async def test_pre_patch_history_replays_only_because_of_the_guard(
    time_skipping_env, monkeypatch
):
    """THE replay-safety test: it fails if `workflow.patched()` is removed.

    The problem with asserting on marker placement (the test above) or on a
    committed fixture (the one below) is that neither exercises the fork. A
    pre-deploy history is the only real evidence, and a new run always records the
    marker, so one has to be MANUFACTURED — not faked: the history recorded here
    is produced by the real workflow issuing the real commands, with
    `workflow.patched("plan-approval-timeout-v1")` returning False exactly as it
    does for a run that started before the deploy. Every OTHER patch id keeps its
    genuine behaviour (markers included), so the command sequence is the one the
    pre-timeout code produced: no timer, no marker, an unbounded park at the gate,
    then a normal approval.

    Then the same history is replayed twice against the CURRENT definition:
      - as shipped -> `patched()` finds no marker, takes the untimed branch,
        reproduces the commands, replay PASSES;
      - with the guard bypassed (patched forced True, i.e. the code you get by
        deleting the `if`) -> the deadline issues a StartTimer the history does
        not contain and replay FAILS with a nondeterminism error.

    Both workers run unsandboxed because that is what lets the monkeypatch of
    `temporalio.workflow.patched` reach the workflow code; the sandbox would
    re-import the module and undo it.
    """
    work_item_id = new_work_item_id("gate-prepatch")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    state = FakeControlPlane(plan_risk_class="high")
    ledger = _Ledger()

    real_patched = temporalio.workflow.patched

    def patched_before_the_deploy(patch_id: str) -> bool:
        if patch_id == _GATE_PATCH_ID:
            return False  # and, crucially, records no marker
        return real_patched(patch_id)

    monkeypatch.setattr(temporalio.workflow, "patched", patched_before_the_deploy)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      workflow_runner=UnsandboxedWorkflowRunner(),
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            # A POSITIVE deadline: the whole point is that the current code would
            # start a timer here, and the recorded history has none.
            _gate_input(work_item_id,
                        plan_approval_reminder_hours=1.0,
                        plan_approval_timeout_hours=2.0),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"awaiting_plan_approval"})
        # Weeks of workflow time with no timer anywhere: the pre-patch park.
        await time_skipping_env.sleep(timedelta(days=14))
        assert "plan_approval_reminder_sent" not in ledger.audit_actions, (
            "the gate fired its deadline with the patch guard suppressed — the "
            "guard is gone, so a run parked at this gate before the deploy can no "
            "longer replay"
        )
        await handle.signal("plan_approval", {"verdict": "approved", "actor": "usr_alice"})
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value

    events = await _history_events(time_skipping_env.client, work_item_id)
    # The two properties that make this a genuine pre-patch history.
    assert _patch_marker(events, _GATE_PATCH_ID) is None
    assert _timers_started(events) == []
    history = WorkflowHistory.from_json(work_item_id, {"events": events})

    monkeypatch.setattr(temporalio.workflow, "patched", real_patched)
    await Replayer(
        workflows=[WorkItemLifecycleWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
        data_converter=pydantic_data_converter,
    ).replay_workflow(history)

    # Now the negative half — without it this file would pass with the guard
    # deleted, which is exactly the criticism that produced this test.
    monkeypatch.setattr(temporalio.workflow, "patched", lambda patch_id: True)
    with pytest.raises(Exception) as excinfo:
        await Replayer(
            workflows=[WorkItemLifecycleWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            data_converter=pydantic_data_converter,
        ).replay_workflow(
            WorkflowHistory.from_json(work_item_id, {"events": events})
        )
    assert "nondeterminism" in str(excinfo.value).lower(), (
        "replaying a pre-patch history with the guard bypassed must be rejected "
        f"as nondeterministic, got: {excinfo.value!r}"
    )


@pytest.mark.asyncio
async def test_committed_history_fixture_still_replays():
    """General regression lock for spec §5 (see test_remediation_replay.py):
    a history captured before this change must keep replaying. Replay executes no
    Activities, so it needs no Postgres and belongs here, where it actually runs.

    NOT evidence for the patch guard, and it was wrongly cited as such: this
    fixture closes BEFORE reaching the approval gate, so it would pass with
    `workflow.patched("plan-approval-timeout-v1")` deleted.
    `test_pre_patch_history_replays_only_because_of_the_guard` is the test that
    actually fails in that case."""
    fixture = Path(__file__).parent / "histories" / "escalated_empty_plan.json"
    if not fixture.exists():
        pytest.skip("history fixture not captured yet")
    await Replayer(
        workflows=[WorkItemLifecycleWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(
        WorkflowHistory.from_json("replay-fixture", json.loads(fixture.read_text()))
    )


# ---------------------------------------------------------------------------
# Window arithmetic + config plumbing (pure, no Temporal, no Postgres)
# ---------------------------------------------------------------------------
def test_approval_windows_splits_the_deadline_around_the_reminder():
    reminder, remaining, overridden = _approval_windows(24.0, 72.0)
    assert reminder == timedelta(hours=24)
    assert remaining == timedelta(hours=48)
    assert overridden is False


@pytest.mark.parametrize(
    "reminder_hours",
    [
        96.0,   # reminder after the deadline
        12.0,   # reminder exactly ON the deadline
        0.0,    # "remind immediately", i.e. duplicate the message just posted
        -5.0,   # nonsense
    ],
)
def test_a_reminder_that_does_not_fit_moves_to_half_the_deadline(reminder_hours):
    """The misconfiguration the docstring used to lie about.

    Clamping to the deadline (reminder := timeout) fired the ping and the
    escalation in the same instant: formally pinged, no lead time, no chance to
    answer. Half the deadline keeps the promise the gate makes, never moves the
    deadline itself, and the boolean lets the caller log it and record the
    EFFECTIVE value in the timeout audit row.
    """
    reminder, remaining, overridden = _approval_windows(reminder_hours, 12.0)
    assert overridden is True
    assert reminder == timedelta(hours=6)
    assert remaining == timedelta(hours=6)
    assert reminder + remaining == timedelta(hours=12)  # the deadline never moves


def test_defaults_are_the_documented_business_windows():
    wf_input = WorkItemLifecycleInput(work_item_id="wi", tenant_id="t", requester="u")
    assert wf_input.plan_approval_reminder_hours == 24.0
    # 72h is the smallest deadline that survives a weekend (see models.py)
    assert wf_input.plan_approval_timeout_hours == 72.0


def test_the_window_has_no_env_knob_because_one_could_not_take_effect(monkeypatch):
    """A `DSE_PLAN_APPROVAL_*` pair was added and removed. It must stay removed.

    `apply_to_input` is the only bridge from `OrchestratorConfig` into a workflow
    and it has no production caller: the deployed starter
    (`ingest_gateway.dispatcher._dispatch_row`) calls
    `start_workflow(WORKFLOW_TYPE, work_item_id)` with a bare string, so
    `_coerce_input` builds the input from the dataclass defaults. An env var that
    appears to widen a deadline which ESCALATES a work item, and in fact does
    nothing, is worse than no knob — so the config carries neither the fields nor
    the vars, and the literal in `models.py` is the single place the window is
    set. See the README for what wiring env config would take (one change in that
    dispatcher call, which turns every knob live at once).
    """
    monkeypatch.setenv("DSE_PLAN_APPROVAL_REMINDER_HOURS", "4")
    monkeypatch.setenv("DSE_PLAN_APPROVAL_TIMEOUT_HOURS", "12.5")
    cfg = OrchestratorConfig.from_env()
    assert not hasattr(cfg, "plan_approval_reminder_hours")
    assert not hasattr(cfg, "plan_approval_timeout_hours")
    # apply_to_input carries every field the config DOES have; the window is not
    # one of them, so the deployed literals survive the env vars untouched.
    wf_input = apply_to_input(
        WorkItemLifecycleInput(work_item_id="wi", tenant_id="t", requester="u"), cfg
    )
    assert wf_input.plan_approval_reminder_hours == 24.0
    assert wf_input.plan_approval_timeout_hours == 72.0


def test_no_reminder_pseudo_status_exists():
    """The landmine that must stay defused.

    A `_STATUS_BODIES` entry for a reminder status is an invitation to post the
    reminder under it, and adapter-slack attaches approval_blocks() ONLY for the
    literal `awaiting_plan_approval` — any other status makes chat_update rewrite
    the single mutable message with no Block Kit, destroying the Approve/Reject
    buttons that are the only way a Slack approver can answer. The reminder
    overrides `body` under the real status instead.
    """
    assert "awaiting_plan_approval_reminder" not in _STATUS_BODIES
    assert not [status for status in _STATUS_BODIES if "reminder" in status]
