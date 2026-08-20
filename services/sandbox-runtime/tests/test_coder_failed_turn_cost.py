"""What a Coder turn that FAILED after spending money reports, and to whom.

`remote_substrate.run_turn` accumulates the cost before it raises, so the money
is real and known — but the Activity's `raise` jumps over `collect_artifacts()`
and everything after it, so the spend used to reach nobody: no row in
model_call_ledger (the console rollup under-reports) and no `cost_usd` on any
result (the workflow's $25 ceiling never counts it). The Activity runs under
`RetryPolicy(maximum_attempts=0)`, so the same turn can fail again and again,
each attempt spending and each one disappearing.

The fix has to be BOTH ends or neither: a ledger row on a path the ceiling
cannot count would only build the opposite asymmetry. So these pin, on the
failure path, the ledger row AND the payload the workflow can read off the
error — plus the two ways to get it wrong: booking a turn twice, and booking a
turn that never spent anything.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from dse_contracts import (
    AgentTurnResult,
    CheckpointOpResult,
    PostTurnResult,
    RunCoderTurnInput,
)
from temporalio.exceptions import ApplicationError

from sandbox_runtime import activities
from sandbox_runtime.activities import (
    FAILED_TURN_SPEND_KEY,
    _error_carrying_spend,
    _run_coder_turn_impl,
)
from sandbox_runtime.driver import StageExecutionResult
from sandbox_runtime.model_gateway_client import VirtualKeyResult
from sandbox_runtime.remote_substrate import RemoteSubstrate, RemoteTurnError

_TENANT = "tenant-a"
_WORK_ITEM = "wi-cost1"
# id the stubbed ledger hands back for the first row it accepts
_FIRST_LEDGER_ID = 4201


class _PodDriver:
    """Every boundary the Activity crosses in K8s mode, stubbed: the checkpoint
    that gives the turn its start sha, the stage execution that IS the turn, and
    the post-turn. `workspace_is_host_visible=False` keeps ALL git inside the
    (simulated) Pod, so the test never needs a repo, a cluster or Postgres — it
    stays on the money path.

    One argument per turn: an `AgentTurnResult` to return, or an exception to
    raise (an exec that never produced a result at all)."""

    workspace_is_host_visible = False

    def __init__(self, *turns: Any):
        self._turns = list(turns)
        self.stage_calls = 0


    def sandbox_id_for(self, work_item_id: str) -> str:
        return f"pod-{work_item_id}"

    def execute_op(self, sandbox_id, op, payload, *, timeout_seconds=180.0):
        if op == "checkpoint":
            return CheckpointOpResult(sha="base0", phase=payload["phase"]).model_dump()
        if op == "post_turn":
            return PostTurnResult(sha="head1", files_changed=["src/app.py"]).model_dump()
        raise AssertionError(f"unexpected op: {op}")

    def execute_stage(self, request):
        nxt = self._turns[self.stage_calls]
        self.stage_calls += 1
        if isinstance(nxt, BaseException):
            raise nxt
        return StageExecutionResult(
            stage=request.stage, output_payload=nxt.model_dump(), exit_code=0, duration_seconds=0.01
        )


def _turn(*, done=True, cost=0.0, tokens=(0, 0), error=None, kind=None) -> AgentTurnResult:
    return AgentTurnResult(
        done=done, cost_usd=cost, tokens_in=tokens[0], tokens_out=tokens[1],
        error=error, error_kind=kind,
    )


def _failed_turn(*, cost, tokens=(0, 0), error="the CLI died mid-turn", kind="substrate_error"):
    return _turn(done=False, cost=cost, tokens=tokens, error=error, kind=kind)


@pytest.fixture()
def ledger(monkeypatch):
    """The rows that would land in model_call_ledger. `record_call` is imported
    INSIDE the Activity, so the module attribute is what has to be patched."""
    rows: list[dict] = []

    def _record(**kw):
        rows.append(kw)
        return _FIRST_LEDGER_ID + len(rows) - 1

    monkeypatch.setattr("model_gateway_client.ledger.record_call", _record)
    return rows


@pytest.fixture()
def audits(monkeypatch):
    """audit_emit talks to Postgres; capture it and read the rows instead."""
    rows: list[dict] = []
    monkeypatch.setattr(activities, "audit_emit", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture(autouse=True)
def _minted_key(monkeypatch):
    """Without this every test pays the model-gateway's connect timeout before
    falling back to the fixture key."""
    monkeypatch.setattr(
        activities,
        "mint_virtual_key",
        lambda headers: VirtualKeyResult(
            virtual_key="vk-test",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            gateway_base_url="http://model-gateway:4000",
            fixture=True,
        ),
    )


def _run(driver: _PodDriver, *, substrate_name: str = "claude-agent"):
    agent = RemoteSubstrate(driver=driver, substrate_name=substrate_name)
    return asyncio.run(
        _run_coder_turn_impl(
            RunCoderTurnInput(
                work_item_id=_WORK_ITEM, tenant_id=_TENANT, instruction="fix the summary",
            ),
            substrate=agent,
        )
    )


def _spend(exc: BaseException) -> dict | None:
    """Exactly what the workflow will have to do: find the spend among the
    error's details."""
    for detail in getattr(exc, "details", ()) or ():
        if isinstance(detail, dict) and FAILED_TURN_SPEND_KEY in detail:
            return detail[FAILED_TURN_SPEND_KEY]
    return None


def _rows(audits: list[dict], action: str) -> list[dict]:
    return [a for a in audits if a["action"] == action]


# ---------------------------------------------------------------------------
# spent, then failed — the money reaches both surfaces
# ---------------------------------------------------------------------------


def test_a_turn_that_spends_and_then_fails_is_booked_on_the_ledger(ledger, audits, state_dir):
    """The half the console reads: without a row here, the rollup computed from
    model_call_ledger alone silently under-reports every failed attempt."""
    with pytest.raises(ApplicationError):
        _run(_PodDriver(_failed_turn(cost=0.75, tokens=(300, 90))))

    assert len(ledger) == 1, "the failed turn's spend has to be booked exactly once"
    row = ledger[0]
    assert row["cost_usd"] == pytest.approx(0.75)
    assert (row["tokens_in"], row["tokens_out"]) == (300, 90)
    assert (row["tenant_id"], row["work_item_id"], row["stage"]) == (_TENANT, _WORK_ITEM, "coder")

    failed = _rows(audits, "coder_turn_failed_after_spend")
    assert len(failed) == 1, "a human asking why this item cost money needs a durable trail"
    assert failed[0]["details"]["ledger_id"] == _FIRST_LEDGER_ID
    assert failed[0]["details"]["cost_usd"] == pytest.approx(0.75)
    assert "substrate_error" in failed[0]["details"]["error"]
    # NOT `coder_turn_completed`: the turn did not complete, and the projector
    # builds a run from that action.
    assert _rows(audits, "coder_turn_completed") == []


def test_the_failed_turn_hands_its_spend_to_the_workflow(ledger, audits, state_dir):
    """The other end of the fix. A ledger row alone would build the OPPOSITE
    asymmetry — money the ledger sees and the ceiling never counts — so the
    same numbers travel on the error, the only channel a failed Activity has."""
    with pytest.raises(ApplicationError) as err:
        _run(_PodDriver(_failed_turn(cost=0.75, tokens=(300, 90))))

    spend = _spend(err.value)
    assert spend is not None, "the workflow cannot sum what the error does not carry"
    assert spend["cost_usd"] == pytest.approx(0.75)
    assert (spend["tokens_in"], spend["tokens_out"]) == (300, 90)
    assert spend["stage"] == "coder" and spend["work_item_id"] == _WORK_ITEM
    # Points at the row just written: the same money, identified, on both sides.
    assert len(ledger) == 1 and spend["ledger_id"] == _FIRST_LEDGER_ID


def test_what_the_workflow_classifies_on_is_left_alone(ledger, audits, state_dir):
    """Attaching the spend must not turn a retryable substrate failure into
    something else: the workflow decides on `type`, `non_retryable` and (legacy
    fallback) the message."""
    with pytest.raises(ApplicationError) as err:
        _run(_PodDriver(_failed_turn(cost=0.75, error="the CLI died mid-turn")))

    assert err.value.type == "RemoteTurnError"  # what Temporal itself would have used
    assert err.value.non_retryable is False
    assert "the CLI died mid-turn" in str(err.value)
    assert "[substrate_error]" in str(err.value)


def _roundtrip(exc: BaseException):
    """The error as the WORKFLOW receives it: through Temporal's failure
    serialization, not as a live Python object."""
    import temporalio.api.failure.v1 as failure_pb
    from temporalio.converter import DataConverter

    async def _go():
        proto = failure_pb.Failure()
        await DataConverter.default.encode_failure(exc, proto)
        return await DataConverter.default.decode_failure(proto)

    return asyncio.run(_go())


def test_the_spend_survives_the_trip_to_the_workflow():
    """The handover contract. `details` is the only slot that crosses, and it
    crosses through a proto + the data converter — a payload that does not
    serialize would fail silently, in production, on the money path."""
    original = RemoteTurnError("substrate_error", "the CLI died mid-turn")
    spend = {"cost_usd": 0.75, "tokens_in": 300, "tokens_out": 90,
             "ledger_id": 4201, "stage": "coder", "work_item_id": _WORK_ITEM}

    received = _roundtrip(_error_carrying_spend(original, {FAILED_TURN_SPEND_KEY: spend}))

    assert _spend(received) == spend


def test_the_error_the_workflow_sees_is_byte_identical_but_for_the_details():
    """The blob the workflow builds to classify a failure
    (`f"{type(cause).__name__}:{cause}"`, workflows.py) must not move: a marker
    that stops matching turns a fail-closed refusal into an infra retry."""
    original = RemoteTurnError("egress_blocked", "egress-proxy unavailable")

    def _blob(exc):
        received = _roundtrip(exc)
        return f"{type(received).__name__}:{received}".lower()

    before = _blob(original)  # what Temporal produces on its own today
    after = _blob(_error_carrying_spend(original, {FAILED_TURN_SPEND_KEY: {"cost_usd": 0.75}}))

    assert after == before
    assert "egress" in after, "the fail-closed marker still has to hit"


# ---------------------------------------------------------------------------
# failed before spending — nothing is booked and nothing is reshaped
# ---------------------------------------------------------------------------


def test_an_exec_that_never_ran_books_nothing(ledger, audits, state_dir):
    """No result came back at all: nothing was measured, so a ledger row would
    be an invented charge."""
    with pytest.raises(RuntimeError) as err:
        _run(_PodDriver(RuntimeError("kubectl exec: pod not found")))

    assert ledger == []
    assert _rows(audits, "coder_turn_failed_after_spend") == []
    # unchanged shape: not even wrapped, so nothing downstream reads differently
    assert not isinstance(err.value, ApplicationError)
    assert str(err.value) == "kubectl exec: pod not found"


def test_a_failure_with_a_zero_cost_books_nothing(ledger, audits, state_dir):
    """The runner answered, and the answer is that it spent nothing (refused
    payload, substrate that never reached the provider)."""
    with pytest.raises(RemoteTurnError) as err:  # NOT reshaped into anything
        _run(_PodDriver(_failed_turn(cost=0.0, kind="invalid_payload")))

    assert ledger == []
    assert _rows(audits, "coder_turn_failed_after_spend") == []
    assert _spend(err.value) is None


# ---------------------------------------------------------------------------
# exactly once
# ---------------------------------------------------------------------------


def test_a_successful_turn_is_still_booked_exactly_once(ledger, audits, state_dir):
    """The path that already worked stays as it was — same single row, same
    ledger_id on the result and on the audit."""
    result = _run(_PodDriver(_turn(cost=0.5, tokens=(100, 40))))

    assert len(ledger) == 1 and ledger[0]["cost_usd"] == pytest.approx(0.5)
    assert result.ledger_id == _FIRST_LEDGER_ID
    assert result.cost_usd == pytest.approx(0.5)
    completed = _rows(audits, "coder_turn_completed")
    assert len(completed) == 1 and completed[0]["details"]["ledger_id"] == _FIRST_LEDGER_ID
    assert _rows(audits, "coder_turn_failed_after_spend") == []


def test_a_multi_turn_attempt_that_fails_is_booked_once_for_the_whole_attempt(
    ledger, audits, state_dir
):
    """The substrate accumulates across the turns of ONE attempt. Booking per
    turn would charge the first turn twice; booking nothing loses both."""
    driver = _PodDriver(
        _turn(done=False, cost=0.4, tokens=(100, 30)),
        _failed_turn(cost=0.25, tokens=(50, 20)),
    )
    with pytest.raises(ApplicationError) as err:
        _run(driver)

    assert driver.stage_calls == 2
    assert len(ledger) == 1
    assert ledger[0]["cost_usd"] == pytest.approx(0.65)
    assert (ledger[0]["tokens_in"], ledger[0]["tokens_out"]) == (150, 50)
    assert _spend(err.value)["cost_usd"] == pytest.approx(0.65)


# ---------------------------------------------------------------------------
# the two failure modes of the failure path itself
# ---------------------------------------------------------------------------


def test_exhausted_credits_keep_their_class_and_still_carry_the_money(ledger, audits, state_dir):
    """The one failure GUARANTEED to arrive after money was spent. Its
    non-retryable classification is what stops the loop, and it must survive the
    spend riding along."""
    with pytest.raises(ApplicationError) as err:
        _run(_PodDriver(_failed_turn(
            cost=1.2, tokens=(400, 10),
            error="Your credit balance is too low to access the Anthropic API",
        )))

    assert err.value.type == "dse.failure.provider_billing"
    assert err.value.non_retryable is True
    assert len(ledger) == 1 and ledger[0]["cost_usd"] == pytest.approx(1.2)
    assert _spend(err.value)["cost_usd"] == pytest.approx(1.2)


def test_a_ledger_outage_never_replaces_the_turns_own_error(monkeypatch, audits, state_dir):
    """Bookkeeping must not become the failure the workflow sees — it would
    misclassify the turn AND hide why it really died."""
    def _boom(**kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("model_gateway_client.ledger.record_call", _boom)

    with pytest.raises(ApplicationError) as err:
        _run(_PodDriver(_failed_turn(cost=0.75, error="the CLI died mid-turn")))

    assert "the CLI died mid-turn" in str(err.value)
    # The money is still reported to the workflow; only the row is missing, and
    # that miss is audited on its own action.
    spend = _spend(err.value)
    assert spend["cost_usd"] == pytest.approx(0.75) and spend["ledger_id"] is None
    assert _rows(audits, "coder_cost_ledger_write_failed")[0]["details"]["outcome"] == "failed"


def test_a_scripted_turn_writes_no_row_but_still_reports_its_spend(ledger, audits, state_dir):
    """Mirrors the success path exactly: FakeSubstrate money never enters the
    ledger (the guard is the substrate name), but it is still reported, because
    the scripted tests' ceiling arithmetic has to keep working."""
    with pytest.raises(ApplicationError) as err:
        _run(_PodDriver(_failed_turn(cost=0.03)), substrate_name="fake")

    assert ledger == []
    spend = _spend(err.value)
    assert spend["cost_usd"] == pytest.approx(0.03) and spend["ledger_id"] is None
