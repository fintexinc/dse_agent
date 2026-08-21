"""L1 heartbeat (item 1.2).

Before this, `run_l1_pipeline` was `await asyncio.to_thread(_run_l1_pipeline)`
and emitted NOTHING: a real suite of 10-15 minutes was killed by the server at
`activity_heartbeat_seconds` (600 s) with ACTIVITY_TASK_TIMED_OUT, and the `test`
finding was neither PASS nor FAIL — it never existed.

These tests run against the REAL `temporalio` heartbeat surface
(`ActivityEnvironment`), not a mock of it, and none of them sleeps for longer
than the pipeline they simulate: the beat rate is derived from the Activity's own
`heartbeat_timeout`, so shrinking the timeout shrinks the test.
"""
from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from datetime import timedelta

import pytest
from dse_contracts import L1Result, PlanArtifact
from temporalio.testing import ActivityEnvironment

from dse_validation import activities
from dse_validation.activities import RunL1PipelineInput
from dse_validation.l1.pipeline import run_l1_pipeline_core


def _l1_input() -> RunL1PipelineInput:
    return RunL1PipelineInput(
        work_item_id="wi_heartbeat",
        tenant_id="tnt-1",
        plan=PlanArtifact(work_item_id="wi_heartbeat"),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )


def _environment(heartbeat_timeout: timedelta | None) -> tuple[ActivityEnvironment, list[dict]]:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, heartbeat_timeout=heartbeat_timeout)
    beats: list[dict] = []
    env.on_heartbeat = lambda details: beats.append(details)
    return env, beats


def test_heartbeats_are_emitted_while_the_pipeline_is_still_running(monkeypatch):
    """The point of the item: beats DURING the work, naming the live stage."""
    reached_test = threading.Event()

    def slow_pipeline(inp, on_step=None):
        on_step("lint")
        time.sleep(0.06)
        on_step("test")  # the stage a real repo spends its 10 minutes in
        reached_test.set()
        time.sleep(0.12)
        return L1Result(work_item_id=inp.work_item_id, passed=True, findings=[])

    monkeypatch.setattr(activities, "_run_l1_pipeline", slow_pipeline)
    # 0.15 s of heartbeat timeout => 0.05 s of interval, by the same rule that
    # turns the production 600 s into 20 s.
    env, beats = _environment(timedelta(seconds=0.15))

    result = asyncio.run(env.run(activities.run_l1_pipeline, _l1_input()))

    assert result.passed is True
    assert reached_test.is_set()
    states = [b["state"] for b in beats]
    assert states[0] == "started"
    assert "running" in states, "a pipeline in progress must beat before it returns"
    assert states[-1] == "completed"
    assert [b["sequence"] for b in beats] == list(range(len(beats)))
    assert all(b["work_item_id"] == "wi_heartbeat" for b in beats)
    assert all(b["stage"] == "l1" for b in beats)

    # The payload tracks the LIVE stage: this is what turns "the Activity died"
    # into "it died in `test`, N seconds in".
    running_stages = [b["operation"] for b in beats if b["state"] == "running"]
    assert "test" in running_stages
    last = beats[-1]
    assert last["operation"] == "test"
    assert last["elapsed_seconds"] <= last["total_elapsed_seconds"]


def test_heartbeat_interval_is_a_third_of_the_timeout_capped_at_20s():
    """Chosen relative to the heartbeat timeout (two missed beats of slack), and
    capped so the details never name a stage the pipeline already left."""
    for timeout, expected in (
        (timedelta(seconds=600), 20.0),  # production: capped
        (timedelta(seconds=30), 10.0),  # a third
        (timedelta(seconds=0.15), 0.05),
        (None, 20.0),  # activity scheduled without a heartbeat timeout
    ):
        env, _beats = _environment(timeout)
        assert env.run(activities._l1_heartbeat_interval) == pytest.approx(expected)


def test_cancelled_activity_stops_the_pipeline_at_the_next_stage_boundary(monkeypatch):
    """`asyncio.to_thread` cannot interrupt the thread. The stage boundary is the
    only lever: without it a cancelled L1 keeps running every remaining check on
    the sandbox's single vCPU, and then writes validation_runs/audit rows for a
    run nobody reads."""
    visited: list[str] = []
    finished_normally = threading.Event()

    def pipeline(inp, on_step=None):
        for stage in ("lint", "typecheck", "test", "build", "sast"):
            on_step(stage)
            visited.append(stage)
            time.sleep(0.03)
        finished_normally.set()
        return L1Result(work_item_id=inp.work_item_id, passed=True, findings=[])

    monkeypatch.setattr(activities, "_run_l1_pipeline", pipeline)
    env, _beats = _environment(timedelta(seconds=0.15))

    async def cancel_midway():
        task = asyncio.ensure_future(env.run(activities.run_l1_pipeline, _l1_input()))
        await asyncio.sleep(0.05)
        env.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # give the abandoned thread time to reach its next boundary and stop
        await asyncio.sleep(0.2)

    asyncio.run(cancel_midway())

    assert visited, "the pipeline must have started before the cancellation"
    assert not finished_normally.is_set(), "a cancelled L1 must not run to the end"
    assert len(visited) < 5


def test_core_reports_every_stage_in_order(sandbox, work_item_id, tenant_id, git_sha, monkeypatch):
    """Pins the vocabulary the heartbeat publishes against the pipeline that
    really runs: a check added without an `on_step` becomes an invisible stretch
    of an Activity that only reports the PREVIOUS stage's name."""
    monkeypatch.setattr("dse_validation.l1.pipeline.audit_emit", None)
    seen: list[str] = []
    sha = git_sha()

    run_l1_pipeline_core(
        executor=sandbox,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        plan=PlanArtifact(work_item_id=work_item_id, no_code_change=True, diff_budget_lines=400),
        base_sha=sha,
        head_sha=sha,
        persist=False,
        on_step=seen.append,
    )

    assert seen == [
        "l1_manifest",
        # The diff is computed BEFORE the per-file gates, because it is what
        # scopes them to the change instead of to the customer's whole
        # repository. It gets its own stage name for the same reason every
        # other one does: an operator watching the heartbeat has to see where
        # the time is going.
        "diff",
        # A ordem é por CUSTO, e ela é o contrato: baratos e de segurança
        # primeiro, `test` e `build` por último. Um gate caro que rode antes de
        # um barato volta a comprar 7 minutos de suíte para reprovar por uma
        # formatação já encontrada em 7 segundos (medido em wi_2325adc).
        "lint",
        "typecheck",
        "sast",
        "secret_scan",
        "plan_compliance",
        "test",
        "build",
        "persist",
    ]


def test_core_without_on_step_is_unchanged(sandbox, work_item_id, tenant_id, git_sha, monkeypatch):
    """The hook is optional: every existing caller (and every test) keeps calling
    the core as a plain synchronous function."""
    monkeypatch.setattr("dse_validation.l1.pipeline.audit_emit", None)
    sha = git_sha()

    result = run_l1_pipeline_core(
        executor=sandbox,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        plan=PlanArtifact(work_item_id=work_item_id, no_code_change=True, diff_budget_lines=400),
        base_sha=sha,
        head_sha=sha,
        persist=False,
    )

    assert {f.check for f in result.findings} == {
        "lint", "typecheck", "test", "build", "sast", "secret_scan",
        "diff_budget", "forbidden_paths",
    }
