"""Contract for migrating the current lifecycle onto a single SandboxDriver."""
from __future__ import annotations

import pytest

from dse_contracts import Stage
from sandbox_runtime import docker_driver
from sandbox_runtime.driver import (
    DockerSandboxDriver,
    IsolatedStageExecutionUnavailable,
    SandboxDriver,
    SandboxProvisionRequest,
    StageExecutionRequest,
)


def _provision_request() -> SandboxProvisionRequest:
    return SandboxProvisionRequest(
        work_item_id="wi-driver",
        tenant_id="tenant-driver",
        branch="dse/wi-driver",
        workspace_path="/state/wi-driver/workspace",
        checkpoint_path="/state/wi-driver/checkpoint.git",
        budget={"resource_class": "small"},
        image="sandbox:test",
        user="10001:10001",
    )


def test_docker_adapter_conforms_to_sandbox_driver_protocol():
    assert isinstance(DockerSandboxDriver(), SandboxDriver)


def test_docker_adapter_preserves_existing_provision_api(monkeypatch):
    captured = {}
    expected = object()

    def fake_provision_container(**kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(docker_driver, "provision_container", fake_provision_container)
    result = DockerSandboxDriver().provision(_provision_request())

    assert result is expected
    assert captured == {
        "work_item_id": "wi-driver",
        "tenant_id": "tenant-driver",
        "branch": "dse/wi-driver",
        "workspace_host_path": "/state/wi-driver/workspace",
        "checkpoint_bare_repo_path": "/state/wi-driver/checkpoint.git",
        "budget": {"resource_class": "small"},
        "image": "sandbox:test",
        "user": "10001:10001",
    }


def test_execute_stage_is_isolated_and_fails_closed_when_unavailable():
    """Phase 1 (plan 09): execute_stage runs the agent-runner via docker exec.
    Unavailability (no docker, nonexistent container, runner missing from the
    image) is a CLEAN failure — it never degrades into running on the worker."""
    driver = DockerSandboxDriver()
    request = StageExecutionRequest(
        sandbox_id="dse-sandbox-nonexistent-xyz",
        work_item_id="wi-driver",
        tenant_id="tenant-driver",
        stage=Stage.coder,
        input_payload={"instruction": "must not run locally"},
        timeout_seconds=30,
    )

    assert driver.sandbox_id_for("wi-driver") == "dse-sandbox-wi-driver"
    with pytest.raises(IsolatedStageExecutionUnavailable, match="fallback"):
        driver.execute_stage(request)


def test_stage_contract_contains_no_host_path_or_privileged_credential_fields():
    field_names = set(StageExecutionRequest.__dataclass_fields__)
    forbidden = {
        "workspace_host_path",
        "docker_socket",
        "github_private_key",
        "litellm_master_key",
        "provider_api_key",
    }
    assert not field_names & forbidden


def test_existing_module_level_driver_api_remains_available():
    """The new adapter is additive; old callers do not break in this sprint."""
    for name in (
        "provision_container",
        "find_existing_container",
        "teardown_container",
        "kill_container",
    ):
        assert callable(getattr(docker_driver, name))

