"""A Tester turn that infrastructure ended must not be paid for as a code fix.

Real incident (audit_log 23474, 2026-07-29 13:37:04 UTC): the test Pod was
killed by the cgroup OOM killer, the suite came back with rc=137, and the fix
loop read it as a failing assertion. The Coder was then asked three more times —
at a measured US$1.03 a turn — to fix a memory limit it cannot reach, and the
item finally died as `tester_failed_after_retry_cap`, a sentence that names
neither the OOM nor the limit.

What is proven here:
  - an OOM kill escalates on the spot and buys ZERO Coder turns;
  - an ordinary failing suite still walks the retry loop to the cap (the branch
    must not swallow the case it was not written for);
  - a suite that never terminates is the one ambiguous ending, so it buys
    exactly ONE Coder turn and then escalates;
  - the escalation reason is queryable (`tester_infra_<outcome>`) and carries
    the runtime's own words, including the deployed memory limit;
  - the whole decision sits behind `tester-infra-outcome-escalates-v1`, late in
    the Coder loop, and a history recorded before it replays ONLY because the
    guard is there.

Runs WITHOUT Postgres ON PURPOSE, like `test_plan_approval_timeout.py`: WS-B's
own DB write path is not the boundary under test, and a cost guard whose test
nobody can run on a laptop is a cost guard that rots. Temporal itself is never
mocked — the real time-skipping test server issues and records the real
commands, which is exactly what the replay assertions read.
"""
from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import temporalio.workflow
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from dse_contracts.activities import (
    ACTIVITY_EMIT_AUDIT,
    ACTIVITY_POST_TRACKING_COMMENT,
    ACTIVITY_RUN_TESTER_TURN,
    GateStatus,
)
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import (
    LOCAL_ACTIVITY_POST_STATUS_TRANSITION,
    LOCAL_ACTIVITY_RECORD_GATE,
    LOCAL_ACTIVITY_UPDATE_STATUS,
    check_clarification_completeness,
    emit_history_metric,
    resolve_budget_cap,
)
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import new_work_item_id
from fakes import FakeControlPlane, build_fake_activities

#: Short alias for the pure-helper assertions, as in test_fix_loop_feedback.py.
WF = WorkItemLifecycleWorkflow

#: The marker that keeps a run already inside the Coder loop replayable.
_PATCH_ID = "tester-infra-outcome-escalates-v1"

#: Verbatim head of what `sandbox_runtime.activities._infra_outcome_note` writes
#: for an OOM kill, with the limit deployed on the VPS. Copied rather than
#: imported: the orchestrator does not depend on the runtime, and the point of
#: the assertions below is that this text SURVIVES the trip to the ledger.
_OOM_NOTE = (
    "THE SUITE WAS KILLED FROM OUTSIDE (rc=137, SIGKILL). THIS IS NOT A TEST FAILURE: "
    "the process died mid-run, no assertion was evaluated, and nothing below is a "
    "verdict on the code.\n"
    "On this runtime that kill is the sandbox going over its MEMORY LIMIT of 1536Mi. "
    "The suite's own clock reports itself as rc=124, never 137, so time is not what "
    "ended this run.\n"
)


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides conftest's autouse skip — see the module docstring."""
    yield


def _tester(returncode: int, *, status: Any = None, failure_output: str = "",
            tests_ran: bool = True) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, status=status,
                           failure_output=failure_output, tests_ran=tests_ran)


# ---------------------------------------------------------------------------
# Classification and reason (pure — no Temporal, no Postgres)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (137, "resource_kill"),   # SIGKILL from outside = the cgroup OOM killer
        (124, "suite_hung"),      # coreutils `timeout` inside the Pod
        (-1001, "exec_timeout"),  # the worker's own kubectl-exec deadline
    ],
)
def test_each_infra_ending_is_named_apart(returncode, expected):
    """Three different problems with three different owners. Collapsing them
    into one bucket is how a memory limit and a hung test got the same fix."""
    assert WF._tester_infra_outcome(_tester(returncode)) == expected


def test_an_old_worker_that_never_says_error_is_still_recognised():
    """The incident itself came from a worker that reported rc=137 with no ERROR
    status at all, and a rolling deploy is precisely when the next one arrives.
    The returncode is a fact about the process; `status` is a newer opinion
    about it."""
    killed = _tester(137, status=GateStatus.FAIL, failure_output="...")
    assert WF._tester_infra_outcome(killed) == "resource_kill"


def test_an_unknown_code_with_an_error_status_is_still_infrastructure():
    """ERROR means the run produced NO verdict. No Coder turn can manufacture
    one, whatever exit code a future runtime chooses to say it with."""
    assert WF._tester_infra_outcome(_tester(99, status=GateStatus.ERROR)) == "infra_error"
    # a decoded payload may carry the bare string instead of the enum member
    assert WF._tester_infra_outcome(_tester(99, status="ERROR")) == "infra_error"


@pytest.mark.parametrize(
    ("returncode", "status"),
    [(1, GateStatus.FAIL), (0, GateStatus.PASS), (5, None)],
)
def test_a_real_verdict_is_not_an_infra_outcome(returncode, status):
    """The guard must not swallow the ordinary failing suite: that one IS the
    Coder's job, and stealing it would break the whole fix loop."""
    assert WF._tester_infra_outcome(_tester(returncode, status=status)) is None


def test_a_turn_that_authored_no_test_is_not_hijacked_from_the_contract_check():
    """The infra branch runs BEFORE the `tests_ran` contract check, so it must
    not swallow it: a Tester that simply produced nothing exits rc=0 with
    NOT_CONFIGURED and still has to reach `tester_contract_failed`."""
    silent = _tester(0, status=GateStatus.NOT_CONFIGURED, tests_ran=False)
    assert WF._tester_infra_outcome(silent) is None


def test_a_kill_with_no_test_authored_is_still_the_pod_dying():
    """And the reason the infra branch runs first: an OOM or an exec deadline can
    land before a single test file exists. Reported as `tests_ran=false` it reads
    as the model producing nothing, which blames the model for a dead Pod."""
    killed_early = _tester(137, status=GateStatus.ERROR, tests_ran=False)
    assert WF._tester_infra_outcome(killed_early) == "resource_kill"


def test_the_oom_reason_is_queryable_and_carries_the_deployed_limit():
    """Somebody will write `WHERE details->>'reason' LIKE 'tester_infra_%'`, and
    whoever reads the row has to learn WHICH limit to raise without opening the
    cluster. The number comes from the runtime's own text: the workflow cannot
    read the sandbox's environment, and a limit retyped here would go stale."""
    reason = WF._tester_infra_reason(
        "resource_kill", _tester(137, status=GateStatus.ERROR, failure_output=_OOM_NOTE)
    )
    assert reason.startswith("tester_infra_resource_kill:")
    assert "DSE_SANDBOX_MEM_LIMIT" in reason, "the operator is not told which knob to turn"
    assert "1536Mi" in reason, "the deployed limit did not survive the trip"
    assert "rc=137" in reason
    assert "\n" not in reason, "this lands in one TEXT column and one issue comment"


def test_the_hung_reason_admits_it_cannot_tell_stuck_from_slow():
    """The runtime refuses to assert which one it was; the escalation must not
    assert it either — that sentence is what sent the Coder hunting a handle
    that may never have existed."""
    reason = WF._tester_infra_reason("suite_hung", _tester(124))
    assert reason.startswith("tester_infra_suite_hung:")
    assert "slow" in reason and "Stuck" in reason


def test_a_reason_without_runtime_output_is_still_complete():
    """An older worker sends no `failure_output`. The sentence that names the
    owner of the problem must not depend on it."""
    reason = WF._tester_infra_reason("exec_timeout", _tester(-1001))
    assert reason.startswith("tester_infra_exec_timeout:")
    assert "worker" in reason
    assert "runtime said" not in reason


# ---------------------------------------------------------------------------
# DB-free stand-ins for the local Activities that write to Postgres.
# ---------------------------------------------------------------------------
@dataclass
class _Ledger:
    """What the workflow WOULD have persisted, read the same way
    `conftest.read_audit_actions` reads Postgres."""

    audit: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    comments: list[tuple[str, str]] = field(default_factory=list)

    @property
    def audit_actions(self) -> list[str]:
        return [action for action, _ in self.audit]

    @property
    def comment_statuses(self) -> list[str]:
        return [status for status, _ in self.comments]

    def all_details(self, action: str) -> list[dict[str, Any]]:
        return [details for recorded, details in self.audit if recorded == action]


def _db_free_activities(ledger: _Ledger, state: FakeControlPlane) -> list[Any]:
    async def emit_audit_event(payload: dict[str, Any]) -> None:
        ledger.audit.append((payload["action"], payload.get("details") or {}))

    async def update_work_item_status(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def record_plan_approval(payload: dict[str, Any]) -> dict[str, Any]:
        return {"persisted": False}

    async def post_tracking_comment(payload: dict[str, Any]) -> dict[str, Any]:
        ledger.comments.append((payload.get("status", ""),
                                str(payload.get("body") or payload.get("detail") or "")))
        return {"ok": True}

    async def post_status_transition(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "target_status": None}

    async def check_group_plan_gate(payload: dict[str, Any]) -> dict[str, Any]:
        # rc.93: a barreira de grupo pergunta logo após o gate — activity
        # ausente = NotFoundError retry storm no time-skipping.
        return {"in_group": False, "holding": False, "abort": False, "reason": ""}

    return [
        activity.defn(name=ACTIVITY_EMIT_AUDIT)(emit_audit_event),
        activity.defn(name=ACTIVITY_POST_TRACKING_COMMENT)(post_tracking_comment),
        activity.defn(name=LOCAL_ACTIVITY_UPDATE_STATUS)(update_work_item_status),
        activity.defn(name=LOCAL_ACTIVITY_RECORD_GATE)(record_plan_approval),
        activity.defn(name=LOCAL_ACTIVITY_POST_STATUS_TRANSITION)(post_status_transition),
        activity.defn(name="check_group_plan_gate")(check_group_plan_gate),
        # Real ones: the checklist is pure, the budget default reads env only,
        # and the history metric is OTel-only.
        check_clarification_completeness,
        resolve_budget_cap,
        emit_history_metric,
    ] + build_fake_activities(state)


def _wf_input(work_item_id: str, **overrides: Any) -> WorkItemLifecycleInput:
    base = dict(
        work_item_id=work_item_id,
        tenant_id="test-tenant",
        requester="usr_test",
        repo="acme/repo",
        base_branch="main",
        acceptance_criteria="crit",
    )
    base.update(overrides)
    return WorkItemLifecycleInput(**base)


async def _history_events(client, work_item_id: str) -> list[dict[str, Any]]:
    from google.protobuf.json_format import MessageToDict

    history = await client.get_workflow_handle(work_item_id).fetch_history()
    return [MessageToDict(event) for event in history.events]


def _patch_marker(events: list[dict[str, Any]], patch_id: str) -> dict[str, Any] | None:
    """The `workflow.patched()` marker as recorded: markerName `core_patch`,
    with the patch id inside a base64 JSON payload."""
    for event in events:
        attrs = event.get("markerRecordedEventAttributes") or {}
        if attrs.get("markerName") != "core_patch":
            continue
        payloads = (attrs.get("details") or {}).get("patch-data", {}).get("payloads") or []
        for payload in payloads:
            if json.loads(base64.b64decode(payload["data"])).get("id") == patch_id:
                return event
    return None


def _scheduled(events: list[dict[str, Any]], activity_name: str) -> list[dict[str, Any]]:
    return [e for e in events
            if (e.get("activityTaskScheduledEventAttributes") or {})
            .get("activityType", {}).get("name") == activity_name]


# ---------------------------------------------------------------------------
# The decision itself (real Temporal, time-skipping, no Postgres)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_oom_kill_escalates_without_buying_a_single_coder_retry(time_skipping_env):
    """The incident, end to end. Three Coder turns at ~US$1.03 used to follow
    this exact Tester result; the only acceptable number is zero."""
    work_item_id = new_work_item_id("oomkill")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(tester_returncode=137, tester_tests_passed=False)
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _wf_input(work_item_id, coder_retry_cap=3),
            id=work_item_id, task_queue=task_queue,
        )
        result = await handle.result()
        # Queried after the close, which replays the whole run: the counter is
        # read from the definition that is shipping, not from a mock.
        final = await handle.query(WorkItemLifecycleWorkflow.get_state)

    assert result.status == WorkItemStatus.escalated.value
    assert state.tester_calls == 1
    assert state.coder_turn_calls == 1, (
        "the OOM bought another Coder turn — this is the US$1.03-a-round bleed"
    )
    assert final["coder_retry_count"] == 0, "an infra kill consumed a code-fix retry"
    assert "tester_failed_retrying" not in ledger.audit_actions

    [row] = ledger.all_details("tester_infra_outcome")
    assert row["outcome"] == "resource_kill"
    assert row["decision"] == "escalate"
    assert row["attempt"] == 0
    assert row["returncode"] == 137
    # The terminal record names the cause instead of "tester_failed_after_retry_cap".
    assert "tester_infra_resource_kill" in (result.detail or "")
    assert "DSE_SANDBOX_MEM_LIMIT" in (result.detail or "")
    assert "escalated" in ledger.comment_statuses, "the surface never learned why"


@pytest.mark.asyncio
async def test_an_ordinary_failing_suite_still_retries_the_coder_to_the_cap(time_skipping_env):
    """The case the branch was NOT written for. A failing assertion is the
    Coder's job and must still walk the loop exactly as before."""
    work_item_id = new_work_item_id("suitefail")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(tester_returncode=1, tester_tests_passed=False)
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=_db_free_activities(ledger, state)):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run,
            _wf_input(work_item_id, coder_retry_cap=2),
            id=work_item_id, task_queue=task_queue,
        )

    assert result.status == WorkItemStatus.failed.value
    assert "tester_failed_after_retry_cap" in (result.detail or "")
    assert state.coder_turn_calls == 3, "1 initial turn + 2 retries, the cap"
    assert ledger.audit_actions.count("tester_failed_retrying") == 2
    assert ledger.all_details("tester_infra_outcome") == [], (
        "a failing assertion was misread as an infrastructure ending"
    )


@pytest.mark.asyncio
async def test_a_hung_suite_buys_one_coder_turn_and_only_one(time_skipping_env):
    """The ambiguous ending. A test the Tester itself authored can hang on a
    handle it never closed, and a Coder turn CAN close that — so one turn is
    worth buying. The second and third would face byte-identical input (same
    note, same wall clock), so they buy nothing, and a suite that is merely
    slower than the runtime's budget cannot be fixed from inside the repo at
    all."""
    work_item_id = new_work_item_id("suitehang")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(tester_returncode=124, tester_tests_passed=False)
    ledger = _Ledger()

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            # A cap of 3 is the deployed default: the point is that the hang
            # stops at 1 by its own rule, not by running out of cap.
            _wf_input(work_item_id, coder_retry_cap=3),
            id=work_item_id, task_queue=task_queue,
        )
        result = await handle.result()
        final = await handle.query(WorkItemLifecycleWorkflow.get_state)

    assert result.status == WorkItemStatus.escalated.value
    assert state.coder_turn_calls == 2, "the hang bought more than the one turn it is worth"
    assert state.tester_calls == 2
    assert final["coder_retry_count"] == 1, "the retry must count against the shared cost cap"

    decisions = [(r["decision"], r["attempt"]) for r in ledger.all_details("tester_infra_outcome")]
    assert decisions == [("retry_once", 0), ("escalate", 1)]
    # The retry has to carry the runtime's explanation, or the second turn is
    # the same request again — the bug `fix-loop-carries-the-failure-v1` fixed.
    assert ledger.audit_actions.count("tester_failed_retrying") == 1
    assert "tester_infra_suite_hung" in (result.detail or "")


# ---------------------------------------------------------------------------
# Replay safety
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_marker_is_recorded_after_the_tester_runs(time_skipping_env):
    """Where the fork sits in the command stream, which is what decides whether
    the live executions survive the deploy.

    The marker is recorded only AFTER `run_tester_turn` has completed, i.e. late
    inside the Coder loop — never at the start of the run, where the executions
    with tens of thousands of events already have their commands recorded. A
    history that stops earlier never evaluates this patch id at all.

    On its own this shows placement plus determinism of the new path;
    `test_a_pre_patch_history_replays_only_because_of_the_guard` is the test
    that fails if the guard is deleted.
    """
    work_item_id = new_work_item_id("oom-patchguard")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(tester_returncode=137, tester_tests_passed=False)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=_db_free_activities(_Ledger(), state)):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
    assert result.status == WorkItemStatus.escalated.value

    events = await _history_events(time_skipping_env.client, work_item_id)
    marker = _patch_marker(events, _PATCH_ID)
    assert marker is not None, "the infra branch is NOT behind a patch marker"
    tester = _scheduled(events, ACTIVITY_RUN_TESTER_TURN)
    assert tester, "no Tester turn in this history"
    assert int(marker["eventId"]) > int(tester[0]["eventId"]), (
        "the marker is evaluated before the Tester result exists — the fork "
        "must sit late in the Coder loop, not at the top of the run"
    )

    # And the new path itself replays.
    await Replayer(
        workflows=[WorkItemLifecycleWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(WorkflowHistory.from_json(work_item_id, {"events": events}))


@pytest.mark.asyncio
async def test_a_pre_patch_history_replays_only_because_of_the_guard(
    time_skipping_env, monkeypatch
):
    """THE replay-safety test: it fails if `workflow.patched()` is removed.

    A run already inside the Coder loop when this deploys is the case that
    matters — two live executions carry more than 30,000 events each. Such a
    history cannot be faked, so it is MANUFACTURED: the real workflow issues the
    real commands with `workflow.patched(_PATCH_ID)` returning False exactly as
    it does for a run that started before the deploy, while every OTHER patch id
    keeps its genuine behaviour. What gets recorded is the old bleed itself —
    rc=137 three times, three paid Coder turns, then the retry cap.

    The same history is then replayed against the CURRENT definition:
      - as shipped -> `patched()` finds no marker, takes the old branch,
        reproduces the commands, replay PASSES;
      - with only this guard bypassed (i.e. the code you get by deleting the
        `if`) -> the escalation issues commands the history does not contain and
        replay FAILS as nondeterministic.

    Both workers run unsandboxed because that is what lets the monkeypatch of
    `temporalio.workflow.patched` reach the workflow code.
    """
    work_item_id = new_work_item_id("oom-prepatch")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(tester_returncode=137, tester_tests_passed=False)
    ledger = _Ledger()
    real_patched = temporalio.workflow.patched

    def patched_before_the_deploy(patch_id: str) -> bool:
        if patch_id == _PATCH_ID:
            return False  # and, crucially, records no marker
        return real_patched(patch_id)

    monkeypatch.setattr(temporalio.workflow, "patched", patched_before_the_deploy)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      workflow_runner=UnsandboxedWorkflowRunner(),
                      activities=_db_free_activities(ledger, state)):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run,
            _wf_input(work_item_id, coder_retry_cap=2),
            id=work_item_id, task_queue=task_queue,
        )

    # The pre-patch behaviour, recorded: the OOM paid for two extra turns.
    assert result.status == WorkItemStatus.failed.value
    assert state.coder_turn_calls == 3
    assert ledger.all_details("tester_infra_outcome") == []

    events = await _history_events(time_skipping_env.client, work_item_id)
    assert _patch_marker(events, _PATCH_ID) is None, "not a pre-patch history"

    monkeypatch.setattr(temporalio.workflow, "patched", real_patched)
    await Replayer(
        workflows=[WorkItemLifecycleWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
        data_converter=pydantic_data_converter,
    ).replay_workflow(WorkflowHistory.from_json(work_item_id, {"events": events}))

    # The negative half — without it this file would pass with the guard deleted.
    def guard_deleted(patch_id: str) -> bool:
        return True if patch_id == _PATCH_ID else real_patched(patch_id)

    monkeypatch.setattr(temporalio.workflow, "patched", guard_deleted)
    with pytest.raises(Exception) as excinfo:
        await Replayer(
            workflows=[WorkItemLifecycleWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            data_converter=pydantic_data_converter,
        ).replay_workflow(WorkflowHistory.from_json(work_item_id, {"events": events}))
    assert "nondeterminism" in str(excinfo.value).lower(), (
        "replaying a pre-patch history with the guard bypassed must be rejected "
        f"as nondeterministic, got: {excinfo.value!r}"
    )
