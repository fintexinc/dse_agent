"""Phase 1 (plan 09): RemoteSubstrate — the worker dispatches the typed contract.

Proves, without docker/k8s (StubDriver), the boundary's properties:
  - no host path crosses over (the request's workspace is /workspace);
  - only the ephemeral virtual key travels (never the master key);
  - a runner failure becomes a RemoteTurnError with a closed kind (no substring
    matching), WITHOUT losing what that turn already spent;
  - the in-process ↔ isolated selection is deployment config
    (`DSE_SANDBOX_INPROCESS`), with no workflow code change;
  - the Coder's full deterministic pipeline (prune → revert test edits → scoped
    commit/push) works with the turn executed "remotely".
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from dse_contracts import AgentTurnResult, RunCoderTurnInput, Stage
from pydantic import ValidationError

from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    _build_substrate,
    _paths_for,
    _run_coder_turn_impl,
    provision_sandbox,
    teardown_sandbox,
)
from sandbox_runtime.driver import StageExecutionRequest, StageExecutionResult
from sandbox_runtime.remote_substrate import RemoteSubstrate, RemoteTurnError
from sandbox_runtime.substrate import FakeSubstrate

from dse_contracts import GatewayCallHeaders


@dataclass
class StubDriver:
    """Minimal SandboxDriver: records the request and returns a scripted result.
    `write_into` simulates the bind mount's effect: the runner edited /workspace
    and the worker sees the edit on the host path."""

    result: dict[str, Any] = field(default_factory=lambda: AgentTurnResult(done=True).model_dump())
    write_into: str | None = None
    write_files: dict[str, str] = field(default_factory=dict)
    requests: list[StageExecutionRequest] = field(default_factory=list)
    # exec that blows up without ever producing a result (no such container,
    # kubectl exec refused): nothing was measured, nothing to meter.
    fail_with: Exception | None = None


    def sandbox_id_for(self, work_item_id: str) -> str:
        return f"stub-sbx-{work_item_id}"

    def execute_stage(self, request: StageExecutionRequest) -> StageExecutionResult:
        self.requests.append(request)
        if self.fail_with:
            raise self.fail_with
        if self.write_into:
            import os

            for rel, content in self.write_files.items():
                dest = os.path.join(self.write_into, rel)
                os.makedirs(os.path.dirname(dest) or self.write_into, exist_ok=True)
                with open(dest, "w") as fh:
                    fh.write(content)
        return StageExecutionResult(
            stage=request.stage, output_payload=self.result, exit_code=0, duration_seconds=0.01
        )


def _headers(wi: str = "wi_r1") -> GatewayCallHeaders:
    return GatewayCallHeaders(
        tenant_id="tenant-a", work_item_id=wi, stage=Stage.coder,
        task_class="feature", data_class="internal",
    )


def _session(sub: RemoteSubstrate, wi: str = "wi_r1", workspace: str = "/tmp/host-ws") -> None:
    sub.create_session(
        work_item_id=wi,
        workspace_dir=workspace,
        gateway_headers=_headers(wi),
        virtual_key="vk-ephemeral",
        gateway_base_url="http://localhost:4000",
    )


def test_request_never_carries_host_paths_or_master_key(tmp_path):
    driver = StubDriver()
    sub = RemoteSubstrate(driver=driver, substrate_name="fake", fake_script=[{"done": True}])
    _session(sub, workspace=str(tmp_path))

    sub.run_turn("implement the handler")

    assert len(driver.requests) == 1
    payload = driver.requests[0].input_payload
    assert payload["workspace_dir"] == "/workspace"  # never the host path
    assert str(tmp_path) not in str(payload)
    assert payload["gateway"]["virtual_key"] == "vk-ephemeral"
    assert payload["fake_script"] == [{"done": True}]
    assert "X-Dse-Work-Item-Id" in payload["gateway"]["headers"]
    # exec slack: the request timeout > the turn timeout
    assert driver.requests[0].timeout_seconds > payload["timeout_seconds"]


def test_gateway_url_override_for_in_network_reachability(tmp_path, monkeypatch):
    monkeypatch.setenv("DSE_SANDBOX_GATEWAY_URL", "http://model-gateway:4000")
    driver = StubDriver()
    sub = RemoteSubstrate(driver=driver, substrate_name="fake")
    _session(sub, workspace=str(tmp_path))
    sub.run_turn("x")
    assert driver.requests[0].input_payload["gateway"]["base_url"] == "http://model-gateway:4000"


def test_cost_accumulates_and_collect_artifacts(tmp_path):
    driver = StubDriver(
        result=AgentTurnResult(done=True, cost_usd=0.5, tokens_in=100, tokens_out=40).model_dump()
    )
    sub = RemoteSubstrate(driver=driver, substrate_name="claude-agent")
    _session(sub, workspace=str(tmp_path))
    log1 = sub.run_turn("a")
    sub.run_turn("b")
    assert log1.done
    artifacts = sub.collect_artifacts()
    assert artifacts.cost_usd == pytest.approx(1.0)
    assert artifacts.tokens_in == 200 and artifacts.tokens_out == 80
    assert "RemoteSubstrate" in artifacts.diff_summary


def test_runner_failure_raises_typed_error_no_substring(tmp_path):
    driver = StubDriver(
        result=AgentTurnResult(
            done=False, error="turn exceeded 900s", error_kind="timeout"
        ).model_dump()
    )
    sub = RemoteSubstrate(driver=driver, substrate_name="claude-agent")
    _session(sub, workspace=str(tmp_path))
    with pytest.raises(RemoteTurnError) as exc:
        sub.run_turn("x")
    assert exc.value.kind == "timeout"


def test_failed_turn_still_reports_what_it_spent(tmp_path):
    """The turn burned tokens and THEN failed. Both surfaces that count money
    (the ledger row and the budget_consumed audit) are fed from
    collect_artifacts(), so a spend dropped on the failure path disappears from
    the reconciliation on BOTH — once per attempt, and the Activity retries
    without a count cap."""
    driver = StubDriver(
        result=AgentTurnResult(
            done=False, cost_usd=0.75, tokens_in=300, tokens_out=90,
            error="the CLI died mid-turn", error_kind="substrate_error",
        ).model_dump()
    )
    sub = RemoteSubstrate(driver=driver, substrate_name="claude-agent")
    _session(sub, workspace=str(tmp_path))

    with pytest.raises(RemoteTurnError) as exc:
        sub.run_turn("x")
    assert exc.value.kind == "substrate_error"

    artifacts = sub.collect_artifacts()
    assert artifacts.cost_usd == pytest.approx(0.75)
    assert artifacts.tokens_in == 300 and artifacts.tokens_out == 90


@pytest.mark.parametrize(
    "driver_kwargs, expected_error",
    [
        ({"fail_with": RuntimeError("docker exec: no such container")}, RuntimeError),
        # a payload the contract rejects (no `done`) is not evidence of a spend,
        # even carrying a cost field: nothing was validated, nothing is booked.
        ({"result": {"cost_usd": 9.99}}, ValidationError),
    ],
    ids=["exec_never_ran", "undecodable_result"],
)
def test_turn_that_fails_before_spending_reports_nothing(driver_kwargs, expected_error, tmp_path):
    sub = RemoteSubstrate(driver=StubDriver(**driver_kwargs), substrate_name="claude-agent")
    _session(sub, workspace=str(tmp_path))

    with pytest.raises(expected_error):
        sub.run_turn("x")

    artifacts = sub.collect_artifacts()
    assert artifacts.cost_usd == 0.0
    assert artifacts.tokens_in == 0 and artifacts.tokens_out == 0


def test_each_turn_is_metered_exactly_once_across_a_failure(tmp_path):
    """The other half of the fix: metering before the raise must not book the
    same turn twice."""
    driver = StubDriver(
        result=AgentTurnResult(done=True, cost_usd=0.50, tokens_in=100, tokens_out=40).model_dump()
    )
    sub = RemoteSubstrate(driver=driver, substrate_name="claude-agent")
    _session(sub, workspace=str(tmp_path))

    sub.run_turn("a")
    assert sub.collect_artifacts().cost_usd == pytest.approx(0.50)

    driver.result = AgentTurnResult(
        done=False, cost_usd=0.25, tokens_in=50, tokens_out=20,
        error="boom", error_kind="substrate_error",
    ).model_dump()
    with pytest.raises(RemoteTurnError):
        sub.run_turn("b")

    artifacts = sub.collect_artifacts()
    assert artifacts.cost_usd == pytest.approx(0.75)
    assert artifacts.tokens_in == 150 and artifacts.tokens_out == 60


def test_build_substrate_mode_is_deployment_config(monkeypatch):
    monkeypatch.delenv("DSE_SANDBOX_INPROCESS", raising=False)
    assert isinstance(_build_substrate([{"done": True}]), FakeSubstrate)

    monkeypatch.setenv("DSE_SANDBOX_INPROCESS", "0")
    remote = _build_substrate([{"done": True}], stage="coder")
    assert isinstance(remote, RemoteSubstrate)


def test_coder_turn_pipeline_with_remote_substrate(work_item_id, state_dir):
    """E2E without docker exec: the turn runs 'in the sandbox' (StubDriver
    simulates the bind mount) and ALL of the deterministic post-turn (prune,
    revert test edits, scoped commit/push, files_changed) stays in the worker,
    intact."""
    tenant_id = "tenant-a"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
    workspace_dir, _bare = _paths_for(work_item_id)

    driver = StubDriver(
        result=AgentTurnResult(done=True, cost_usd=0.01, tokens_in=10, tokens_out=5).model_dump(),
        write_into=workspace_dir,
        write_files={"src/from_sandbox.py": "VALUE = 42\n"},
    )
    remote = RemoteSubstrate(driver=driver, substrate_name="fake")

    result = asyncio.run(
        _run_coder_turn_impl(
            RunCoderTurnInput(work_item_id=work_item_id, tenant_id=tenant_id, instruction="implement it"),
            substrate=remote,
        )
    )

    assert "src/from_sandbox.py" in result.files_changed
    assert result.cost_usd == pytest.approx(0.01)
    # the request that crossed the boundary did not carry the host path
    assert driver.requests[0].input_payload["workspace_dir"] == "/workspace"

    asyncio.run(teardown_sandbox(__import__("dse_contracts").TeardownSandboxInput(
        work_item_id=work_item_id, tenant_id=tenant_id
    )))
