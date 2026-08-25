"""Single contract for sandbox runtimes.

The existing Docker adapter remains the local lifecycle implementation, with no
change to the public functions of ``docker_driver``/``git_checkpoint``.
``execute_stage`` delivers the typed payload to the agent-runner INSIDE the
sandbox (`docker exec -i` here; `kubectl exec -i` in the K8s driver) and is
fail-closed on any unavailability — this contract never degrades to
``agent.run_turn()`` in the worker (invariant 2 of the spec).
"""
from __future__ import annotations

import logging

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from dse_contracts import CheckpointRef, Stage

from . import docker_driver, git_checkpoint


@dataclass(frozen=True)
class SandboxProvisionRequest:
    work_item_id: str
    tenant_id: str
    branch: str
    workspace_path: str
    checkpoint_path: str
    budget: dict[str, Any] = field(default_factory=dict)
    image: str = docker_driver.DEFAULT_SANDBOX_IMAGE
    user: str = docker_driver.DEFAULT_NONROOT_USER
    # The task's target repo. On the K8s driver the workspace lives in a Pod
    # volume (not visible to the worker), so the clone happens INSIDE the Pod,
    # at bootstrap, through the egress-proxy. None → empty workspace.
    repo: str | None = None
    base_branch: str = "main"
    #: Tema 1 — os serviços de apoio que o REPO declara (`services` do
    #: manifesto, já validados pelo parser real no probe). Forma normalizada
    #: `nome -> {image, port, env, ready, user, writable}`. None/{} = nenhum,
    #: o comportamento de sempre.
    services: dict[str, Any] | None = None
    #: O comando de migração+seed DO REPO, executado após o clone com os
    #: serviços prontos. A plataforma não entende seed nenhuma — só executa.
    prepare: list[str] | None = None


@dataclass(frozen=True)
class StageExecutionRequest:
    """Internal payload the worker sends to the isolated agent-runner.

    Contains no host paths and no privileged credentials. ``input_payload`` must
    already satisfy the stage-specific contract before it reaches the driver.
    """

    sandbox_id: str
    work_item_id: str
    tenant_id: str
    stage: Stage
    input_payload: dict[str, Any]
    timeout_seconds: float


@dataclass(frozen=True)
class StageExecutionResult:
    stage: Stage
    output_payload: dict[str, Any]
    exit_code: int
    duration_seconds: float


@dataclass(frozen=True)
class SandboxCheckpointRequest:
    work_item_id: str
    workspace_path: str
    branch: str
    phase: str


@dataclass(frozen=True)
class SandboxRebuildRequest:
    provision: SandboxProvisionRequest
    checkpoint_ref: CheckpointRef


@dataclass(frozen=True)
class SandboxRebuildResult:
    sandbox: docker_driver.ProvisionedSandbox
    recovered_sha: str


class IsolatedStageExecutionUnavailable(RuntimeError):
    """The driver has no isolated agent-runner; never execute locally."""


@runtime_checkable
class SandboxDriver(Protocol):
    """Lifecycle and execution shared by the Docker/Kubernetes drivers."""

    @property
    def workspace_is_host_visible(self) -> bool:
        """True when the worker can see the workspace through the filesystem
        (Docker bind mount) and can run git/hygiene locally; False when the
        workspace lives only inside the sandbox (Pod volume) and the post-turn
        has to run via `execute_op` (post_turn)."""
        ...

    def sandbox_id_for(self, work_item_id: str) -> str:
        """Stable identity of this work item's sandbox in the target runtime
        (container name on Docker, Pod name on K8s)."""
        ...

    def provision(self, request: SandboxProvisionRequest) -> docker_driver.ProvisionedSandbox:
        ...

    def execute_stage(self, request: StageExecutionRequest) -> StageExecutionResult:
        ...

    def execute_op(
        self, sandbox_id: str, op: str, payload: dict[str, Any], *, timeout_seconds: float = 180.0
    ) -> dict[str, Any]:
        """Run a runner lifecycle op (bootstrap/checkpoint/post_turn) INSIDE the
        sandbox and return the result JSON."""
        ...

    def checkpoint(self, request: SandboxCheckpointRequest) -> CheckpointRef:
        ...

    def rebuild(self, request: SandboxRebuildRequest) -> SandboxRebuildResult:
        ...

    def teardown(self, sandbox_id: str) -> float:
        ...


class DockerSandboxDriver:
    """Compatible adapter over the current Docker lifecycle.

    ``execute_stage`` (Phase 1, plan 09) runs the agent-runner INSIDE the
    sandbox container via ``docker exec -i`` — symmetric to the K8s driver's
    ``kubectl exec -i``. It requires the sandbox image to have the
    ``agent_runner`` module installed (``make agent-runner-image``); any
    unavailability (docker missing, container dead, runner missing from the
    image) is a clean failure — NEVER a fallback to executing in the worker.
    """

    @property
    def workspace_is_host_visible(self) -> bool:
        return True  # bind mount: the worker runs git/hygiene from the host

    def sandbox_id_for(self, work_item_id: str) -> str:
        return docker_driver.container_name_for(work_item_id)

    def _warn_services_unsupported(self, request: SandboxProvisionRequest) -> None:
        """O driver Docker local (dev) não implementa sidecars — e não pode
        fingir que implementa: os testes do repo rodariam sem o banco que o
        manifesto declara e reprovariam acusando o diff. Ignora COM aviso
        nomeado; nunca em silêncio."""
        if request.services:
            logging.getLogger("sandbox_runtime.driver").warning(
                "sandbox %s declares services %s but the local Docker driver "
                "does not run sidecars — integration tests that need them will "
                "fail here; use the K8s runtime",
                request.work_item_id, sorted(request.services),
            )

    def provision(self, request: SandboxProvisionRequest) -> docker_driver.ProvisionedSandbox:
        self._warn_services_unsupported(request)
        return docker_driver.provision_container(
            work_item_id=request.work_item_id,
            tenant_id=request.tenant_id,
            branch=request.branch,
            workspace_host_path=request.workspace_path,
            checkpoint_bare_repo_path=request.checkpoint_path,
            budget=request.budget,
            image=request.image,
            user=request.user,
        )

    def _exec_runner(
        self, sandbox_id: str, runner_args: list[str], payload: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]:
        docker_bin = shutil.which("docker")
        if docker_bin is None:
            raise IsolatedStageExecutionUnavailable(
                "docker CLI not found — isolated execution unavailable; "
                "local execution is forbidden as a fallback (invariant 2)"
            )
        try:
            proc = subprocess.run(
                [docker_bin, "exec", "-i", sandbox_id, "python", "-m", "agent_runner", *runner_args],
                input=json.dumps({"input": payload}),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise IsolatedStageExecutionUnavailable(
                f"agent-runner exceeded {timeout_seconds:.0f}s in the exec "
                f"(args={runner_args}, sandbox={sandbox_id})"
            ) from exc
        except FileNotFoundError as exc:  # docker vanished between the which and the exec
            raise IsolatedStageExecutionUnavailable(
                "docker CLI unavailable in the exec — no local fallback"
            ) from exc
        if proc.returncode != 0:
            raise IsolatedStageExecutionUnavailable(
                f"docker exec agent_runner failed (exit={proc.returncode}, "
                f"sandbox={sandbox_id}): {proc.stderr.strip()[:400]} "
                "— no local fallback"
            )
        try:
            # last JSON line: the runner writes exactly one result
            return json.loads((proc.stdout or "").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise IsolatedStageExecutionUnavailable(
                f"agent-runner returned non-JSON stdout (sandbox={sandbox_id}): "
                f"{(proc.stdout or '')[:200]!r}"
            ) from exc

    def execute_stage(self, request: StageExecutionRequest) -> StageExecutionResult:
        started = time.time()
        out = self._exec_runner(
            request.sandbox_id,
            ["--stage", request.stage.value],
            request.input_payload,
            request.timeout_seconds,
        )
        return StageExecutionResult(
            stage=request.stage,
            output_payload=out,
            exit_code=0,
            duration_seconds=time.time() - started,
        )

    def execute_op(
        self, sandbox_id: str, op: str, payload: dict[str, Any], *, timeout_seconds: float = 180.0
    ) -> dict[str, Any]:
        return self._exec_runner(sandbox_id, ["--op", op], payload, timeout_seconds)

    def checkpoint(self, request: SandboxCheckpointRequest) -> CheckpointRef:
        return git_checkpoint.checkpoint(
            request.work_item_id,
            request.workspace_path,
            request.branch,
            request.phase,
        )

    def rebuild(self, request: SandboxRebuildRequest) -> SandboxRebuildResult:
        provision = request.provision
        rebuilt_workspace = provision.workspace_path
        recovered_sha = git_checkpoint.rebuild_from_checkpoint(
            rebuilt_workspace,
            provision.checkpoint_path,
            provision.branch,
            request.checkpoint_ref,
        )
        sandbox = self.provision(provision)
        return SandboxRebuildResult(sandbox=sandbox, recovered_sha=recovered_sha)

    def teardown(self, sandbox_id: str) -> float:
        return docker_driver.teardown_container(sandbox_id)


def select_sandbox_driver() -> SandboxDriver:
    """plan 08 §G — picks the driver by profile (`DSE_SANDBOX_DRIVER`):
    'docker' (default, local dev) or 'k8s' (pilot — isolated Pod under a
    RuntimeClass). The K8s driver is imported lazily to avoid an import cycle.
    On the pilot profile, combine it with `DSE_SANDBOX_INPROCESS=0` (the
    fail-closed check already refuses local execution)."""
    import os

    choice = os.environ.get("DSE_SANDBOX_DRIVER", "docker").strip().lower()
    if choice in ("k8s", "kubernetes"):
        from .k8s_driver import KubernetesSandboxDriver
        return KubernetesSandboxDriver()
    return DockerSandboxDriver()


DEFAULT_SANDBOX_DRIVER: SandboxDriver = select_sandbox_driver()

