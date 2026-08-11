"""The runner's post_turn op + the Coder Activity in K8s mode (Phase 1, F1.7).

Two layers of proof, without a cluster:
  1. The op itself (real git in tmp dirs): pruning disposables, restoring
     lockfile churn and the scoped commit/push — the SAME sequence as the
     worker, now executable inside the Pod. (O revert de edição de teste saiu
     em 2026-08-10 com o reauthor; a fronteira nova está em
     `test_no_test_edit_is_ever_reverted.py`.)
  2. The whole `_run_coder_turn_impl` in pod-git mode: a StubDriver with
     `workspace_is_host_visible=False` routes execute_op/execute_stage to the
     REAL runner functions against a directory that simulates the Pod volume —
     the worker never touches that filesystem with its own git.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest
from dse_contracts import (
    AgentTurnResult,
    CheckpointOpRequest,
    PostTurnRequest,
    RunCoderTurnInput,
    WorkspaceBootstrapRequest,
)

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.gitops import bootstrap_workspace, checkpoint_workspace  # noqa: E402
from agent_runner.postturn import run_post_turn  # noqa: E402

from sandbox_runtime.activities import _run_coder_turn_impl  # noqa: E402
from sandbox_runtime.driver import StageExecutionResult  # noqa: E402
from sandbox_runtime.remote_substrate import RemoteSubstrate  # noqa: E402


def _bootstrap(tmp_path, wi="wi-pt1"):
    req = WorkspaceBootstrapRequest(
        work_item_id=wi,
        branch=f"dse/{wi}",
        workspace_dir=str(tmp_path / "workspace"),
        checkpoint_path=str(tmp_path / "checkpoint.git"),
    )
    res = bootstrap_workspace(req)
    assert not res.failed
    return req, res


def test_post_turn_full_hygiene_and_scoped_push(tmp_path):
    wi = "wi-pt1"
    req, boot = _bootstrap(tmp_path, wi)
    ws = tmp_path / "workspace"

    # simulates the agent turn: legitimate source + junk + churn + dois testes
    # novos, um em convenção de cliente e outro com o antigo marcador `-dse`.
    # Desde a remoção do revert (2026-08-10) os DOIS ficam: o marcador deixou
    # de significar posse, e apagar arquivo novo de teste seria destruir
    # trabalho do único ator que estava trabalhando.
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("X = 1\n")
    (ws / "BUG_FIX_REPORT.md").write_text("spontaneous report\n")
    (ws / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    (ws / "tests").mkdir()
    (ws / "tests" / "test_smuggled.py").write_text("def test_x(): pass\n")
    (ws / "tests" / "test_forged-dse.py").write_text("def test_f(): pass\n")

    post = run_post_turn(
        PostTurnRequest(
            work_item_id=wi,
            branch=req.branch,
            turn_start_sha=boot.sha,
            commit_message=f"coder({wi}): implement",
            expected_files=["src/app.py"],
            workspace_dir=str(ws),
        )
    )
    assert not post.failed
    assert post.sha != boot.sha
    assert post.files_changed == [
        "src/app.py", "tests/test_forged-dse.py", "tests/test_smuggled.py",
    ]
    assert post.pruned == ["BUG_FIX_REPORT.md"]
    assert post.restored_lockfiles == ["package-lock.json"]
    assert not (ws / "BUG_FIX_REPORT.md").exists()
    assert (ws / "tests" / "test_smuggled.py").exists(), "convenção de cliente fica"
    assert (ws / "tests" / "test_forged-dse.py").exists(), (
        "o marcador -dse não significa mais posse; o arquivo fica e aparece na PR"
    )

    # the push reached the checkpoint (fixed refspec)
    ck = checkpoint_workspace(
        CheckpointOpRequest(work_item_id=wi, branch=req.branch, phase="verify", workspace_dir=str(ws))
    )
    assert ck.sha == post.sha


class PodGitStubDriver:
    """Simulates the K8s runtime: the 'Pod volume' is a tmp dir the worker does
    NOT drive with its own git — every op goes through execute_op/execute_stage
    and runs the REAL runner functions against it."""

    def __init__(self, pod_workspace: str, pod_checkpoint: str):
        self.pod_workspace = pod_workspace
        self.pod_checkpoint = pod_checkpoint
        self.ops: list[str] = []

    @property
    def supports_isolated_stage_execution(self) -> bool:
        return True

    @property
    def workspace_is_host_visible(self) -> bool:
        return False

    def sandbox_id_for(self, work_item_id: str) -> str:
        return f"pod-{work_item_id}"

    def execute_op(self, sandbox_id, op, payload: dict[str, Any], *, timeout_seconds=180.0):
        self.ops.append(op)
        payload = dict(payload, workspace_dir=self.pod_workspace)
        if op == "checkpoint":
            return checkpoint_workspace(CheckpointOpRequest.model_validate(payload)).model_dump()
        if op == "post_turn":
            return run_post_turn(PostTurnRequest.model_validate(payload)).model_dump()
        raise AssertionError(f"unexpected op: {op}")

    def execute_stage(self, request):
        self.ops.append(f"stage:{request.stage.value}")
        # fake turn "inside the pod": writes to the simulated volume
        target = os.path.join(self.pod_workspace, "src", "from_pod.py")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as fh:
            fh.write("IN_POD = True\n")
        return StageExecutionResult(
            stage=request.stage,
            output_payload=AgentTurnResult(done=True, cost_usd=0.02).model_dump(),
            exit_code=0,
            duration_seconds=0.01,
        )


def test_coder_activity_full_pipeline_in_pod_git_mode(tmp_path, work_item_id, state_dir):
    branch = f"dse/{work_item_id}"
    pod_ws = tmp_path / "pod-volume" / "workspace"
    pod_ck = tmp_path / "pod-volume" / "checkpoint.git"
    boot = bootstrap_workspace(
        WorkspaceBootstrapRequest(
            work_item_id=work_item_id, branch=branch,
            workspace_dir=str(pod_ws), checkpoint_path=str(pod_ck),
        )
    )
    assert not boot.failed

    driver = PodGitStubDriver(str(pod_ws), str(pod_ck))
    remote = RemoteSubstrate(driver=driver, substrate_name="fake")

    result = asyncio.run(
        _run_coder_turn_impl(
            RunCoderTurnInput(
                work_item_id=work_item_id, tenant_id="tenant-a", instruction="implement it",
            ),
            substrate=remote,
        )
    )

    assert result.files_changed == ["src/from_pod.py"]
    assert result.cost_usd == pytest.approx(0.02)
    # pod-git mode sequence: initial sha via checkpoint, turn, post-turn
    assert driver.ops[0] == "checkpoint"
    assert "stage:coder" in driver.ops
    assert driver.ops[-1] == "post_turn"
