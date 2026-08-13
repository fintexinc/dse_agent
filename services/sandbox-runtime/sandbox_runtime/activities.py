"""Temporal Activities for the sandbox lifecycle (WSC-E1-T3) + Coder session
(WSC-E3-T2). Exact names from `dse_contracts.activities` — imported by WS-B's
single worker (`services/orchestrator/worker.py`).

Defensive import: this module itself must never fail to import merely because a
heavy dependency is missing from the importer's venv — but since
`docker`/`temporalio`/`dse_contracts`/`dse_audit` are DECLARED dependencies of
this package (pyproject.toml), we import them directly here as usual. Anyone
importing this module without those dependencies installed must do so in THEIR
OWN try/except (the integrator's responsibility, see the docstring of
`sandbox_runtime/__init__.py`).

State between Activity calls: Temporal does not guarantee that a workflow's
Activity always runs on the same worker/process — which is why this module NEVER
keeps state in process memory across calls. All state lives:
  - in Docker (the sandbox container, found via the `dse.work_item_id` label);
  - on the filesystem, at deterministic paths derived from `work_item_id`
    (`_paths_for`) — the working workspace + the checkpoint bare repo.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger("sandbox_runtime.activities")

# How much of a failed suite's output travels back to the Coder. Enough
# for a stack trace and the failing assertions; bounded because it ends up
# in a model prompt and in Temporal's history.
_FAILURE_OUTPUT_CHARS = 4000

# How much of a failing suite's output is kept ON THE LEDGER. The full text
# already travels back to the Coder; what was missing is the answer to "why did
# this work item fail?" surviving anywhere durable. It lived only in the
# orchestrator pod's log, truncated to 300 chars and rotated away — so the one
# question the audit ledger exists to answer had to be chased through
# `kubectl logs`. Smaller than the Coder's copy on purpose: this is for a human
# reading the trail, not for the model.
_FAILURE_OUTPUT_AUDIT_CHARS = 1200

from pydantic import BaseModel, Field
from temporalio import activity

from dse_audit import emit as audit_emit
from dse_contracts.paths import is_test_path
from dse_contracts import (
    ACTIVITY_CHECKPOINT_SANDBOX,
    ACTIVITY_PROVISION_SANDBOX,
    ACTIVITY_REBUILD_SANDBOX,
    ACTIVITY_RUN_CODER_TURN,
    ACTIVITY_RUN_L2_REVIEW,
    ACTIVITY_RUN_PLANNER_TURN,
    ACTIVITY_RUN_TESTER_TURN,
    ACTIVITY_TEARDOWN_SANDBOX,
    CheckpointRef,
    CoderTurnResult,
    GateStatus,
    GatewayCallHeaders,
    L2Verdict,
    PlanArtifact,
    SandboxHandle,
    Stage,
)

from . import docker_driver, git_checkpoint, leases_store, metrics
from .activity_heartbeat import run_sync_with_heartbeat
from .driver import (
    DEFAULT_SANDBOX_DRIVER,
    SandboxCheckpointRequest,
    SandboxProvisionRequest,
    SandboxRebuildRequest,
    select_sandbox_driver,
)
from .model_gateway_client import mint_virtual_key
from .remote_substrate import RemoteSubstrate
from .retrieval import RetrievalService
from .runtime_profile import (
    RuntimeProfile,
    reject_local_agent_execution,
    sandbox_inprocess_enabled,
    validate_runtime_profile,
    validate_runtime_startup,
)
from .scoped_git import GitScopeViolation, ScopedGitSession
from .skill_files import (
    materialize_skills,
    workspace_skills_note,
    workspace_skills_note_in_pod,
)
from .sessions import (
    FreshReviewerSession,
    PlannerContext,
    ReviewerContext,
    ScriptedAgentSession,
    classify_risk_class,
    hydrate_planner_context,
)
from . import workspace_hygiene
from .substrate import SUBSTRATE_ENV_VAR, AgentSubstrate, substrate_from_env
from .toolsets import PlannerToolset, TesterToolset

_STATE_DIR = os.environ.get("DSE_SANDBOX_STATE_DIR", "/tmp/dse-sandboxes")


def _paths_for(work_item_id: str) -> tuple[str, str]:
    """Deterministic paths derived solely from the work_item_id — lets any
    worker, on any call, find the same workspace/bare repo without relying on
    in-memory state (see the module docstring)."""
    root = Path(_STATE_DIR) / work_item_id
    workspace_dir = str(root / "workspace")
    bare_repo_path = str(root / "checkpoint.git")
    return workspace_dir, bare_repo_path


def _default_branch(work_item_id: str) -> str:
    return f"dse/{work_item_id}"


# ---------------------------------------------------------------------------
# provision_sandbox
# ---------------------------------------------------------------------------
class ProvisionSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    branch: str | None = None
    base_branch: str = "main"
    repo: str | None = None  # S4: target repo (e.g. "andre2654/fintex-wallet") to clone
    budget: dict[str, Any] = Field(default_factory=dict)
    image: str | None = None


@activity.defn(name=ACTIVITY_PROVISION_SANDBOX)
async def provision_sandbox(inp: ProvisionSandboxInput) -> SandboxHandle:
    profile = validate_runtime_startup()
    branch = inp.branch or _default_branch(inp.work_item_id)
    driver = select_sandbox_driver()
    workspace_dir, bare_repo_path = _paths_for(inp.work_item_id)

    if not driver.workspace_is_host_visible:
        # === K8s runtime: the workspace lives in a Pod volume (not visible to
        # the worker), so NOTHING is cloned/init'd on the host — the driver
        # brings up the Pod and the bootstrap clones the real repo INSIDE it,
        # through the egress-proxy; the repo token never enters the control
        # plane. Skills are materialized INTO the Pod after the bootstrap has
        # cloned (below) — the host writer used by the Docker path cannot reach
        # this workspace. ===
        provisioned = driver.provision(
            SandboxProvisionRequest(
                work_item_id=inp.work_item_id,
                tenant_id=inp.tenant_id,
                branch=branch,
                workspace_path=workspace_dir,
                checkpoint_path=bare_repo_path,
                budget=inp.budget or {},
                repo=inp.repo,
                base_branch=inp.base_branch,
            )
        )
        # Guidance into the Pod. `provision` returns only after `_bootstrap`, so
        # the repo is cloned and `.git` exists — which the repo-sovereignty rule
        # and the local exclude both depend on.
        #
        # Best-effort by construction: a skill that fails to land must never
        # take down a provision that otherwise succeeded. The audit says which
        # way it went, because the failure mode this replaces was silence — the
        # Planner reading 21 skills while the Coder worked with none, and
        # nothing anywhere recording the difference.
        try:
            from .skill_files import materialize_skills_in_pod as _materialize_pod
            from .skill_registry import read_approved_skills as _read_skills
            _served = _read_skills(inp.tenant_id, repo=inp.repo)
            if not _served:
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="skills_resolved_empty",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"repo": inp.repo, "stage": "provision", "runtime": "k8s"},
                )
            else:
                _mat = _materialize_pod(
                    _served,
                    run=lambda argv, stdin: driver.run_in_pod(
                        provisioned.container_id, argv, stdin
                    ),
                )
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="skills_materialized" if _mat else "skills_materialization_skipped",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"skills": _mat, "repo": inp.repo, "runtime": "k8s",
                             "served": len(_served)},
                )
        except Exception as exc:  # noqa: BLE001 — guidance never fails a provision
            audit_emit(
                actor="system:sandbox-runtime",
                action="skills_materialization_skipped",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"error": f"{type(exc).__name__}: {str(exc)[:200]}",
                         "repo": inp.repo, "runtime": "k8s"},
            )
    else:
        is_new_checkpoint_repo = not Path(bare_repo_path).exists()
        if is_new_checkpoint_repo:
            git_checkpoint.provision_checkpoint_repo(bare_repo_path, branch)
        if not Path(workspace_dir).exists():
            # S4 (Phase 5): if the task has a target repo (e.g. github.com/andre2654/
            # fintex-wallet), CLONE the real code (with a token minted in the control
            # plane and scrubbed from the config) — the Coder works on the real repo.
            # With no repo/token (tests), fall back to the original empty-workspace
            # mechanics.
            cloned = False
            if inp.repo:
                from . import repo_clone
                token = repo_clone.mint_installation_token()
                cloned = repo_clone.clone_repo_into(
                    workspace_dir=workspace_dir, repo=inp.repo,
                    base_branch=inp.base_branch, task_branch=branch,
                    bare_repo_path=bare_repo_path, token=token,
                )
                if cloned and not repo_clone.token_absent_from_config(workspace_dir):
                    raise RuntimeError("SECURITY: token leaked into the workspace git config")
                if not cloned and profile is RuntimeProfile.production:
                    validate_runtime_profile(
                        local_fallback=(
                            f"clone of {inp.repo!r} failed/no credential and would fall back to an empty workspace"
                        )
                    )
            if not cloned:
                git_checkpoint.init_task_workspace(workspace_dir, bare_repo_path, branch, inp.base_branch)

        # Skills ticked for this repo (console → skill_registry.repo_scope, 0029)
        # are materialized HERE — after the clone, when the workspace is
        # guaranteed to be a git repo. Guidance is best-effort at provision time
        # (the Planner still fails cleanly if the registry goes down — the
        # mandatory read is its own); any skip is audited (P8).
        try:
            from .skill_files import materialize_skills as _materialize
            from .skill_registry import read_approved_skills as _read_skills
            _served = _read_skills(inp.tenant_id, repo=inp.repo)
            if not _served:
                # Keyed off the REGISTRY READ, not off the materialize result:
                # an empty _mat with a non-empty _served is the legitimate
                # "the repo already commits these skills" case, and the
                # "workspace has no .git yet" no-op. Only an empty READ means
                # the agent is running with no guidance at all — which is the
                # state this whole tenant was silently in, because an empty
                # list is a perfectly valid return and nothing said so.
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="skills_resolved_empty",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"repo": inp.repo, "stage": "provision"},
                )
            _mat = _materialize(workspace_dir, _served)
            if _mat:
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="skills_materialized",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"skills": _mat, "repo": inp.repo},
                )
        except Exception as exc:  # noqa: BLE001 — guidance never brings down the provision
            audit_emit(
                actor="system:sandbox-runtime",
                action="skills_materialization_skipped",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"reason": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )

        provisioned = docker_driver.provision_container(
            work_item_id=inp.work_item_id,
            tenant_id=inp.tenant_id,
            branch=branch,
            workspace_host_path=workspace_dir,
            checkpoint_bare_repo_path=bare_repo_path,
            budget=inp.budget,
            image=inp.image or docker_driver.DEFAULT_SANDBOX_IMAGE,
        )

    audit_emit(
        actor="system:sandbox-runtime",
        action="sandbox_provisioned",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "container_id": provisioned.container_id,
            "reused_existing": not provisioned.created_new,
            "resource_class": provisioned.resource_caps.resource_class,
            "branch": branch,
        },
    )
    leases_store.record_lifecycle_event(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        container_id=provisioned.container_id,
        branch=branch,
        resource_class=provisioned.resource_caps.resource_class,
        status="provisioned",
    )

    return SandboxHandle(
        sandbox_id=provisioned.container_name,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        branch=branch,
        container_id=provisioned.container_id,
    )


# ---------------------------------------------------------------------------
# checkpoint_sandbox
# ---------------------------------------------------------------------------
class CheckpointSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    branch: str | None = None
    phase: str = "manual"


@activity.defn(name=ACTIVITY_CHECKPOINT_SANDBOX)
async def checkpoint_sandbox(inp: CheckpointSandboxInput) -> CheckpointRef:
    driver = select_sandbox_driver()
    branch = inp.branch or _default_branch(inp.work_item_id)
    workspace_dir, _bare_repo_path = _paths_for(inp.work_item_id)
    if not driver.workspace_is_host_visible:
        ref = driver.checkpoint(
            SandboxCheckpointRequest(
                work_item_id=inp.work_item_id, workspace_path=workspace_dir,
                branch=branch, phase=inp.phase,
            )
        )
    else:
        ref = git_checkpoint.checkpoint(inp.work_item_id, workspace_dir, branch, inp.phase)

    audit_emit(
        actor="system:sandbox-runtime",
        action="sandbox_checkpointed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={"git_ref": ref.git_ref, "phase": ref.phase},
    )
    if not driver.workspace_is_host_visible:
        container_id = driver.sandbox_id_for(inp.work_item_id)
        resource_class = "small"
    else:
        existing = docker_driver.find_existing_container(inp.work_item_id)
        container_id = existing.id if existing else None
        resource_class = existing.labels.get("dse.resource_class", "small") if existing else "small"
    leases_store.record_lifecycle_event(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        container_id=container_id,
        branch=branch,
        resource_class=resource_class,
        status="checkpointed",
    )
    return ref


# ---------------------------------------------------------------------------
# rebuild_sandbox
# ---------------------------------------------------------------------------
# Imported, not redefined. There WAS a local copy of this model here, and it had
# already drifted: it lacked `base_sha`, and when `repo`/`base_branch` were added
# to the contract the activity kept receiving the old shape and raised
# `AttributeError: 'RebuildSandboxInput' object has no attribute 'repo'`. A
# duplicated cross-service contract that only one side updates is a trap that
# fires at runtime, and it fired.
#
# `ProvisionSandboxInput`, `CheckpointSandboxInput` and `TeardownSandboxInput`
# below are duplicated the same way. They are left alone here deliberately —
# converting them is a separate change, not something to do mid-incident — but
# they carry the same hazard.
from dse_contracts.activities import RebuildSandboxInput  # noqa: E402


@activity.defn(name=ACTIVITY_REBUILD_SANDBOX)
async def rebuild_sandbox(inp: RebuildSandboxInput) -> SandboxHandle:
    validate_runtime_startup()
    driver = select_sandbox_driver()
    branch = inp.branch or _default_branch(inp.work_item_id)
    old_workspace_dir, bare_repo_path = _paths_for(inp.work_item_id)

    if not driver.workspace_is_host_visible:
        # === K8s runtime: rebuild = a fresh Pod mounting the same checkpoint
        # volume; the bootstrap recovers the branch from the checkpoint. ===
        #
        # `repo`/`base_branch` are forwarded so the bootstrap has a FALLBACK
        # when the checkpoint is gone. It usually is: the checkpoint volume is
        # an emptyDir unless a PVC is configured, and the chart ships
        # `checkpointPvc.enabled: false` — so a rebuild gets a fresh, empty
        # volume, `_checkpoint_has_branch` is False, and without a repo to fall
        # back on the bootstrap took its LAST branch and initialised an EMPTY
        # git repo. The Coder then ran against a workspace with none of the
        # customer's code in it, and every retry reproduced that state exactly:
        # seen as fourteen identical failures on the VPS.
        #
        # Re-cloning is the honest recovery. It loses the turn's uncommitted
        # work — which the emptyDir already lost — instead of silently
        # continuing against an empty tree.
        rebuild_result = driver.rebuild(
            SandboxRebuildRequest(
                provision=SandboxProvisionRequest(
                    work_item_id=inp.work_item_id,
                    tenant_id=inp.tenant_id,
                    branch=branch,
                    repo=inp.repo,
                    base_branch=inp.base_branch,
                    workspace_path=old_workspace_dir,
                    checkpoint_path=bare_repo_path,
                    budget=inp.budget or {},
                ),
                checkpoint_ref=inp.checkpoint_ref,
            )
        )
        provisioned = rebuild_result.sandbox
        recovered_sha = rebuild_result.recovered_sha
    else:
        # The old container may be dead (chaos) — remove it if it still exists
        # before recreating, so it does not collide with the new one's
        # name/labels.
        existing = docker_driver.find_existing_container(inp.work_item_id)
        if existing is not None:
            try:
                existing.remove(force=True)
            except Exception:  # noqa: BLE001 - the daemon may already have removed it
                pass

        # Fresh workspace (simulates losing the old container — it does not reuse
        # the previous working directory, only the checkpoint bare repo, which is
        # the durable source of truth).
        rebuilt_workspace_dir = old_workspace_dir + "-rebuilt"
        recovered_sha = git_checkpoint.rebuild_from_checkpoint(
            rebuilt_workspace_dir, bare_repo_path, branch, inp.checkpoint_ref
        )

        provisioned = docker_driver.provision_container(
            work_item_id=inp.work_item_id,
            tenant_id=inp.tenant_id,
            branch=branch,
            workspace_host_path=rebuilt_workspace_dir,
            checkpoint_bare_repo_path=bare_repo_path,
            budget=inp.budget,
            image=inp.image or docker_driver.DEFAULT_SANDBOX_IMAGE,
        )

    audit_emit(
        actor="system:sandbox-runtime",
        action="sandbox_rebuilt",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "container_id": provisioned.container_id,
            "checkpoint_git_ref": inp.checkpoint_ref.git_ref,
            "recovered_sha": recovered_sha,
        },
    )
    leases_store.record_lifecycle_event(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        container_id=provisioned.container_id,
        branch=branch,
        resource_class=provisioned.resource_caps.resource_class,
        status="rebuilt",
    )

    return SandboxHandle(
        sandbox_id=provisioned.container_name,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        branch=branch,
        container_id=provisioned.container_id,
    )


# ---------------------------------------------------------------------------
# teardown_sandbox
# ---------------------------------------------------------------------------
class TeardownSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    stage: str = "coder"


@activity.defn(name=ACTIVITY_TEARDOWN_SANDBOX)
async def teardown_sandbox(inp: TeardownSandboxInput) -> None:
    driver = select_sandbox_driver()
    resource_class = "small"
    runtime_minutes = 0.0
    if not driver.workspace_is_host_visible:
        # K8s runtime: delete the Pod. Pod lifetime is not tracked yet (the
        # metric stays 0.0 — fast-follow); the driver is fail-closed without a
        # cluster. container_id = the Pod name (Docker's `existing` does not
        # exist here).
        driver.teardown(driver.sandbox_id_for(inp.work_item_id))
        container_id = driver.sandbox_id_for(inp.work_item_id)
    else:
        existing = docker_driver.find_existing_container(inp.work_item_id)
        container_id = existing.id if existing else None
        if existing is not None:
            resource_class = existing.labels.get("dse.resource_class", "small")
            runtime_minutes = docker_driver.teardown_container(existing.id)

    metrics.record_sandbox_runtime_minutes(
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        stage=inp.stage,
        resource_class=resource_class,
        minutes=runtime_minutes,
    )
    audit_emit(
        actor="system:sandbox-runtime",
        action="sandbox_torn_down",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={"runtime_minutes": round(runtime_minutes, 4), "resource_class": resource_class},
    )
    leases_store.record_lifecycle_event(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        container_id=container_id,
        branch=_default_branch(inp.work_item_id),
        resource_class=resource_class,
        status="torn_down",
    )


# ---------------------------------------------------------------------------
# run_coder_turn
# ---------------------------------------------------------------------------
# CANONICAL contract (anti-shadow — the same finding as in L1: the local model
# silently dropped fields the workflow sends, e.g. model_override and the new
# expected_files). Never redefine contract models locally.
from dse_contracts.activities import RunCoderTurnInput  # noqa: E402


def _build_substrate(script: list[dict[str, Any]] | None, *, stage: str = "coder") -> AgentSubstrate:
    """Substrate factory. Phase 3 (WSC-E3-T6): the choice is PER-DEPLOYMENT
    CONFIG — `DSE_CODER_SUBSTRATE` in {fake|openhands|claude-agent}, default
    `fake` (no gateway/SDK dependency has to be up for the tests). Swapping
    substrates never changes workflow code: WS-B keeps calling `run_coder_turn`
    by name, and this factory resolves the adapter behind the same
    `AgentSubstrate` interface.

    Phase 1 (plan 09): with `DSE_SANDBOX_INPROCESS=0` (and ALWAYS in production)
    the substrate becomes `RemoteSubstrate` — same substrate name, but the SDK
    executes INSIDE the sandbox via `SandboxDriver.execute_stage`; the worker
    only dispatches the typed contract (invariant 2 of the spec)."""
    if sandbox_inprocess_enabled():
        return substrate_from_env(script=script)
    return RemoteSubstrate(
        driver=select_sandbox_driver(),
        substrate_name=os.environ.get(SUBSTRATE_ENV_VAR, "fake").strip().lower(),
        stage=stage,
        fake_script=script,
    )


# Post-turn hygiene extracted to workspace_hygiene.py (Phase 1, plan 09): the
# same logic runs in the worker (Docker) and INSIDE the runner (K8s, --op
# post_turn) — single source of truth; the aliases preserve call sites/tests.
_prune_disposable_artifacts = workspace_hygiene.prune_disposable_artifacts


# Key the spend of a FAILED turn travels under, inside the `details` of the
# ApplicationError raised to the workflow. A failed Activity produces no result,
# and `cost_usd` ON THE RESULT is the only way the workflow's per-work-item
# ceiling ever learns about money (`_consume_cost`) — so `details` is the one
# channel left. Nested under a single key so a reader can identify THIS payload
# among any other details a raiser might add.
FAILED_TURN_SPEND_KEY = "dse_failed_turn_spend"


def _is_scripted_substrate(agent: Any) -> bool:
    """Whether this turn spent PLAY money. The discriminator is the SUBSTRATE
    NAME, not the cost: FakeSubstrate reports $0.01 per step, so a cost-based
    guard would start writing ledger rows from existing scripted tests into a
    table nothing is allowed to delete from."""
    return getattr(agent, "substrate_name", "") == "fake" or type(agent).__name__ == "FakeSubstrate"


def _meter_coder_spend(
    inp: RunCoderTurnInput, artifacts: CoderTurnResult, *, agent: Any, outcome: str
) -> int | None:
    """Write this turn's spend into model_call_ledger; return the row id, or
    None when no row was written.

    Every other stage lands there through the gateway client, but the coder
    drives the bundled CLI with the gateway configured by env, so its cost only
    ever existed on this result — which is why the console's rollup, computed
    from the ledger alone, reported $0.50 against $27.91 of real spend.

    NEVER raises. On the success path the commit and push already happened, so
    raising would burn a Temporal retry and re-spend real money to fix a
    bookkeeping miss; on the failure path it would replace the turn's real error
    with a Postgres one. Fail loud and fall back to the legacy audit-derived
    path.
    """
    if not artifacts.cost_usd or _is_scripted_substrate(agent):
        return None
    try:
        from model_gateway_client.ledger import record_call

        return record_call(
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            stage=inp.stage,
            task_class=inp.task_class,
            model=os.environ.get("DSE_CODER_MODEL", "anthropic/claude"),
            cost_usd=artifacts.cost_usd,
            tokens_in=artifacts.tokens_in,
            tokens_out=artifacts.tokens_out,
        )
    except Exception as exc:  # noqa: BLE001 — never re-raise here
        logger.error(
            "coder cost ledger write FAILED (%s: %s); $%.4f falls back to the audit path",
            type(exc).__name__, str(exc)[:200], artifacts.cost_usd,
        )
        with contextlib.suppress(Exception):
            audit_emit(
                actor="system:sandbox-runtime",
                action="coder_cost_ledger_write_failed",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"error": type(exc).__name__, "cost_usd": artifacts.cost_usd,
                         "stage": inp.stage, "outcome": outcome},
            )
        return None


def _meter_failed_coder_turn(
    exc: Exception, *, inp: RunCoderTurnInput, agent: Any
) -> dict[str, Any] | None:
    """Book what a turn spent BEFORE it failed, and return the payload that has
    to ride on the propagated error. None = there was nothing to book.

    `remote_substrate.run_turn` accumulates the cost before raising, so by the
    time we are here the money is real and known — but the turn never reaches
    `collect_artifacts()` below, so it used to land NOWHERE: no ledger row (the
    console under-reports), no `cost_usd` on any result (the $25 ceiling never
    counts it). Under `RetryPolicy(maximum_attempts=0)` the same turn can fail
    over and over, each attempt spending and each one disappearing.

    Both ends or neither: a ledger row on a path the workflow cannot count would
    only build the OPPOSITE asymmetry — money the ledger sees and the ceiling
    never does. So the same numbers are also returned, to travel on the error.

    Exactly once per attempt: the substrate is rebuilt on every Activity attempt
    (`_build_substrate`), so the accumulated cost covers THIS attempt alone, and
    the `raise` that follows guarantees the success-path metering never runs for
    the same turn. A retry re-spends real money and legitimately gets a new row.

    Nothing in here may mask the turn's own error, so every step is defensive.
    """
    try:
        artifacts = agent.collect_artifacts()
    except Exception:  # noqa: BLE001 — a bookkeeping failure never wins over the turn's
        logger.exception("could not collect the artifacts of the failed coder turn")
        return None
    if not artifacts.cost_usd:
        # Failed BEFORE spending anything (the exec never ran, the result did
        # not decode): a ledger row here would be an invented charge.
        return None

    spend: dict[str, Any] = {
        "cost_usd": artifacts.cost_usd,
        "tokens_in": artifacts.tokens_in,
        "tokens_out": artifacts.tokens_out,
        "ledger_id": _meter_coder_spend(inp, artifacts, agent=agent, outcome="failed"),
        "stage": inp.stage,
        "work_item_id": inp.work_item_id,
    }
    with contextlib.suppress(Exception):  # audit_emit talks to Postgres
        audit_emit(
            actor="system:sandbox-runtime",
            action="coder_turn_failed_after_spend",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={**spend, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
        )
    return {FAILED_TURN_SPEND_KEY: spend}


def _error_carrying_spend(exc: Exception, spend: dict[str, Any]) -> Exception:
    """The same failure, re-shaped so the money it already spent rides along.

    A plain `raise` cannot carry it: Temporal converts a non-ApplicationError
    into an ApplicationError with EMPTY details, and `details` is the only slot
    on a failed Activity the workflow can read. Everything the workflow
    classifies on is therefore copied VERBATIM — the `type` (Temporal itself
    uses the exception's class name; a structured raiser uses the dse.failure.*
    vocabulary), `non_retryable`, and the message that the legacy substring
    fallback still reads. Only `details` changes.
    """
    from temporalio.exceptions import ApplicationError

    if isinstance(exc, ApplicationError):
        return ApplicationError(
            exc.message, *exc.details, spend, type=exc.type, non_retryable=exc.non_retryable
        )
    return ApplicationError(
        str(exc) or type(exc).__name__, spend, type=type(exc).__name__, non_retryable=False
    )


@activity.defn(name=ACTIVITY_RUN_CODER_TURN)
async def run_coder_turn(inp: RunCoderTurnInput) -> CoderTurnResult:
    """Thin wrapper registered as the actual Activity — Temporal does not accept
    extra arguments (neither keyword-only nor optional positional) on functions
    decorated with `@activity.defn`. The real logic and the dependency-injection
    points for tests (`substrate`/`script`) live in `_run_coder_turn_impl`,
    called both from here (production, no overrides) and directly by the tests
    (with a scripted `FakeSubstrate`)."""
    if sandbox_inprocess_enabled():
        # Legacy in-process path: forbidden in production (fail-closed).
        reject_local_agent_execution("coder")
    else:
        # Isolated path (Phase 1): the SDK runs in the agent-runner inside the
        # sandbox; here we only validate real substrate/gateway per profile.
        validate_runtime_profile(require_real_substrate=True, require_real_gateway=True)
    return await _run_coder_turn_impl(inp)


async def _run_coder_turn_impl(
    inp: RunCoderTurnInput, substrate: AgentSubstrate | None = None, script: list[dict[str, Any]] | None = None
) -> CoderTurnResult:
    """Run one Coder turn inside the already-provisioned sandbox.

    P1 (no flow decision made by an LLM): the `substrate` ONLY edits files — the
    commit/push to the task branch happens here, in deterministic code
    (`ScopedGitSession`), never by the LLM. `substrate`/`script` are
    dependency-injection parameters used by the tests; in production WS-B's
    worker calls the `run_coder_turn` Activity without them and gets
    `FakeSubstrate` (document the real override via the env
    `DSE_CODER_SUBSTRATE=openhands` — see the README) until the OpenHands
    integration is complete.
    """
    branch = inp.branch or _default_branch(inp.work_item_id)
    workspace_dir, _bare_repo_path = _paths_for(inp.work_item_id)

    headers = GatewayCallHeaders(
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        stage=Stage(inp.stage),
        task_class=inp.task_class,
        data_class=inp.data_class,
    )
    vk = mint_virtual_key(headers)

    agent = substrate if substrate is not None else _build_substrate(script, stage=inp.stage)
    agent.create_session(
        work_item_id=inp.work_item_id,
        workspace_dir=workspace_dir,
        gateway_headers=headers,
        virtual_key=vk.virtual_key,
        gateway_base_url=vk.gateway_base_url,
    )

    # Anchor the plan into the instruction (found in the real run: the CLI
    # creates unsolicited reports — BUG_FIX_REPORT.md). Layer 1: an explicit
    # instruction; layer 2 (deterministic): a post-turn prune of disposable
    # artifacts ONLY (report/log/scratch), never of legitimate new source — see
    # _prune_disposable_artifacts.
    note = plan_constraints_note(list(inp.expected_files or []))
    if note:
        inp.instruction += note

    pod_git = isinstance(agent, RemoteSubstrate) and not getattr(
        agent.driver, "workspace_is_host_visible", True
    )

    # Repo skills (console ticks materialized at provision + skills committed in
    # the target repo): ClaudeAgentSubstrate loads them via
    # setting_sources=["project"]; the note covers the other substrates. The
    # note is read WHERE THE WORKSPACE IS: on K8s that is the Pod volume — the
    # host read below sees a directory that does not exist on the worker and
    # shipped "" on every production Coder turn, while the Tester (whose note is
    # built by a command run inside the Pod) listed the same skills (H3).
    if pod_git:
        run_in_pod = getattr(agent.driver, "run_in_pod", None)
        if run_in_pod is not None:
            inp.instruction += workspace_skills_note_in_pod(
                lambda argv, stdin: run_in_pod(agent.sandbox_id, argv, stdin)
            )
    else:
        inp.instruction += workspace_skills_note(workspace_dir)
    if pod_git:
        # K8s runtime: the workspace lives in the Pod volume — the turn's start
        # sha comes from a no-op checkpoint INSIDE the sandbox.
        base_sha = agent.checkpoint_sha(branch=branch, phase="turn-start")
    else:
        base_sha_session = ScopedGitSession(workspace_dir=workspace_dir, branch=branch)
        base_sha = base_sha_session.current_sha()

    done = False
    max_turns = 8
    turns = 0
    while not done and turns < max_turns:
        try:
            log = await run_sync_with_heartbeat(
                agent.run_turn,
                inp.instruction,
                stage=inp.stage,
                work_item_id=inp.work_item_id,
                operation=f"substrate_turn_{turns + 1}",
            )
        except Exception as exc:  # noqa: BLE001 — classification, not swallowing
            # It is this `raise` that skips the metering at the end of the
            # function: a turn that burned tokens and THEN failed reported its
            # spend to nobody. Book it, and hand it to the workflow on the error
            # itself — the only channel a failed Activity has.
            spend = _meter_failed_coder_turn(exc, inp=inp, agent=agent)
            _raise_if_permanent_provider_error(exc, spend=spend)
            if spend is None:
                raise  # nothing was measured: the error travels exactly as before
            raise _error_carrying_spend(exc, spend) from exc
        done = log.done
        turns += 1

    artifacts = agent.collect_artifacts()

    if pod_git:
        # K8s runtime: ALL the deterministic post-turn runs INSIDE the Pod
        # (--op post_turn — same sequence/source of truth as the block below, via
        # workspace_hygiene + scoped_git vendored into the runner). The audits
        # (P8) stay here in the worker, using the returned lists.
        post = agent.run_post_turn(
            branch=branch,
            expected_files=list(inp.expected_files or []),
            turn_start_sha=base_sha,
            commit_message=f"coder({inp.work_item_id}): {inp.instruction[:72]}",
        )
        for action, items in (
            ("coder_out_of_plan_files_pruned", post.pruned),
            ("coder_out_of_plan_files_kept", post.kept_out_of_plan),
            ("lockfile_churn_restored", post.restored_lockfiles),
        ):
            if items:
                audit_emit(
                    actor="system:sandbox-runtime",
                    action=action,
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"paths": items[:20]},
                )
        files_changed = list(post.files_changed)
    else:
        # Layer 2 (deterministic, P1): delete NEW (untracked) files that are
        # obvious CLI JUNK (unsolicited report/log/scratch) before the commit —
        # NOT "everything outside the plan" anymore (expected_files became
        # advisory in L1; a legitimate new source file outside the plan
        # SURVIVES). EXISTING files modified outside the plan stay — it is L1/the
        # budget that judges them.
        if inp.expected_files:
            pruned, kept_out_of_plan = _prune_disposable_artifacts(
                workspace_dir, inp.expected_files, inp.work_item_id
            )
            if pruned:
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="coder_out_of_plan_files_pruned",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"pruned": pruned[:20]},
                )
            if kept_out_of_plan:
                # Reconciliation observability (2026-07-22): under the old policy
                # these NEW out-of-plan files would be deleted; now they stay
                # (expected_files is advisory) and L1 is the judge (line budget +
                # forbidden_paths).
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="coder_out_of_plan_files_kept",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"kept_out_of_plan": kept_out_of_plan[:20]},
                )

        # BEFORE the hygiene block: `post-checkout` fires on path-limited
        # checkouts too (measured), and the hygiene steps run
        # `git checkout -- <lockfile>` moments after the turn's own `npm install`
        # repointed core.hooksPath at `.husky/` — the same window the OOM loop
        # came out of, one call earlier.
        ScopedGitSession(workspace_dir=workspace_dir, branch=branch).ensure_identity()

        _restore_lockfile_churn_audited(workspace_dir, inp.tenant_id, inp.work_item_id, stage="coder")

        # Aqui havia o revert de edição de teste do Coder. SAIU em 2026-08-10
        # junto com o reauthor: o DSE altera qualquer teste, e a mudança entra
        # no diff da PR, que é onde a supervisão passou a morar.

        # Deterministic commit/push — the substrate never has git access.
        git_session = ScopedGitSession(workspace_dir=workspace_dir, branch=branch)
        git_session.ensure_identity()
        if git_session.has_changes():
            git_session.commit(f"coder({inp.work_item_id}): {inp.instruction[:72]}")
        try:
            git_session.push()
        except GitScopeViolation:
            audit_emit(
                actor="system:sandbox-runtime",
                action="coder_push_rejected",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"branch": branch},
            )
            raise

        files_changed = git_session.files_changed_against(base_sha) if base_sha != git_session.current_sha() else []

    # Meter the coder into model_call_ledger — the same call the failure path
    # above makes, so a turn is booked once whichever way it ends.
    ledger_id = _meter_coder_spend(inp, artifacts, agent=agent, outcome="completed")

    audit_emit(
        actor="system:sandbox-runtime",
        action="coder_turn_completed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "instruction": inp.instruction,
            "files_changed": files_changed or artifacts.files_changed,
            "cost_usd": artifacts.cost_usd,
            "virtual_key_fixture": vk.fixture,
            "ledger_id": ledger_id,
        },
    )

    return CoderTurnResult(
        ledger_id=ledger_id,
        sandbox_id=artifacts.sandbox_id,
        diff_summary=artifacts.diff_summary,
        files_changed=files_changed or artifacts.files_changed,
        cost_usd=artifacts.cost_usd,
        tokens_in=artifacts.tokens_in,
        tokens_out=artifacts.tokens_out,
    )


# ===========================================================================
# Phase 2 — stage-scoped sessions (WSC-E3-T3/T4/T5)
# ===========================================================================

# ---------------------------------------------------------------------------
# run_planner_turn (WSC-E3-T3) — read-only session, emits a PlanArtifact
# ---------------------------------------------------------------------------
# PROMOTED to the contract (addendum 02 §2.3, the Phase 3 entry gate): the
# canonical definition lives in `dse_contracts.activities` with boundary
# regression tests (packages/contracts/tests/test_activity_boundaries.py)
# validating WS-B's exact payloads. Re-imported for compatibility — every local
# consumer (tests, sessions) keeps working unchanged.
from dse_contracts import RunPlannerTurnInput  # noqa: E402


def _default_plan_proposer(ctx: PlannerContext, inp: "RunPlannerTurnInput") -> dict[str, Any]:
    """MINIMAL plan proposal when no real substrate is plugged in — a clearly
    flagged fixture (same spirit as the Coder's `FakeSubstrate`). With WS-B's
    anti-empty-PR guard (`planner_expected_files_empty_...`), a plan from this
    fixture ESCALATES at the gate — deliberate behavior: with no real model, DSE
    does not pretend to plan."""
    return {
        "steps": [f"Analyze and implement: {inp.instruction[:120]}"],
        "expected_files": [],
        "test_plan": "Add/run tests covering the new behavior (Tester turn).",
    }


#: Clamp são da estimativa do Planner: lixo/ausente/não-positivo → None (nunca
#: se inventa um número — foi um default nunca dimensionado, o 400, que passou
#: anos na tela como se fosse previsão); acima do teto → teto (um modal não
#: exibe "3 milhões de linhas" como fato).
_ESTIMATED_LINES_MIN = 1
_ESTIMATED_LINES_MAX = 20_000


def _coerce_estimated_lines(raw: object) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(float(str(raw)))
    except (TypeError, ValueError):
        return None
    if value < _ESTIMATED_LINES_MIN:
        return None
    return min(value, _ESTIMATED_LINES_MAX)


_PLAN_PROMPT = """You are the Planner of Fintex DSE (an autonomous software engineer).
Based on the task below, produce a MINIMAL, verifiable implementation plan.

Respond ONLY with a valid JSON object (no markdown, no comments), in the format:
{{"steps": ["step 1", "step 2", ...],
  "expected_files": ["relative/path/1", "path/2", ...],
  "estimated_lines": 120,
  "test_plan": "how to verify the change",
  "needs_paths": ["directory/you/could/not/see/", ...]}}

"needs_paths" is OPTIONAL and exists only for the case below where the
repository listing says PARTIAL: it is how you declare the areas you could not
see. Omit the key entirely when the listing is complete.

Rules:
- "expected_files": the files that will be CREATED/EDITED (relative to the root).
  {files_rule}
  The implementation diff will be validated AGAINST this list (test files
  are exempt) — include ALL production files that may change. NEVER
  empty.
- "estimated_lines": integer, your order-of-magnitude estimate of the TOTAL
  lines the diff will change (added+removed, production and test files). An
  honest rough number — it is shown to the human approver; it is not a cap.
- 2 to 6 steps, specific and executable.
- The plan must solve EXACTLY the task in the "Task" section — nothing beyond it
  (no extra feature/refactor, however useful it may seem).
- Do not include anything besides the JSON.

## Task
{instruction}

## Additional context (skills/AGENTS.md/retrieval — may be empty)
{context}
{tree_section}"""


# ---------------------------------------------------------------------------
# What the Planner is allowed to SEE before it decides (plan items 0.4/3.1/3.3)
# ---------------------------------------------------------------------------
# There used to be ONE number, 8000, applied twice as a blind prefix cut: once
# on the rendered context and once on the repo tree. A blind prefix is the worst
# way to spend a budget — it cuts in the middle of a title, it lets one section
# evict another, and nothing anywhere records what was lost.
#
# Context budget: measured 2026-07-30 against the live fintex-poc registry (21
# skills), `ctx.render(skill_body_chars=0)` is 10.351 chars, so the 8.000 cut
# dropped 2.351 and 5 skills never reached the model — among them
# `redacting-pii-and-secrets`, in a regulated fintech tenant — while the ledger
# recorded all 21 as hydrated. 16.000 holds today's registry with ~55% headroom,
# but the headroom is not what makes this safe: overflow now drops WHOLE skills,
# least relevant first, and names them on the ledger.
_PLANNER_CONTEXT_BUDGET_CHARS = 16_000

# Tree budget: derived from the requirement, not rounded by taste. Assertion A1
# wants coverage 1.0 for the directories the final diff touches, so the selected
# directories of the acceptance repo have to fit COMPLETE — django/tests alone is
# 2.577 paths at a measured 44,76 chars/path ≈ 116 KB, and a selection of two
# needs room for the second. Under this budget django's whole tree (7.079 paths ≈
# 317 KB) still does not fit and takes the two-pass route, while dse_agent (1.045
# paths ≈ 47 KB) and fintex-wallet (12 paths) are delivered whole in one call.
# The bill: ~40k input tokens on a django-sized repo, ~$0.12 a plan, against a
# Coder turn measured at $1,0343 and up to 8 of them per work item.
_PLANNER_TREE_BUDGET_CHARS = 160_000

# The client's default (300) was tuned for a single blind prefix. The directory
# summary is a count over the WHOLE tree, and a summary computed from a 300-path
# prefix would misreport the repo's shape — which is the exact failure being
# fixed here. torvalds/linux, the worst case measured, is 71.798 blobs ≈ 7 MB of
# strings against a 1Gi pod.
_PLANNER_TREE_FETCH_LIMIT = 200_000

# Per-doc caps for the trusted repo docs. Sized against the measured specimen —
# a real client AGENTS.md is 564 tokens ≈ 2.256 chars — with room for a longer
# one, and low enough that both docs together (4.000 chars) cannot push the
# render past the 16.000-char budget and start evicting skills. A tenant whose
# doc is genuinely larger gets it cut on a line boundary, with the dropped char
# count on the ledger, rather than the whole block silently disappearing.
_PLANNER_AGENTS_MD_MAX_CHARS = 2_800
_PLANNER_CODEOWNERS_MAX_CHARS = 1_200

_TREE_MAX_SELECTED_DIRS = 3
# Caps on the summary itself, so a repo with thousands of entries at one level
# cannot eat the budget it is supposed to be a cheap index of. Whatever they cut
# is stated in the summary — a silent cap here would be the same bug one level up.
_TREE_SUMMARY_MAX_DIRS = 1000
_TREE_SUMMARY_MAX_FILES = 1000

_TREE_SELECT_PROMPT = """You are the Planner of Fintex DSE, deciding WHERE to look before planning.

This repository has {total} files — too many to list. Below is its top level.
Name the 1 to {max_dirs} directories whose COMPLETE file list you need in order
to plan the task: where the change will land, and where its tests live. A
narrower path ("pkg/db") is better than a broad one when you are sure of it.

Respond ONLY with a valid JSON object (no markdown, no comments):
{{"directories": ["dir1", "dir2"]}}

## Task
{instruction}

## Repository top level (counts cover the whole subtree)
{summary}"""

_WORD_RE = re.compile(r"[a-z0-9]+")


def _strip_json_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`\n")
        text = text[4:] if text.startswith("json") else text
    return text.strip()


def _planner_github_client():
    """One GitHub client per call site, built the way the tree fetch has always
    built it. Hoisted out of `_repo_tree_for_planner` so the tree and the repo
    docs — same repo, same ref, same turn — go through one construction path."""
    from dse_validation.config import GitHubConfig
    from dse_validation.github.client import build_github_client

    return build_github_client(GitHubConfig())


def _repo_tree_for_planner(repo: str, base_branch: str) -> list[str]:
    """The REAL repo tree on the base branch, via the control plane's GitHub API
    — without it the Planner guesses paths and plan_compliance rejects the real
    diff (found in the real run).

    RAISES on failure instead of returning []: the caller degrades AND records
    it. An empty return used to be indistinguishable from a repo with no files,
    on the log and on the ledger alike, so a Planner running blind looked exactly
    like a Planner running on a tiny repo."""
    return _planner_github_client().get_tree_paths(
        repo, base_branch or "main", limit=_PLANNER_TREE_FETCH_LIMIT
    )


def _cap_doc(text: str, limit: int) -> tuple[str, int]:
    """Cut to `limit` on a line boundary, and say how much was dropped. Never
    mid-line — half a convention reads exactly like a whole one, the same reason
    `_expand_into` refuses to slice a directory listing."""
    if len(text) <= limit:
        return text, 0
    head = text[:limit].rsplit("\n", 1)[0]
    return head + "\n[truncated to the planner doc budget]", len(text) - len(head)


def _repo_docs_for_planner(
    repo: str, base_branch: str, *, telemetry: dict[str, Any]
) -> tuple[str | None, str | None]:
    """AGENTS.md and CODEOWNERS at the base ref, through the same GitHub App the
    tree comes from.

    Returns '' when the file is genuinely absent at this ref and None when we
    could not ask. The caller records only the second: a repo without an
    AGENTS.md is a fact about that repo, not a degradation of ours, and an
    `absent` that cannot be told apart from an `unavailable` is how the workspace
    read stayed broken for as long as it did.

    At most four small GETs per turn, most of them 404s on a repo that curates
    neither file. Gating them on the tree would remove that, but the tree is
    fetched inside the proposer — after this runs — and hoisting it to save four
    1 KB requests at sandbox concurrency 1 is not a trade worth making yet."""
    from dse_validation.config import GitHubConfig

    if not GitHubConfig().is_configured:
        telemetry["repo_doc_unavailable_reason"] = "github_app_not_configured"
        return None, None

    ref = base_branch or "main"
    try:
        client = _planner_github_client()
        agents_md = client.get_file_text(repo, "AGENTS.md", ref) or ""
        codeowners = ""
        codeowners_path = ""
        # GitHub's own precedence: .github/ wins over the root, docs/ last.
        for candidate in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            found = client.get_file_text(repo, candidate, ref)
            if found:
                codeowners, codeowners_path = found, candidate
                break
    except Exception as exc:  # noqa: BLE001 — the docs are context, not a requirement
        telemetry["repo_doc_unavailable_reason"] = type(exc).__name__
        logger.warning("planner: repo docs unavailable for %s: %s", repo, exc)
        return None, None

    agents_md, agents_cut = _cap_doc(agents_md, _PLANNER_AGENTS_MD_MAX_CHARS)
    codeowners, owners_cut = _cap_doc(codeowners, _PLANNER_CODEOWNERS_MAX_CHARS)
    telemetry.update(
        agents_md_source="repo" if agents_md else "absent",
        agents_md_chars=len(agents_md),
        agents_md_truncated_chars=agents_cut,
        codeowners_source="repo" if codeowners else "absent",
        codeowners_path=codeowners_path,
        codeowners_chars=len(codeowners),
        codeowners_truncated_chars=owners_cut,
    )
    return agents_md, codeowners


def _run_episodes_for_planner(
    tenant_id: str, repo: str | None, base_sha: str | None, *, telemetry: dict[str, Any]
) -> str:
    """The diário de bordo for this repo, rendered for the prompt.

    Fail-soft like the tree and the repo docs — a Planner without the journal is
    a Planner that explores a bit more, not a failed work item — but the failure
    is recorded rather than becoming an empty string nobody can distinguish from
    a repository with no history."""
    from .run_episodes import RunEpisodesUnavailable, read_recent_episodes, render_episodes_block

    try:
        episodes = read_recent_episodes(tenant_id, repo=repo)
    except RunEpisodesUnavailable as exc:
        telemetry["run_episodes_unavailable_reason"] = type(exc.__cause__ or exc).__name__
        logger.warning("planner: run_episode journal unavailable for %s: %s", repo, exc)
        return ""
    telemetry["run_episodes_read"] = len(episodes)
    return render_episodes_block(episodes, base_sha=base_sha)


def _split_by_dir(paths: list[str], prefix: str = "") -> tuple[list[str], dict[str, int]]:
    """Files sitting DIRECTLY under `prefix`, and {subdirectory: files in its
    whole subtree}. Every path is assumed to start with `prefix`."""
    start = len(prefix)
    files: list[str] = []
    dirs: dict[str, int] = {}
    for path in paths:
        head, sep, _rest = path[start:].partition("/")
        if sep:
            dirs[head] = dirs.get(head, 0) + 1
        else:
            files.append(path)
    return files, dirs


def _dir_summary_block(paths: list[str], prefix: str = "") -> tuple[str, list[str]]:
    """Depth-1 summary under `prefix`, plus the files it lists VERBATIM.

    Depth 1 and not 2 because this block is what pass 1 pays for: measured,
    depth 1 is 207 chars on django and depth 2 is 8.655 — 40x the prompt to
    answer a question ("which 2-3 directories?") that the top level already
    answers. Depth 2 is bought back for free by the expansion below, and only
    where the model said it was needed."""
    files, dirs = _split_by_dir(paths, prefix)
    ranked = sorted(dirs.items(), key=lambda kv: (-kv[1], kv[0]))
    lines = [f"{prefix}{name}/ — {count} files" for name, count in ranked[:_TREE_SUMMARY_MAX_DIRS]]
    if len(ranked) > _TREE_SUMMARY_MAX_DIRS:
        lines.append(f"… and {len(ranked) - _TREE_SUMMARY_MAX_DIRS} more directories, not listed")
    listed = files[:_TREE_SUMMARY_MAX_FILES]
    lines.extend(listed)
    if len(files) > _TREE_SUMMARY_MAX_FILES:
        lines.append(f"… and {len(files) - _TREE_SUMMARY_MAX_FILES} more files here, not listed")
    return "\n".join(lines), listed


def _complete_dirs(delivered: list[str], tree: list[str]) -> list[str]:
    """Top-level directories delivered COMPLETE. This is what makes assertion A1
    ("coverage of the directories the final diff touches is 1.0") answerable by
    a query instead of by re-deriving the prompt."""
    _, total_by_dir = _split_by_dir(tree)
    _, got_by_dir = _split_by_dir(delivered)
    return sorted(name for name, count in total_by_dir.items() if got_by_dir.get(name, 0) == count)


def _validate_selected_dirs(wanted: list[Any], tree: list[str]) -> list[str]:
    """Keep only names that are REAL directories of this tree, in the order the
    model asked for them, without nesting (a chosen parent already delivers its
    children). A hallucinated directory is dropped here, not expanded."""
    out: list[str] = []
    for raw in wanted:
        name = str(raw).strip().removeprefix("./").strip("/")
        if not name or any(name == kept or name.startswith(kept + "/") or kept.startswith(name + "/")
                           for kept in out):
            continue
        if not any(path.startswith(name + "/") for path in tree):
            continue
        out.append(name)
        if len(out) >= _TREE_MAX_SELECTED_DIRS:
            break
    return out


def _expand_into(name: str, subtree: list[str], budget_left: int) -> tuple[str, list[str], list[str], list[str]]:
    """Deliver `name/` COMPLETE if it fits; otherwise one more level of shape
    plus whatever of its subdirectories fits whole.

    Smallest subdirectory first, deliberately: django/conf/locale is 2.500 of
    django's 3.000 files, and largest-first would spend the entire budget on
    locale catalogues and starve django/db and django/forms — which is the
    window the old prefix produced and the reason this route exists.

    Returns (block, delivered paths, directories delivered whole, directories
    given as shape only)."""
    listing = f"\n### {name}/ — all {len(subtree)} files\n" + "\n".join(subtree)
    if len(listing) <= budget_left:
        return listing, list(subtree), [name], []

    inner, inner_files = _dir_summary_block(subtree, name + "/")
    header = f"\n### {name}/ — {len(subtree)} files, too many to list; its own top level:\n{inner}"
    if len(header) > budget_left:
        return "", [], [], []
    blocks = [header]
    delivered = list(inner_files)
    expanded: list[str] = []
    used = len(header)
    _, subdirs = _split_by_dir(subtree, name + "/")
    for child, count in sorted(subdirs.items(), key=lambda kv: (kv[1], kv[0])):
        child_dir = f"{name}/{child}"
        child_tree = [path for path in subtree if path.startswith(child_dir + "/")]
        child_listing = f"\n#### {child_dir}/ — all {count} files\n" + "\n".join(child_tree)
        if used + len(child_listing) > budget_left:
            break  # sorted ascending: nothing after this one fits either
        blocks.append(child_listing)
        used += len(child_listing)
        delivered.extend(child_tree)
        expanded.append(child_dir)
    return "".join(blocks), delivered, expanded, [name]


def _partial_files_rule(delivered: int, total: int) -> str:
    return (
        f"The listing below is PARTIAL — {delivered} of {total} files. Prefer paths from it. "
        "Directories shown only as a file count DO exist and are NOT listed one by one, so if "
        "the change must touch one of them, name your best path for it AND add "
        '"needs_paths": ["<directory>/", ...] to the JSON, naming the areas you could not see. '
        "Do not plan as if the listing were complete."
    )


def _select_tree_directories(
    *, instruction: str, summary: str, total: int, headers: Any, virtual_key: str,
    model: str, chat_completion: Any,
) -> list[Any]:
    """Pass 1 of the two-pass tree view: the model names the directories it needs
    expanded. Deliberately cheap — the ONLY things it carries are the task and
    the depth-1 summary (207 chars on django). It does not re-render the skills
    context and it does not re-fetch the tree; pass 2 reuses both."""
    result = chat_completion(
        headers=headers,
        virtual_key=virtual_key,
        model=model,
        messages=[{"role": "user", "content": _TREE_SELECT_PROMPT.format(
            total=total, max_dirs=_TREE_MAX_SELECTED_DIRS,
            instruction=instruction, summary=summary,
        )}],
        timeout=60.0,
        max_tokens=300,
        temperature=0,
    )
    payload, _ = json.JSONDecoder().raw_decode(_strip_json_fence(result.content or ""))
    dirs = [d for d in payload.get("directories", []) if str(d).strip()]
    if not dirs:
        raise ValueError("the model named no directory")
    return dirs


class _TreeView(NamedTuple):
    section: str
    files_rule: str
    telemetry: dict[str, Any]


def _build_tree_view(tree: list[str], *, choose_dirs=None, fetch_error: str | None = None) -> _TreeView:
    """The Planner's view of the repository, AND the truth about how partial it
    is.

    The old view was `tree[:250]` then `[:8000]` chars: 219 of 7.079 blobs on
    django (3,09%), a window ending inside Croatian locale files, with nothing
    from django/db/, django/forms/ or tests/ — while the prompt told the model
    the window was authoritative. A model that knows it is seeing 3% behaves
    differently from one that believes it saw everything.

    Now: if the whole tree fits the budget it is delivered whole and this costs
    ONE model call, which is what small repos get (fintex-wallet's 12 files must
    not pay for two round trips). Otherwise the model is asked once, cheaply,
    which directories it needs, and those are delivered COMPLETE. Whatever does
    not make it is counted, named and put on the ledger."""
    total = len(tree)
    tel: dict[str, Any] = {
        "tree_total": total,
        "tree_delivered": 0,
        "tree_coverage": 0.0,
        "tree_mode": "unavailable",
        "tree_selection_calls": 0,
        "tree_dirs_expanded": [],
        "tree_dirs_summarized": [],
        "tree_degraded_reason": fetch_error,
    }
    if not tree:
        return _TreeView("", "Propose the most likely paths per the ecosystem's conventions.", tel)

    whole = "\n".join(tree)
    if len(whole) <= _PLANNER_TREE_BUDGET_CHARS:
        tel.update(tree_delivered=total, tree_coverage=1.0, tree_mode="full",
                   tree_dirs_expanded=_complete_dirs(tree, tree))
        return _TreeView(
            f"\n## Repo tree (base branch) — all {total} files\n{whole}",
            "Use ONLY paths from the tree below (or new ones consistent with it).",
            tel,
        )

    summary, root_files = _dir_summary_block(tree)
    selected: list[str] = []
    degraded: str | None = None
    if choose_dirs is None:
        degraded = "selection_unavailable"
    else:
        tel["tree_selection_calls"] = 1
        try:
            selected = _validate_selected_dirs(choose_dirs(summary, total), tree)
        except Exception as exc:  # noqa: BLE001 — a bad pass 1 degrades, it does not fail the plan
            degraded = f"selection_failed: {type(exc).__name__}: {str(exc)[:120]}"
        else:
            if not selected:
                degraded = "selection_named_no_real_directory"
    if degraded:
        logger.warning("planner tree selection degraded (%s) — falling back to the tree-order prefix", degraded)
        kept: list[str] = []
        used = 0
        for path in tree:
            if used + len(path) + 1 > _PLANNER_TREE_BUDGET_CHARS:
                break
            kept.append(path)
            used += len(path) + 1
        tel.update(tree_delivered=len(kept), tree_coverage=round(len(kept) / total, 4),
                   tree_mode="prefix_fallback", tree_degraded_reason=degraded,
                   tree_dirs_expanded=_complete_dirs(kept, tree))
        return _TreeView(
            f"\n## Repo tree (base branch) — PARTIAL: the first {len(kept)} of {total} files, "
            f"in tree order\n" + "\n".join(kept),
            _partial_files_rule(len(kept), total),
            tel,
        )

    blocks: list[str] = []
    delivered: list[str] = list(root_files)
    expanded: list[str] = []
    summarized: list[str] = []
    dropped: list[str] = []
    used = len(summary)
    for name in selected:
        subtree = [path for path in tree if path.startswith(name + "/")]
        # Whole, or shape plus whole children — never an arbitrary slice: half a
        # directory listing reads exactly like a complete one, which is the
        # failure this whole function exists to end.
        block, got, whole_dirs, shape_only = _expand_into(name, subtree, _PLANNER_TREE_BUDGET_CHARS - used)
        if not block:
            dropped.append(name)
            continue
        blocks.append(block)
        used += len(block)
        delivered.extend(got)
        expanded.extend(whole_dirs)
        summarized.extend(shape_only)

    tel.update(
        tree_delivered=len(delivered),
        tree_coverage=round(len(delivered) / total, 4),
        tree_mode="expanded",
        tree_dirs_expanded=expanded,
        tree_dirs_summarized=summarized,
        tree_degraded_reason=(f"budget_exhausted: {','.join(dropped)}" if dropped else None),
    )
    section = (
        f"\n## Repo tree (base branch) — PARTIAL VIEW: {len(delivered)} of {total} files listed\n"
        "Directories shown only as a file count are REAL and are NOT listed file by file.\n"
        f"{summary}" + "".join(blocks)
    )
    return _TreeView(section, _partial_files_rule(len(delivered), total), tel)


def _skills_by_relevance(skills: list[Any], instruction: str) -> list[int]:
    """Skill indices, most relevant to THIS work item first; ties keep registry
    order, so two identical runs drop the same skills. Relevance decides only
    the ORDER of eviction — which skill is worth keeping when the registry
    outgrows the budget is a registry decision (there is no priority column
    today), and inventing one here would put policy in the wrong file."""
    words = set(_WORD_RE.findall((instruction or "").lower()))
    scored: list[tuple[int, int]] = []
    for index, skill in enumerate(skills):
        haystack = " ".join([
            skill.skill_key, skill.title, skill.category, *(skill.applies_to or []),
        ]).lower()
        scored.append((-len(words & set(_WORD_RE.findall(haystack))), index))
    return [index for _score, index in sorted(scored)]


def _fit_planner_context(
    ctx: PlannerContext, *, instruction: str, budget: int = _PLANNER_CONTEXT_BUDGET_CHARS
) -> tuple[str, dict[str, Any]]:
    """Fit the context into `budget` by dropping WHOLE skills, never by cutting
    the string.

    skill_body_chars=0 renders the skills as an INDEX (key, category, title,
    path) instead of inlining ~100 KB of bodies; the Coder still reads the full
    text from .claude/skills/<key>/SKILL.md. What is new is that the index is
    fitted by UNIT: a skill either arrives whole or is dropped by name onto the
    ledger. The old `[:8000]` truncated inside a title and evicted everything
    render() places after the skills — the repo map included."""
    skills = list(getattr(ctx, "skills", None) or [])
    rendered = ctx.render(skill_body_chars=0)
    tel: dict[str, Any] = {
        "skills_resolved": len(skills),
        "skills_delivered": [s.skill_key for s in skills],
        "skills_dropped": [],
        "context_budget_chars": budget,
        "context_rendered_chars": len(rendered),
        "context_truncated_chars": 0,
    }
    tel["run_episodes_chars"] = len(getattr(ctx, "run_episodes", "") or "")
    tel["run_episodes_dropped"] = False
    if len(rendered) > budget and tel["run_episodes_chars"] and dataclasses.is_dataclass(ctx):
        # The journal goes BEFORE any skill does. A skill is curated by a human
        # and approved by a named one; a journal entry is a machine's note about
        # a past run. Evicting the human's guidance to keep the machine's memo
        # inverts the whole point of the registry — and it would happen silently,
        # because the skill-eviction loop below reports only which skills it
        # dropped, never what it kept them out for.
        ctx = dataclasses.replace(ctx, run_episodes="")
        rendered = ctx.render(skill_body_chars=0)
        tel["run_episodes_dropped"] = True
    if len(rendered) > budget and skills and dataclasses.is_dataclass(ctx):
        rendered = dataclasses.replace(ctx, skills=[]).render(skill_body_chars=0)
        keep: set[int] = set()
        for index in _skills_by_relevance(skills, instruction):
            candidate = sorted(keep | {index})
            text = dataclasses.replace(ctx, skills=[skills[i] for i in candidate]).render(skill_body_chars=0)
            if len(text) <= budget:
                keep = set(candidate)
                rendered = text
        tel["skills_delivered"] = [skills[i].skill_key for i in sorted(keep)]
        tel["skills_dropped"] = [s.skill_key for i, s in enumerate(skills) if i not in keep]
    if len(rendered) > budget:
        # Backstop for what skills cannot buy back (a monorepo CODEOWNERS, a
        # repo map). Still never mid-line, and still counted.
        marker = "\n[context truncated to the planner budget]"
        cut = rendered[: max(0, budget - len(marker))]
        cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
        tel["context_truncated_chars"] = len(rendered) - len(cut)
        rendered = cut + marker
    tel["context_chars"] = len(rendered)
    return rendered, tel


def _model_plan_proposer(
    ctx: PlannerContext, inp: "RunPlannerTurnInput", headers: Any, virtual_key: str,
    *, telemetry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Plan proposed by the REAL MODEL through the gateway (stage=planner,
    virtual key, enforcement + cost ledger on the path — WSD). Returns None on
    any failure (missing import, refused call, invalid JSON) — the caller falls
    back to the fixture and WS-B's guard escalates CLEANLY (P6), never an
    invented plan.

    P1 preserved: the model only PROPOSES steps/expected_files/test_plan; risk
    and gates remain derived deterministically (classify_risk_class).

    `telemetry` is filled IN PLACE rather than returned, because the caller has
    to be able to record WHAT THE PLANNER SAW even on the paths where this
    returns None — a plan that failed with 3% of the tree in front of it is
    exactly the case the ledger has to be able to explain."""
    tel = telemetry if telemetry is not None else {}
    try:
        from model_gateway_client.gateway_call import chat_completion
    except ImportError:
        logger.warning("model_gateway_client unavailable — planner falls back to the fixture")
        return None

    model = os.environ.get("DSE_PLANNER_MODEL") or os.environ.get("DSE_CODER_MODEL", "anthropic/claude")
    # The INSTRUCTION goes straight into the prompt (3rd real run: PlannerContext
    # does not carry the instruction — render() only has AGENTS.md/skills/repo
    # map, all empty for this tenant — and the model, never having SEEN the
    # issue, planned a generic wallet feature instead of the DELETE bug).
    instruction = (inp.instruction or "").strip()[:6000] or "(instruction missing)"
    try:
        tree = _repo_tree_for_planner(inp.repo, inp.base_branch or "main")
        fetch_error = None
    except Exception as exc:  # noqa: BLE001 — the tree is context, not a requirement
        logger.warning("repo tree unavailable for the planner (%s: %s)",
                       type(exc).__name__, str(exc)[:120])
        tree, fetch_error = [], f"tree_fetch_failed: {type(exc).__name__}"

    view = _build_tree_view(
        tree,
        choose_dirs=lambda summary, total: _select_tree_directories(
            instruction=instruction, summary=summary, total=total, headers=headers,
            virtual_key=virtual_key, model=model, chat_completion=chat_completion,
        ),
        fetch_error=fetch_error,
    )
    context, context_tel = _fit_planner_context(ctx, instruction=instruction)
    tel.update(view.telemetry)
    tel.update(context_tel)
    tel["planner_model_calls"] = 1 + int(view.telemetry["tree_selection_calls"])

    prompt = _PLAN_PROMPT.format(
        instruction=instruction,
        context=context,
        files_rule=view.files_rule,
        tree_section=view.section,
    )
    try:
        result = chat_completion(
            headers=headers,
            virtual_key=virtual_key,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            timeout=120.0,
            max_tokens=1500,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001 — refusal/error => fixture (clean escalation)
        _raise_if_permanent_provider_error(exc)  # billing/auth: the right message on the issue
        logger.warning("planner via model failed (%s: %s) — fixture", type(exc).__name__, str(exc)[:200])
        return None

    text = _strip_json_fence(result.content or "")
    try:
        proposal, _ = json.JSONDecoder().raw_decode(text)
        steps = [str(s) for s in proposal.get("steps", []) if str(s).strip()]
        files = [str(f) for f in proposal.get("expected_files", []) if str(f).strip()]
        if not steps or not files:
            raise ValueError("steps/expected_files are empty")
        # What the model says it could NOT see. It only exists because the
        # prompt stopped claiming the window was authoritative, and it is the
        # only signal that distinguishes "planned with everything in view" from
        # "planned around a gap it was honest about".
        unseen = [str(p) for p in proposal.get("needs_paths", []) if str(p).strip()][:20]
        if unseen:
            tel["planner_unseen_paths_requested"] = unseen
        return {
            "steps": steps[:10],
            "expected_files": files[:30],
            "estimated_lines": _coerce_estimated_lines(proposal.get("estimated_lines")),
            "test_plan": str(proposal.get("test_plan") or "Cover the change with tests (Tester turn)."),
        }
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("model plan did not parse (%s) — fixture; response: %.200s", exc, text)
        return None


@activity.defn(name=ACTIVITY_RUN_PLANNER_TURN)
async def run_planner_turn(inp: RunPlannerTurnInput) -> PlanArtifact:
    """Thin wrapper registered as the Temporal Activity (same pattern as
    `run_coder_turn`). The logic and the test injection points live in
    `_run_planner_turn_impl`."""
    reject_local_agent_execution("planner")
    return await _run_planner_turn_impl(inp)


async def _run_planner_turn_impl(
    inp: RunPlannerTurnInput,
    *,
    retrieval: RetrievalService | None = None,
    proposer=None,
    exploration_script: list[dict[str, Any]] | None = None,
    skills_conn=None,
) -> PlanArtifact:
    """READ-ONLY Planner session (WSC-E3-T3).

    Read-only toolset: hydrates AGENTS.md + the tenant's approved skill registry
    (E4) + CODEOWNERS + related tickets + retrieval/index (E5), and emits a
    structured PlanArtifact. Any WRITE tool fails (`ToolPermissionError`) — the
    session uses `PlannerToolset`. P1: `risk_class` is DERIVED by
    `classify_risk_class` (deterministic), not by the LLM's word — and it is what
    drives WS-B's gate.
    """
    workspace_dir, _bare = _paths_for(inp.work_item_id)

    # The model call (if any) goes out ONLY through the gateway, stage=planner.
    headers = GatewayCallHeaders(
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        stage=Stage.planner,
        task_class=inp.task_class,
        data_class=inp.data_class,
    )
    vk = mint_virtual_key(headers)

    # Declared HERE, not beside the proposer: doc provenance is resolved before
    # any model runs, so it has to reach `planner_turn_completed` on the fixture
    # path too — where no proposer ever writes into this dict.
    context_telemetry: dict[str, Any] = {}
    agents_md, codeowners = _repo_docs_for_planner(
        inp.repo, inp.base_branch or "main", telemetry=context_telemetry
    )
    episodes_block = _run_episodes_for_planner(
        inp.tenant_id, inp.repo, inp.base_sha, telemetry=context_telemetry
    )

    retrieval = retrieval if retrieval is not None else RetrievalService()
    ctx = hydrate_planner_context(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        instruction=inp.instruction,
        task_class=inp.task_class,
        related_tickets=inp.related_tickets,
        retrieval=retrieval,
        skills_conn=skills_conn,
        agents_md=agents_md,
        codeowners=codeowners,
        doc_ref=f"{inp.repo}@{inp.base_branch or 'main'}",
        run_episodes=episodes_block,
    )
    if context_telemetry.get("repo_doc_unavailable_reason"):
        # Not the same as "this repo has no AGENTS.md" — that is countable off
        # planner_turn_completed.agents_md_source and needs no event. This one is
        # us failing to ask, and it has to be findable with `WHERE action = …`.
        audit_emit(
            actor="system:sandbox-runtime",
            action="planner_repo_doc_unavailable",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={
                "repo": inp.repo,
                "ref": inp.base_branch or "main",
                "reason": context_telemetry["repo_doc_unavailable_reason"],
            },
        )
    if not ctx.skills:
        # The emptiness was already ON the ledger — planner_turn_completed
        # carries skills_hydrated=[] — but buried in a details field, so it
        # could not be found with `WHERE action = …`. This makes the symmetric
        # provision-stage fact queryable at the Planner too.
        audit_emit(
            actor="system:sandbox-runtime",
            action="skills_resolved_empty",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={"repo": inp.repo, "stage": "planner", "task_class": inp.task_class},
        )

    # File-based skills (`.claude/skills/`) are materialized by
    # provision_sandbox (after the clone — the Planner may run BEFORE the
    # provision, with the workspace not yet existing here). This re-materialize
    # is a no-op in that case (the `.git` guard in skill_files) and refreshes the
    # workspace when the registry changed between retries.
    skills_materialized = materialize_skills(workspace_dir, ctx.skills)

    # Read-only session: any write step in the exploration_script FAILS here
    # (the planner toolset), which is exactly the conformance test.
    session = ScriptedAgentSession(
        toolset=PlannerToolset(),
        workspace_dir=workspace_dir,
        retrieval=retrieval,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        context_reads={
            "read_agents_md": ctx.agents_md,
            "read_codeowners": ctx.codeowners,
            "list_skills": "\n".join(s.skill_key for s in ctx.skills),
        },
    )
    if exploration_script:
        await run_sync_with_heartbeat(
            session.run_script,
            exploration_script,
            stage=Stage.planner.value,
            work_item_id=inp.work_item_id,
            operation="planner_exploration",
        )

    # Proposer selection (P1 — by CONFIG, never by a model):
    #   explicit proposer (tests) > real model (substrate != fake) with a
    #   fallback to the fixture > fixture. The fixture has empty expected_files
    #   and WS-B's guard escalates — deliberate when there is no model.
    # What the Planner actually SAW: doc provenance is already in here from
    # before the hydrate; the model proposer adds the tree/skill telemetry and is
    # read after the call — including on its failure paths, which is where the
    # question "what was in front of it?" matters most.
    if proposer is not None:
        proposal_fn = proposer
    elif os.environ.get(SUBSTRATE_ENV_VAR, "fake").strip().lower() != "fake":
        def proposal_fn(c):  # noqa: ANN001 — run_sync_with_heartbeat's signature
            return (
                _model_plan_proposer(c, inp, headers, vk.virtual_key, telemetry=context_telemetry)
                or _default_plan_proposer(c, inp)
            )
    else:
        proposal_fn = lambda c: _default_plan_proposer(c, inp)  # noqa: E731
    proposal = await run_sync_with_heartbeat(
        proposal_fn,
        ctx,
        stage=Stage.planner.value,
        work_item_id=inp.work_item_id,
        operation="planner_proposal",
    )
    expected_files = list(proposal.get("expected_files", []))
    forbidden = PlanArtifact.model_fields["forbidden_paths"].default_factory()
    # rc.89: o risco é classificado pela ESTIMATIVA do Planner (None quando não
    # há — fixture nunca finge), não pela constante 400 do input, que fazia
    # todo plano sair >= medium e mantinha o ramo high-por-tamanho morto.
    estimated_lines = proposal.get("estimated_lines")
    risk_class = classify_risk_class(expected_files, estimated_lines, forbidden)

    plan = PlanArtifact(
        work_item_id=inp.work_item_id,
        steps=list(proposal.get("steps", [])),
        expected_files=expected_files,
        diff_budget_lines=inp.diff_budget_lines,
        estimated_lines=estimated_lines,
        test_plan=proposal.get("test_plan", ""),
        risk_class=risk_class,
        forbidden_paths=forbidden,
    )

    # Both events below are QUERYABLE actions, not fields buried in details —
    # the same reason `skills_resolved_empty` exists above. A number nobody can
    # find with `WHERE action = …` is a number nobody investigates.
    # The backstop cut counts as a truncation too: a context with NO skills (or
    # one whose CODEOWNERS alone overflows) loses text without a single skill
    # being dropped, and firing only on `skills_dropped` would leave exactly
    # that case findable only by reading a field.
    if context_telemetry.get("run_episodes_unavailable_reason") or context_telemetry.get(
        "run_episodes_dropped"
    ):
        # The journal going missing is the fourth way this Planner can quietly
        # see less than it should, after the AGENTS.md workspace read, the
        # skills note under k8s, and repo_map's '(not indexed)'. It gets its own
        # action rather than a field, so "how often does the journal actually
        # reach the model?" is answerable.
        audit_emit(
            actor="system:sandbox-runtime",
            action="planner_run_journal_absent",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={
                "repo": inp.repo,
                "reason": context_telemetry.get("run_episodes_unavailable_reason")
                or "evicted_for_budget",
                "episodes_read": context_telemetry.get("run_episodes_read"),
            },
        )
    if context_telemetry.get("skills_dropped") or context_telemetry.get("context_truncated_chars"):
        audit_emit(
            actor="system:sandbox-runtime",
            action="planner_context_truncated",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={
                "stage": "planner",
                "skills_resolved": context_telemetry.get("skills_resolved"),
                "skills_delivered": context_telemetry.get("skills_delivered"),
                "skills_dropped": context_telemetry.get("skills_dropped"),
                "context_chars": context_telemetry.get("context_chars"),
                "context_budget_chars": context_telemetry.get("context_budget_chars"),
                "context_truncated_chars": context_telemetry.get("context_truncated_chars"),
            },
        )
    # `budget_exhausted: <dirs>` happens with tree_mode="expanded" — the model
    # named a directory and it did not fit. That is a degradation like any
    # other, so it gets the same queryable action instead of a field.
    if (context_telemetry.get("tree_mode") in {"prefix_fallback", "unavailable"}
            or context_telemetry.get("tree_degraded_reason")):
        audit_emit(
            actor="system:sandbox-runtime",
            action="planner_tree_degraded",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={
                "stage": "planner",
                "repo": inp.repo,
                "tree_mode": context_telemetry.get("tree_mode"),
                "reason": context_telemetry.get("tree_degraded_reason"),
                "tree_total": context_telemetry.get("tree_total"),
                "tree_delivered": context_telemetry.get("tree_delivered"),
                "tree_coverage": context_telemetry.get("tree_coverage"),
            },
        )

    audit_emit(
        actor="system:sandbox-runtime",
        action="planner_turn_completed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "stage": "planner",
            # Without this the event cannot be grouped by repository, and
            # "which repositories curate no AGENTS.md?" — the query that decides
            # where to propose one — has nothing to group agents_md_source by.
            "repo": inp.repo,
            "steps": plan.steps,
            "expected_files": plan.expected_files,
            "risk_class": plan.risk_class,
            # rc.89: audita o número DECLARADO (ou None, honesto) — o campo
            # antigo era a constante 400, um número de ninguém.
            "estimated_lines": plan.estimated_lines,
            # `skills_hydrated` keeps its meaning (RESOLVED into the context)
            # and stops implying delivery: `skills_delivered`/`skills_dropped`
            # carry that, by name. It used to record 21 while 16 reached the
            # model, and the console had no way to know.
            "skills_hydrated": [s.skill_key for s in ctx.skills],
            "skills_materialized": skills_materialized,
            "retrieval_hits": [f"{h.repo}/{h.path}" for h in ctx.retrieval_hits],
            "virtual_key_fixture": vk.fixture,
            **context_telemetry,
        },
    )
    return plan


# ---------------------------------------------------------------------------
# run_tester_turn (WSC-E3-T4) — test runners + test authoring (test paths only)
# ---------------------------------------------------------------------------
# PROMOTED to the contract (addendum 02 §2.3) — canonical definition in
# `dse_contracts.activities`, boundary tests in the foundation. Re-imported for
# the local consumers' compatibility.
from dse_contracts import RunTesterTurnInput, TesterTurnResult  # noqa: E402


# PERMANENT provider error patterns (found in the real 2026-07-22 run): exhausted
# credits/an invalid key are not transient — retrying is an infinite loop of
# attempts. Raised non_retryable; the workflow converts them into _FailClosed →
# a clean failure commented on the issue (P6).
_PERMANENT_PROVIDER_MARKERS = (
    "credit balance is too low",
    "plans & billing",
    "insufficient credits",
    "authentication_error",
    "invalid x-api-key",
)


def _raise_if_permanent_provider_error(exc: Exception, *, spend: dict[str, Any] | None = None) -> None:
    """`spend`: what the failing turn had ALREADY paid for, when the caller
    measured it (see FAILED_TURN_SPEND_KEY). Exhausted credits are the one
    failure that is certain to arrive AFTER money was spent, so dropping it here
    would lose exactly the money the ceiling most needs to know about."""
    blob = f"{type(exc).__name__}:{exc}".lower()
    if any(m in blob for m in _PERMANENT_PROVIDER_MARKERS):
        from dse_contracts.failure import FailureClass, failure_type
        from temporalio.exceptions import ApplicationError

        # Phase 2 (plan 09): the class travels in the TYPE (the contract's closed
        # vocabulary) — the workflow no longer depends on a message substring.
        # (The legacy "ProviderBillingError" is still recognized on parse for
        # replay.)
        raise ApplicationError(
            f"provider_billing_or_auth: {str(exc)[:200]}",
            *((spend,) if spend is not None else ()),
            type=failure_type(FailureClass.provider_billing),
            non_retryable=True,
        ) from exc


_TEST_AUTHOR_PROMPT = """You are the Tester of Fintex DSE. Write AUTOMATED test(s) that
verify the described change — ideally reproducing the bug (they fail without the fix,
pass with it).

Respond ONLY with valid JSON (no markdown):
{{"files": [{{"path": "relative/path/of/the/test", "content": "full file content"}}]}}

CRITICAL RULES:
- Use EXACTLY the runner and style of the EXISTING TEST shown below (same
  imports, same structure). Do NOT use jest/mocha/vitest/supertest or ANY
  package that is not in the dependencies of the package.json shown — the repo
  may have no dependencies at all (native runner).
- NEVER rewrite a test the REPOSITORY owns.
  FORBIDDEN PATHS (the repository's own): {existing_tests}
  Use a new name, e.g.: test/<subject>-dse.test.js
- You MAY rewrite a test YOU wrote on this branch. If the failure you were
  given names a file you authored, FIX THAT FILE — same path, corrected
  content. Writing a second copy under a new name leaves both broken and the
  suite red forever: that has happened, and it is why the run above failed.
- Paths MUST be test paths (tests/, __tests__/, *.test.js|ts, test_*.py…).
- 1 file (2 at most); CONCISE (~40-80 lines). Truncated JSON = failure.
- Do not modify production code — tests only.
- THE PROCESS MUST EXIT WHEN THE TESTS FINISH. Close every resource you open —
  an http server started in a test or a before() hook must be closed in an
  after() that runs even when a test fails; clear every timer/interval; do not
  leave a connection open. A suite whose tests all pass but whose process never
  exits is a FAILURE here: the runner is killed on a timeout and your tests
  count for nothing. If you need a server, prefer `server.listen(0)` and
  `after(() => new Promise(r => server.close(r)))`.
{error_feedback}
## Task
{instruction}

## Plan
{plan}

## Repo package.json (REAL runner/dependencies)
{package_json}

## EXISTING test from the repo (IMITATE this style/runner)
{example_test}

## Coder's change (diff)
{diff}
"""


#: Every slice of the authoring prompt is bounded, and bounded AT THE SOURCE —
#: the Pod truncates before the bytes ever cross into the worker.
#: Directories a source tree keeps but nobody reads here. The list is NOT
#: Node-only on purpose: on a Python repo a project-local `.venv` buries the
#: repository's own tests under site-packages, and the listing's bound then
#: truncates the real ones away — measured on this repo, 73% of matches came
#: from `.venv` and only 246 of 2501 real test paths survived. `existing_tests`
#: exists to stop the Tester authoring a duplicate; a listing full of
#: `site-packages/aiohttp/test_utils.py` does the opposite.
_PRUNED_DIRS = (
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".tox", "dist", "build", "target", "vendor", "coverage",
    ".angular", ".next", ".nuxt", ".gradle", ".cache",
)
_FIND_TESTS_SCRIPT = (
    "find . \\( "
    + " -o ".join(f"-name {d}" for d in _PRUNED_DIRS)
    + " \\) -prune -o -type f -print | grep -Ei 'test|spec'"
)

_PKG_JSON_CHARS = 1_500
_EXAMPLE_TEST_CHARS = 3_000
_TEST_LISTING_CHARS = 64_000
_TESTER_DIFF_CHARS = 8_000
_SKILLS_NOTE_CHARS = 2_000
#: Referência de spec das skills do repo: uma spec completa cabe folgada; o
#: cap protege o prompt de um arquivo de referência acidental gigante.
_REFERENCE_SPEC_CHARS = 4_000


def _run_pod_command(argv: list[str], *, timeout: int, input_text: str | None = None):
    """Run one `kubectl exec` and decode its output TOLERANTLY.

    `errors="replace"` is the whole reason this is a named function rather than
    an inline call: every context read is bounded with `head -c`, which cuts
    BYTES, so a truncated multibyte sequence is normal rather than exceptional.
    Decoding it strictly raised `UnicodeDecodeError` out of the activity — past
    every handler — so Temporal retried the Tester turn from a cold `npm ci`
    and nothing durable said why. One em dash near a bound was enough."""
    import subprocess as _sp

    return _sp.run(
        argv, capture_output=True, text=True, errors="replace",
        timeout=timeout, input=input_text,
    )


def _sh_quote(path: str) -> str:
    """Single-quote a repository path for `sh -c`. The path comes from `find`
    inside the customer's repo, so a filename with a quote or a `;` in it is
    input, not an accident."""
    return "'" + path.replace("'", "'\\''") + "'"


def _suite_verdict_deferred() -> bool:
    """Whether the Tester's suite run is informational rather than a gate.

    On by default. It is a flag rather than a deletion because the Tester's own
    run is still the only thing that can tell a hung suite from a slow one
    (`suite_hung`, rc=124) before L1's much longer clock — so a repository where
    that matters can turn the gate back on with
    `DSE_TESTER_SUITE_IS_A_GATE=1`."""
    return os.environ.get("DSE_TESTER_SUITE_IS_A_GATE", "").strip().lower() not in {
        "1", "true", "yes",
    }


def _example_candidates(existing: set[str], diff_files: list[str]) -> list[str]:
    """Os testes existentes em ordem de PROXIMIDADE do diff (D1).

    O exemplo ensina fixtures, mocks e imports — e só ensina o certo se vier do
    subsistema que a mudança tocou. Ordem: (1) o teste cujo STEM casa com um
    arquivo do diff (`Foo.java` → `FooTest.java`, `foo.ts` → `foo.spec.ts`);
    (2) o teste que compartilha o diretório mais profundo com algum arquivo do
    diff; (3) o resto, por caminho, para ser determinístico.

    Antes disto o exemplo era o primeiro que o `os.walk` topava — ordem do
    sistema de arquivos — e num repo grande isso é um subsistema aleatório."""
    def _stem(path: str) -> str:
        return path.rsplit("/", 1)[-1].split(".", 1)[0]

    diff_stems = {_stem(d) for d in diff_files}
    diff_dirs = [d.rsplit("/", 1)[0] for d in diff_files if "/" in d]

    def _shared_depth(rel: str) -> int:
        parts = rel.split("/")
        best = 0
        for d in diff_dirs:
            dp = d.split("/")
            n = 0
            for a, b in zip(parts, dp):
                if a != b:
                    break
                n += 1
            best = max(best, n)
        return best

    def _rank(rel: str) -> tuple[int, int, str]:
        stem = _stem(rel)
        # `FooTest`/`FooSpec`/`foo.spec` → o sujeito é o stem sem o sufixo
        subject = stem
        for suffix in ("Test", "Tests", "Spec", "IT"):
            if subject.endswith(suffix) and len(subject) > len(suffix):
                subject = subject[: -len(suffix)]
                break
        matches_stem = subject in diff_stems or stem in diff_stems
        return (0 if matches_stem else 1, -_shared_depth(rel), rel)

    return sorted(existing, key=_rank)


def _tester_repo_context(
    workspace_dir: str, diff_files: list[str] | None = None
) -> tuple[str, str, set[str]]:
    """Deterministic context so authoring imitates the REAL repo (found in the
    real run: the model wrote Jest in a node:test repo with no deps and also
    overwrote the original test — nothing ever ran). Returns
    (package_json, example_test, existing_test_paths)."""
    from dse_contracts.paths import is_test_path

    pkg = ""
    try:
        pkg = open(os.path.join(workspace_dir, "package.json")).read()[:1500]
    except OSError:
        pkg = "(no package.json — likely Python/pytest)"
    existing: set[str] = set()
    for root, _dirs, files in os.walk(workspace_dir):
        if "/.git" in root or "/node_modules" in root:
            continue
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), workspace_dir)
            if is_test_path(rel):
                existing.add(rel)

    example = ""
    for rel in _example_candidates(existing, diff_files or []):
        try:
            with open(os.path.join(workspace_dir, rel), encoding="utf-8",
                      errors="replace") as fh:
                example = f"# {rel}\n" + fh.read()[:3000]
            break
        except OSError:
            continue
    return pkg, example or "(no existing tests — use the ecosystem's default runner)", existing


class _TesterContextUnavailable(RuntimeError):
    """The sandbox's workspace could not be read at all."""


class _TesterContext(NamedTuple):
    """Everything the authoring prompt needs about the repository — and nothing
    else. Tens of kB at the very worst, against a workspace that is hundreds of
    megabytes: that ratio is the whole point.

    The K8s path used to `kubectl cp` the entire /workspace here to produce it.
    After the L1 gate's `npm ci` that tree is an Angular install, and it landed
    in the worker's /tmp — an emptyDir capped at 256Mi — so kubelet evicted the
    Pod mid-activity, five times in five minutes, each replacement refilling the
    volume. Excluding node_modules only narrowed it: `.angular/cache` and
    `dist/` are there too, `--exclude=./node_modules` does not match a nested
    `packages/*/node_modules` under GNU tar, and buffering the archive to strip
    it just moves an eviction into an OOMKill.

    The consumer never wanted a filesystem. It wants package.json, one example
    test, the list of test paths and the last diff, each already truncated. So
    each is read with a bounded command instead."""

    package_json: str
    example_test: str
    existing_tests: set[str]
    diff: str
    skills_note: str
    #: The local copy of the repository, when one exists. `None` on K8s, where
    #: the repository lives only in the Pod.
    workspace_dir: str | None = None
    #: Spec de referência declarada pelas SKILLS do repo
    #: (.claude/skills/*/references/*.spec.*) — CONTEÚDO, não caminho. A nota
    #: de skills lista arquivos e manda "ler", mas a autoria é one-shot sem
    #: tools: instrução de leitura para ator sem leitura é prompt vazio.
    #: Medido 3x (badge 'warning', pageSize, sortField): a resposta estava no
    #: Pod e o modelo nunca a viu. Vazio quando o repo não declara referência.
    reference_spec: str = ""


def _pod_tester_context(pod_sh) -> _TesterContext:
    """Reads the authoring context out of the sandbox Pod, one bounded command
    at a time. Nothing is copied, so nothing can overrun a disk or a heap.

    Every read is truncated IN THE POD (`head -c`), so an enormous tracked file
    costs one buffer of the size we asked for, not its own size."""
    from dse_contracts.paths import is_test_path

    def _read(script: str, limit: int, *, required: bool = False) -> str:
        # `head -c` counts BYTES. Cutting inside a multibyte character leaves a
        # truncated sequence, and `text=True` decodes strictly — so a
        # package.json with an em dash near the bound raised UnicodeDecodeError
        # OUT of the activity, Temporal retried the turn from a cold `npm ci`,
        # and nothing durable said why. `_pod_sh` decodes with errors="replace"
        # for exactly this; the byte bound stays (it is what caps the transfer)
        # and Python re-truncates to characters below.
        proc = pod_sh(
            f"cd /workspace && {script} 2>/dev/null | head -c {limit}",
            timeout=_CONTEXT_READ_TIMEOUT_SECONDS,
        )
        if required and proc.returncode != 0:
            # A read marked `required` is one whose failure means the Pod or the
            # workspace is not readable at all. Degrading quietly here would
            # send the model an empty repository and bill a turn for tests
            # written against nothing.
            raise _TesterContextUnavailable(
                f"reading the workspace failed (rc={proc.returncode}): "
                f"{(proc.stderr or '')[-300:]}"
            )
        # A trailing U+FFFD is the byte cut, not repository content.
        return (proc.stdout or "").rstrip("\ufffd")

    package_json = _read("cat package.json", _PKG_JSON_CHARS) or (
        "(no package.json — likely Python/pytest)"
    )
    # `-prune` stops find BEFORE it descends into the trees nobody reads; on an
    # Angular install that is the difference between listing a handful of paths
    # and walking 40k. The `grep` is a coarse pre-filter, and it is a strict
    # SUPERSET of `is_test_path` — every pattern there contains "test" or
    # "spec" (tests?/, __tests__/, test_*.py, *_test.py, conftest.py,
    # *.test.ts, *.spec.ts, *_test.go). The exact rule is still applied below;
    # this only keeps the listing small enough that its bound cannot truncate a
    # real repo's test files.
    listing = _read(
        # `git rev-parse` first, and its exit code is the one that matters: the
        # pipeline's own rc is `head`'s, which is 0 even when `find` dies. This
        # is the positive proof the old code got from checking for `.git` in the
        # copied tree, and without it an EMPTY /workspace reads as a repository
        # with no tests — a state this system has actually shipped
        # ("A rebuilt sandbox came up empty", #43/#44) — and bills a whole
        # authoring turn for tests written against nothing.
        "git rev-parse --git-dir >/dev/null 2>&1 || exit 3; "
        + _FIND_TESTS_SCRIPT,
        _TEST_LISTING_CHARS,
        required=True,
    )
    lines = listing.splitlines()
    if len(listing) >= _TEST_LISTING_CHARS and lines:
        # The bound cut mid-path. `./tests/integration/very_long...` truncated
        # to `tests/integr` still satisfies is_test_path and would enter the set
        # as a file that does not exist.
        lines.pop()
    existing = {
        rel for rel in (ln.strip().removeprefix("./") for ln in lines)
        if rel and is_test_path(rel)
    }
    example = ""
    # D1: os candidatos vêm por PROXIMIDADE DO DIFF, não em ordem alfabética —
    # o exemplo ensina fixtures/mocks/imports e só ensina o certo se vier do
    # subsistema que a mudança tocou (o alfabético entregava um subsistema
    # aleatório, origem provável dos testes que não compilam).
    changed = [
        ln.strip() for ln in
        _read("git show --name-only --pretty=format: HEAD", 4000).splitlines()
        if ln.strip()
    ]
    # `--` because `-` sorts before every other path character: a repository
    # file called `-e.test.js` would otherwise reach `cat` as an OPTION, return
    # rc 0 with empty output, and the prompt would say "no existing tests" while
    # listing those same tests as forbidden. And more than one candidate,
    # because the old local reader fell through to the next file when one could
    # not be read.
    for candidate in _example_candidates(existing, changed)[:3]:
        body = _read(f"cat -- {_sh_quote(candidate)}", _EXAMPLE_TEST_CHARS)
        if body.strip():
            example = f"# {candidate}\n{body}"
            break
    # The diff is read IN THE POD, which is where the repository is. Running it
    # against a local copy is what broke when the copy stopped carrying .git:
    # `git show` exits 128 and the prompt silently reads "(diff unavailable)".
    # `tail -c`, not `head -c`: the Docker path keeps the END of the diff
    # (`proc.stdout[-8000:]`), which is where the actual hunks are — the head is
    # the --stat summary. The two paths must show the model the same thing.
    diff = _read("git show --stat -p HEAD | tail -c $((%d))" % _TESTER_DIFF_CHARS,
                 _TESTER_DIFF_CHARS)
    # Same shape as `workspace_skills_note` on the Docker path, down to the
    # header: the directory is `.claude/skills`, each entry names the SKILL.md
    # and carries its `description:` line, and the framing tells the model the
    # guidance is mandatory. Getting any of that wrong does not fail — it
    # quietly weakens the prompt, which is the hardest kind of bug to notice.
    skills = _read(
        'for d in .claude/skills/*/; do [ -f "$d/SKILL.md" ] || continue; '
        'desc=$(grep -m1 "^description:" "$d/SKILL.md" | cut -d: -f2- | '
        "sed 's/^ *//'); n=$(basename \"$d\"); "
        'echo "- .claude/skills/$n/SKILL.md — ${desc:-$n}"; done',
        _SKILLS_NOTE_CHARS,
    )
    skills_note = (
        "\n\n## Repository skills (MANDATORY guidance)\n"
        "Before writing code, read each SKILL.md below and follow its rules:\n"
        + skills.rstrip("\n")
        if skills.strip()
        else ""
    )
    # A referência declarada pelo REPO nas suas skills — CONTEÚDO inline, não
    # caminho: a autoria é one-shot sem tools e não pode "ler" nada (a doença
    # medida 3x: badge 'warning', pageSize, sortField — a forma completa da
    # store estava em angular-testbed/references/ e o modelo nunca a viu).
    # Primeiro match por ordem de glob; sem classificador — o repo declara.
    reference_spec = _read(
        'for f in .claude/skills/*/references/*.spec.*; do '
        '[ -f "$f" ] && { echo "// $f"; cat "$f"; break; }; done',
        _REFERENCE_SPEC_CHARS,
    )
    # `workspace_dir` fica None aqui — no K8s o repositório vive só no Pod, e o
    # worker não tem onde rodar `git`. Isso era uma limitação enquanto havia
    # oráculo de autoria a consultar; desde 2026-08-10 não há posse de teste a
    # decidir, e o campo não tem mais consequência.
    return _TesterContext(
        package_json=package_json,
        example_test=example or "(no existing tests — use the ecosystem's default runner)",
        existing_tests=existing,
        diff=diff,
        skills_note=skills_note,
        reference_spec=reference_spec,
    )


def _local_tester_context(workspace_dir: str) -> _TesterContext:
    """The Docker path, where the workspace really is a local directory."""
    import subprocess as _sp

    diff = ""
    try:
        proc = _sp.run(["git", "show", "--stat", "-p", "HEAD"],
                       cwd=workspace_dir, capture_output=True, text=True, timeout=30)
        diff = proc.stdout[-_TESTER_DIFF_CHARS:]
    except Exception:  # noqa: BLE001 — the diff is context, not a requirement
        pass
    changed: list[str] = []
    try:
        names = _sp.run(["git", "show", "--name-only", "--pretty=format:", "HEAD"],
                        cwd=workspace_dir, capture_output=True, text=True, timeout=30)
        changed = [ln.strip() for ln in names.stdout.splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001 — proximidade é melhoria, nunca requisito
        pass
    package_json, example_test, existing_tests = _tester_repo_context(
        workspace_dir, diff_files=changed
    )
    # Espelho do read de referência do caminho K8s ("the two paths must show
    # the model the same thing").
    reference_spec = ""
    try:
        import glob as _glob
        for f in sorted(_glob.glob(os.path.join(workspace_dir, ".claude/skills/*/references/*.spec.*"))):
            with open(f, encoding="utf-8", errors="replace") as fh:
                rel = os.path.relpath(f, workspace_dir)
                reference_spec = f"// {rel}\n" + fh.read()[:_REFERENCE_SPEC_CHARS]
            break
    except OSError:
        pass
    return _TesterContext(
        package_json=package_json,
        example_test=example_test,
        existing_tests=existing_tests,
        diff=diff,
        skills_note=workspace_skills_note(workspace_dir)[:_SKILLS_NOTE_CHARS],
        workspace_dir=workspace_dir,
        reference_spec=reference_spec,
    )


def _model_authored_test_script(
    inp: "RunTesterTurnInput", ctx: _TesterContext, headers: Any, virtual_key: str,
    *, error_feedback: str = "",
) -> tuple[list[dict[str, Any]] | None, float]:
    """Test authoring by the REAL MODEL (same pattern as the planner): 1
    stage=tester call through the gateway → test files → deterministic script
    [write_file..., run_tests]. Deterministic guard-rails (P1):
      - IMITATION context: package.json + one existing test from the repo (the
        model copies the real runner, it never invents jest/supertest);
      - paths outside test paths OR pointing at ALREADY EXISTING tests are
        rejected (overwriting a repo test would destroy the suite);
      - `error_feedback` re-injects the infra error from the 1st attempt (1
        retry).
    Any failure → None → tests_ran=False → WS-B's gate stops cleanly.

    Returns (script, cost_usd). The COST is returned separately, and on the
    failure paths too: the call was billed the moment the gateway answered, so a
    response that does not parse costs exactly as much as one that does — and
    that money used to disappear, because the caller only ever saw the script."""
    try:
        from model_gateway_client.gateway_call import chat_completion
    except ImportError:
        logger.warning("model_gateway_client unavailable — tester without real authoring")
        return None, 0.0
    from dse_contracts.paths import is_test_path

    model = os.environ.get("DSE_TESTER_MODEL") or os.environ.get("DSE_CODER_MODEL", "anthropic/claude")
    prompt = _TEST_AUTHOR_PROMPT.format(
        instruction=(inp.instruction or "")[:3000],
        plan=json.dumps(inp.plan or {}, ensure_ascii=False)[:1500],
        package_json=ctx.package_json,
        example_test=ctx.example_test,
        existing_tests=", ".join(sorted(ctx.existing_tests)) or "(none)",
        diff=ctx.diff or "(diff unavailable)",
        error_feedback=(
            f"\n## ERROR FROM THE PREVIOUS ATTEMPT (fix it!)\n{error_feedback}\n" if error_feedback else ""
        ),
    )
    # Repo skills (materialized by the Planner + committed in the target repo):
    # the Tester must follow the guidance too (test style, tenant conventions).
    prompt += ctx.skills_note
    # A referência das skills vai INLINE — este é um one-shot sem tools: um
    # caminho listado é ilegível por construção, e foi assim que o mock saiu
    # sem `pagination`/`tableSorting` três runs seguidos com a resposta
    # committada no repo.
    if getattr(ctx, "reference_spec", ""):
        prompt += (
            "\n\n## Known-good reference spec for THIS repo (declared by its "
            "skills) — mirror its TestBed/store setup exactly:\n"
            + ctx.reference_spec
        )
    try:
        result = chat_completion(
            headers=headers, virtual_key=virtual_key, model=model,
            messages=[{"role": "user", "content": prompt}],
            # 8000: found in the real run with Haiku — 4000 truncated the JSON in
            # the middle of the content ("Unterminated string") and the whole
            # authoring pass fell over.
            timeout=_AUTHORING_CALL_TIMEOUT_SECONDS, max_tokens=8000, temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_if_permanent_provider_error(exc)  # billing/auth: the right message on the issue
        logger.warning("tester via model failed (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None, 0.0

    cost_usd = float(getattr(result, "cost_usd", 0.0) or 0.0)
    text = (result.content or "").strip()
    if text.startswith("```"):
        text = text.strip("`\n")
        text = text[4:] if text.startswith("json") else text
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text.strip())
        files = parsed.get("files") or []
    except json.JSONDecodeError as exc:
        logger.warning("test authoring did not parse (%s); response: %.200s", exc, text)
        return None, cost_usd

    script: list[dict[str, Any]] = []
    # O arquivo vai ONDE O TESTER PEDIU. O guard de renomeação — que desviava
    # escrita sobre teste existente do cliente para uma cópia `-dse` — SAIU em
    # 2026-08-10: não há mais posse de teste no DSE, e a supervisão de toda
    # edição é o diff da PR.
    #
    # O que ele custava, medido no testbed: contra a tentativa anterior do
    # PRÓPRIO Tester ele fazia o oposto do desejado — o laço reautorava a mesma
    # spec, o caminho estava ocupado, o arquivo caía como `...-dse-dse.spec.ts`
    # ao lado do original quebrado, e a rodada seguinte somava um terceiro.
    # Quatro arquivos acumulados, a suite piorando a cada rodada, o item
    # queimando o orçamento inteiro para ficar pior.
    for f in files[:3]:
        path, content = str(f.get("path") or ""), str(f.get("content") or "")
        if not (path and content and is_test_path(path)):
            logger.warning("test path refused (outside the allowed test paths): %r", path)
            continue
        script.append({"tool": "write_file", "path": path, "content": content})
    if not script:
        return None, cost_usd
    script.append({"tool": "run_tests"})
    return script, cost_usd


#: Commit subjects the platform writes for its own turns (scoped_git.commit).
# Fonte única em dse_contracts.paths (a regra que vive em cada call site
# diverge — ver hooksPath); o alias preserva os usos locais.


def _zero_verdict_specs(output: str, owned: list[str]) -> list[str]:
    """Porta 5 — os arquivos do PRÓPRIO Tester cuja suite falhou SEM produzir
    veredito: a carga morreu (jest: `Test suite failed to run` — módulo
    inexistente, import quebrado) ou a compilação (Maven: `[ERROR]
    /workspace/<path>:` do testCompile). Nenhuma asserção foi avaliada, então
    não há veredito a proteger — é instrumento quebrado, não teste reprovando.

    POR ARQUIVO, deliberadamente: uma spec quebrada não autoriza reescrever a
    saudável ao lado. Um arquivo cuja suite EXECUTOU (falha de asserção) nunca
    entra aqui — re-autorá-lo seria reescrever o teste que reprova o código,
    e esse veredito pertence ao L1/humano (o racional do deferral, intacto)."""
    broken: list[str] = []
    for path in owned:
        esc = re.escape(path)
        if re.search(
            rf"FAIL\s+{esc}[^\n]*\n(?:[^\n]*\n){{0,3}}?\s*●\s+Test suite failed to run",
            output,
        ):
            broken.append(path)
            continue
        if re.search(rf"\[ERROR\]\s+/workspace/{esc}[:\[]", output):
            broken.append(path)
    return broken


def plan_constraints_note(expected_files: list[str]) -> str:
    """As regras do plano que entram na instrução do Coder.

    Duas coisas que esta função existe para NÃO fazer, ambas medidas em
    2026-08-10 (wi_049e6fb8, 5ª ocorrência do primeiro turno vazio — US$ 0,12 e
    zero arquivos):

    1. **Contradição.** O `expected_files` do Planner inclui os testes que a
       mudança precisa. Listá-los sob "modifique SOMENTE estes" logo antes de
       proibir tocar em teste entrega ao ator uma ordem impossível — a
       explicação mais simples para um turno que devolve nada. Os testes saem
       da lista; a lista some se não sobrar produção (afirmar "somente estes"
       com lista vazia proibiria tudo).

    2. **Mentira sobre a política.** "any test change you make is reverted"
       valeu até a decisão de operador de 2026-08-10. Hoje o Coder PODE editar
       spec de cliente — a edição entra no diff da PR, onde o revisor a vê — e
       o pós-turno reverte apenas o INSTRUMENTO do Tester. Dizer ao ator que
       seu trabalho será desfeito é pedir que ele não o faça."""
    production = [p for p in (expected_files or []) if not is_test_path(p)]
    lines = ["\n\n## Plan constraints (mandatory)"]
    if production:
        lines.append(
            f"- Focus your production changes on these files: {', '.join(production)}."
        )
    # A nota dizia só o que era PROIBIDO ("suas edições em spec do Tester são
    # revertidas"), e um ator que nunca é autorizado não exerce permissão
    # nenhuma: o item parou três vezes num impasse que ele podia resolver. Desde
    # 2026-08-10 não há mais posse de teste — o revert saiu junto com o reauthor
    # — então o texto passa a conceder o que a política concede, e a única
    # proibição que sobra é dita como o que é: uma regra sem mecanismo atrás.
    lines.append(
        "- Writing the task's NEW tests is the Tester's stage, not yours; your job "
        "is the production code. But ANY test that your change breaks is yours to "
        "deal with — including the ones written for this task: update the assertion "
        "when it pins behaviour this task deliberately changed, or fix the code when "
        "it exposes a real defect. That edit lands in the PR diff for human review — "
        "legitimate work, not a violation."
    )
    lines.append(
        "- NEVER delete, skip or empty a test to make the suite pass, and never "
        "weaken an assertion you cannot justify in the PR. Nothing reverts test "
        "edits any more: what you write is exactly what the reviewer gets."
    )
    lines.append(
        "- Do NOT create documentation/report files (README, *_REPORT.md, "
        "CHANGELOG…) — the change and the tests speak for themselves."
    )
    return "\n".join(lines)


_restore_lockfile_churn = workspace_hygiene.restore_lockfile_churn
def _restore_lockfile_churn_audited(
    workspace_dir: str, tenant_id: str, work_item_id: str, *, stage: str
) -> None:
    restored = _restore_lockfile_churn(workspace_dir)
    if restored:
        audit_emit(
            actor="system:sandbox-runtime",
            action="lockfile_churn_restored",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"stage": stage, "restored": restored[:10]},
        )


def _tester_authored_files_in_history(workspace_dir: str) -> list[str]:
    """Test files from previous `tester(...)` commits that are still present in
    the workspace. If they exist, the turn RE-RUNS those tests instead of
    authoring new ones (2nd real run: every fix cycle authored ONE MORE test —
    the diff only grew, the target moved on each lap and the loop never
    converged). The fix cycle only works with a fixed target."""
    import subprocess as _sp

    from dse_contracts.paths import is_test_path as _is_test

    try:
        log = _sp.run(
            ["git", "log", "--format=%H %s"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — with no readable history, author as usual
        return []
    files: list[str] = []
    for line in log.splitlines():
        sha, _, subject = line.partition(" ")
        if not subject.startswith("tester("):
            continue
        try:
            names = _sp.run(
                ["git", "show", "--name-only", "--format=", sha], cwd=workspace_dir,
                capture_output=True, text=True, timeout=30,
            ).stdout
        except Exception:  # noqa: BLE001
            continue
        for rel in names.splitlines():
            rel = rel.strip()
            if (
                rel
                and _is_test(rel)
                and rel not in files
                and os.path.exists(os.path.join(workspace_dir, rel))
            ):
                files.append(rel)
    return files


_TEST_INFRA_ERROR_MARKERS = (
    "err_module_not_found", "cannot find package", "cannot find module",
    "err_require_esm", "syntaxerror", "modulenotfounderror", "importerror",
)

# ---------------------------------------------------------------------------
# The Tester's clocks.
#
# They nest, and the nesting is the whole point:
#
#     npm install ─┐
#                  ├─ pod exec ─ start_to_close ─ schedule_to_close
#     the suite   ─┘
#
# An inner clock that is not STRICTLY smaller than the one wrapping it never
# fires — the outer one gets there first and the ending is MISNAMED. A suite
# that overruns then comes back as `exec_timeout` (the worker's own deadline: an
# infra fact) instead of `suite_hung` (the suite itself), and those two escalate
# down different paths, so a clock out of order hands the reader someone else's
# problem.
#
# This was one module constant, 180 s, calibrated against a 1,4 s suite in a
# 12-file repo. Before it existed the suite ran bare under the 600 s exec, so
# anything from 180 s to 600 s passed; afterwards it could not. And the exit
# criterion is a real suite of 10 to 15 minutes on 1 vCPU, which 180 s makes
# impossible by construction. So the numbers come from CONFIGURATION and the
# code guarantees only the order.
# ---------------------------------------------------------------------------

# Named after the L1 side's DSE_L1_TIMEOUT_SECONDS: the same concept, for the
# same repo, at the other end of the system. They are still two numbers — the
# single source is the repo's own `.dse/validation.json` (RunTesterTurnInput
# already carries the `base_sha` it would be read at), and joining them is the
# item that gives that manifest a per-command `timeouts` block.
_SUITE_TIMEOUT_ENV_VAR = "DSE_TESTER_SUITE_TIMEOUT_SECONDS"
_INSTALL_TIMEOUT_ENV_VAR = "DSE_TESTER_INSTALL_TIMEOUT_SECONDS"

# 20 minutes. The criterion asks for a real suite of 10 to 15 minutes on 1 vCPU;
# a default sitting exactly on the requirement's ceiling would kill the run it
# exists to allow.
#
# It is also HALF OF A PAIR, which is the part no reader can see from here.
# Once this turn passes, L1 runs the repository's own `test` command in THIS
# SAME Pod, in the same /workspace, over the node_modules this turn's install
# just wrote: a SECOND clock on the SAME suite
# (`dse_validation.config._DEFAULT_TEST_TIMEOUT_SECONDS`, which is derived FROM
# this one and must stay above it). Only the SHORTER of the two is ever
# observed, so which service holds it decides what the ending gets called — and
# the two endings are not priced alike:
#
#   shorter here → `suite_hung`, named for what was actually measured, worth one
#                  Coder turn and then an escalation that says so;
#   shorter in L1 → a `test` finding with status ERROR, worth up to THREE paid
#                  Coder turns (~US$1.03 each) hunting an assertion that never
#                  failed. That is the mis-diagnosis rc.12 removed from this
#                  service, reappearing one service to the right.
#
# So this number never moves alone: it is the FLOOR the other side sizes itself
# against. Raising it without raising L1's re-creates that diagnosis; lowering
# it below the criterion's 15 minutes makes the acceptance run impossible here
# instead, which is why the fix for the inversion was L1 coming up rather than
# this coming down. `test_the_default_never_outruns_the_l1_clock_on_the_same_
# suite` is what fails when the pair comes apart.
_DEFAULT_SUITE_TIMEOUT_SECONDS = 1200

# 15 minutes. `npm install` had no budget of its own: it shared the exec with
# the suite, so whatever one did not spend belonged to the other and nothing
# said which one ran out. Every install here is COLD (the workspace is an
# emptyDir recreated per sandbox), the acceptance repo's node_modules is ≥ 1 GB,
# and this pod measures 6,7x slower than the laptop the reference numbers came
# from — 60-90 s there is 400-600 s here.
#
# So the band's own slow end (90 s x 6,7 = 603 s) lands OUTSIDE a 600 s budget,
# which is why that is not the number. What an install killed at its own clock
# costs is not a lost turn — the suite still runs, and the note on stderr says
# the tree is incomplete — it is a WRONG VERDICT: the suite resolves against a
# half-written node_modules, fails with MODULE_NOT_FOUND, and `tests_failed` is
# a statement about the code. That is the mis-diagnosis rc.12 removed from this
# service, and the exit criterion asks for a real PASS/FAIL on a BIG repository,
# where this is the likeliest way to not get one.
#
# 900 is 1,5x the slow end of the band and costs nothing structurally: with the
# suite at its default the fit against a 3600 s start_to_close admits an install
# of up to 1140 s before anything is clamped, so nothing here moves.
_DEFAULT_INSTALL_TIMEOUT_SECONDS = 900

# Floors for the clamp below: under a minute neither number is a budget.
_MIN_SUITE_TIMEOUT_SECONDS = 60
_MIN_INSTALL_TIMEOUT_SECONDS = 60

# What the exec gets ON TOP of install+suite: the two `timeout -k 10` grace
# windows, the kubectl round trip, and the shell around them. This is what keeps
# the exec strictly outside the suite, so an overrun is named `suite_hung`.
_POD_EXEC_MARGIN_SECONDS = 60

# Short commands in the Pod — git config, the reuse probe, writing a test file,
# the commit. None of them is a suite, and the 600 s they used to inherit as the
# default was room the suite could not have.
_POD_CONTROL_TIMEOUT_SECONDS = 120
_HEAD_READ_TIMEOUT_SECONDS = 60
#: The five reads that build the authoring context (package.json, the test
#: listing, one example test, the diff, the skills note). Each is a `cat`,
#: a pruned `find` or a `git show` — seconds, not minutes. This replaced a 180s
#: `kubectl cp` of the whole workspace, so the turn's non-suite budget shrinks
#: rather than grows.
_CONTEXT_READ_TIMEOUT_SECONDS = 30
_CONTEXT_READ_CALLS = 5
_AUTHORING_CALL_TIMEOUT_SECONDS = 180.0

# Everything one turn spends inside the SAME activity besides the suite's exec:
# the authoring call, the five context reads, at most six control
# commands (git config, the reuse probe, up to three file writes, the commit)
# and the HEAD read. The suite's exec has to fit in what is LEFT of
# start_to_close: an activity the server kills returns NO result, so the turn is
# retried from zero — cold install included — with nothing durable saying why.
_MAX_POD_CONTROL_CALLS = 6
_TESTER_NON_SUITE_BUDGET_SECONDS = (
    int(_AUTHORING_CALL_TIMEOUT_SECONDS)
    + _CONTEXT_READ_CALLS * _CONTEXT_READ_TIMEOUT_SECONDS
    + _MAX_POD_CONTROL_CALLS * _POD_CONTROL_TIMEOUT_SECONDS
    + _HEAD_READ_TIMEOUT_SECONDS
)


class _TesterClocks(NamedTuple):
    """The nested budgets of one Tester turn, in seconds."""

    install: int
    suite: int
    pod_exec: int
    #: (install, suite) AS CONFIGURED, when they did not fit and were clamped.
    clamped_from: tuple[int, int] | None


def _timeout_env(name: str, default: int) -> int:
    """A clock from the environment. Anything that is not a positive whole
    number of seconds is a typo in a ConfigMap, and failing the turn over it
    would throw away work the tenant already paid for — so it falls back to the
    documented default, loudly."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        seconds = int(raw)
    except ValueError:
        seconds = 0
    if seconds <= 0:
        logger.error(
            "%s=%r is not a positive number of seconds — using the default %ds",
            name, raw, default,
        )
        return default
    return seconds


def _outer_activity_budget_seconds() -> float | None:
    """The clock the SERVER will enforce on THIS attempt, or None when there is
    no activity context (the Docker path, and the tests). `start_to_close` is
    the per-attempt ceiling; `schedule_to_close` bounds the whole chain of
    attempts, so it is only the fallback for a caller that sets one and not the
    other."""
    try:
        if not activity.in_activity():
            return None
        info = activity.info()
    except Exception:  # noqa: BLE001 — a missing context never fails a turn
        return None
    for candidate in (info.start_to_close_timeout, info.schedule_to_close_timeout):
        if candidate is not None and candidate.total_seconds() > 0:
            return candidate.total_seconds()
    return None


def _tester_clocks() -> _TesterClocks:
    """Read the budgets and hand them back ALREADY ordered.

    Two things are guaranteed here and nowhere else: the suite is strictly
    inside the exec that wraps it (by construction — the exec is their sum plus
    a margin), and the exec is inside what the activity has left. The second is
    not the operator's to get right: `start_to_close` lives in the WORKFLOW's
    input, so a suite budget that was legal yesterday stops being legal the day
    someone lowers it, and nothing on this side would notice."""
    install = _timeout_env(_INSTALL_TIMEOUT_ENV_VAR, _DEFAULT_INSTALL_TIMEOUT_SECONDS)
    suite = _timeout_env(_SUITE_TIMEOUT_ENV_VAR, _DEFAULT_SUITE_TIMEOUT_SECONDS)
    configured = (install, suite)

    outer = _outer_activity_budget_seconds()
    if outer is not None:
        # Twice the margin: once for the shell inside the exec, once for the
        # activity's own work around it (the audit writes, encoding the result).
        # It is what makes the total STRICTLY less than the activity's clock
        # instead of exactly equal to it.
        room = int(outer) - _TESTER_NON_SUITE_BUDGET_SECONDS - 2 * _POD_EXEC_MARGIN_SECONDS
        if install + suite > room:
            # Clamped, not refused. Refusing produces no verdict at all — which
            # is the failure mode this whole path exists to end — while a
            # shorter clock still produces one, and the audit says the number
            # was not the one that was asked for. Proportional, so the ratio the
            # operator chose between preparing and measuring survives.
            budget = max(room, _MIN_INSTALL_TIMEOUT_SECONDS + _MIN_SUITE_TIMEOUT_SECONDS)
            scale = budget / (install + suite)
            suite = max(_MIN_SUITE_TIMEOUT_SECONDS, int(suite * scale))
            install = max(_MIN_INSTALL_TIMEOUT_SECONDS, budget - suite)

    return _TesterClocks(
        install=install,
        suite=suite,
        pod_exec=install + suite + _POD_EXEC_MARGIN_SECONDS,
        clamped_from=None if (install, suite) == configured else configured,
    )


def _authored_test_infra_error(workspace_dir: str, test_paths: list[str]) -> str | None:
    """Run ONLY the freshly authored tests and detect an INFRA error
    (import/syntax — a test that never executes), as distinct from a failing
    assertion (which is a legitimate signal of an incomplete fix → the Coder's
    fix cycle). Returns the error for re-authoring, or None if the tests
    execute."""
    import subprocess as _sp

    if not test_paths:
        return None
    if test_paths[0].endswith(".py"):
        cmd = [sys.executable, "-m", "pytest", "-q", *test_paths]
    else:
        cmd = ["node", "--test", *test_paths]
    try:
        proc = _sp.run(cmd, cwd=workspace_dir, capture_output=True, text=True, timeout=180)
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    blob = (proc.stdout + proc.stderr).lower()
    if any(m in blob for m in _TEST_INFRA_ERROR_MARKERS):
        return (proc.stdout + proc.stderr)[-1500:]
    return None


@activity.defn(name=ACTIVITY_RUN_TESTER_TURN)
async def run_tester_turn(inp: RunTesterTurnInput) -> TesterTurnResult:
    reject_local_agent_execution("tester")
    return await _run_tester_turn_impl(inp)


# A kubectl call the WORKER's own clock killed. Outside the 0-255 range a shell
# can return, so it can never be confused with something the suite said.
_RC_POD_EXEC_TIMEOUT = -1001

# What ENDED the suite. Only `tests_failed` is a verdict about the code under
# test; the rest are infra facts the Coder cannot act on — and it was being
# handed all of them as failing assertions.
_SUITE_OUTCOME_INFRA = frozenset({"suite_hung", "resource_kill", "exec_timeout"})

#: Teto do typecheck quando o manifesto do repo não declara o seu.
_TYPECHECK_TIMEOUT_DEFAULT_SECONDS = 300


def _tail_both(proc: Any) -> str:
    """Cauda de stdout+stderr. Os DOIS: o tsc escreve diagnóstico em stdout e o
    runner de outro dialeto escreve em stderr — ler um só foi como todo gate L1
    publicou a evidência errada por dois dias (#60)."""
    return ((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or ""))[
        -_FAILURE_OUTPUT_CHARS:
    ]


def _pod_manifest_typecheck(_pod_sh) -> tuple[list[str] | None, int]:
    """O comando de typecheck DECLARADO pelo repo em `.dse/validation.json`, que
    é a régua que o L1 aplica depois — a do build, nunca a das specs.

    Devolve `(None, _)` quando não há manifesto ou não há comando: ausência
    declarada não vira veredito, e um repo que nunca declarou typecheck segue
    com o turno que sempre teve."""
    r = _pod_sh("cat /workspace/.dse/validation.json 2>/dev/null")
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None, _TYPECHECK_TIMEOUT_DEFAULT_SECONDS
    try:
        manifest = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        logger.warning("tester: .dse/validation.json ilegível; typecheck do turno pulado")
        return None, _TYPECHECK_TIMEOUT_DEFAULT_SECONDS
    cmd = ((manifest.get("commands") or {}).get("typecheck")) or []
    if not isinstance(cmd, list) or not cmd:
        return None, _TYPECHECK_TIMEOUT_DEFAULT_SECONDS
    timeout = (manifest.get("timeouts") or {}).get("typecheck")
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = _TYPECHECK_TIMEOUT_DEFAULT_SECONDS
    return [str(c) for c in cmd], max(30, timeout)


def _suite_outcome(*, tests_ran: bool, returncode: int) -> str:
    """Name what ended the suite. The infra codes are read FIRST: they are facts
    about the process, true whether or not any test was ever authored."""
    if returncode == 124:
        # coreutils `timeout`: the suite outlived the budget it was given.
        return "suite_hung"
    if returncode == 137:
        # 128+9 = SIGKILL, and NOT the suite's own clock — `timeout` reports its
        # kill as 124 even after `-k` escalates to SIGKILL. A SIGKILL from
        # outside the process means, on this runtime, the cgroup OOM killer.
        # Seen in production on 2026-07-29 and handed to the Coder as a failing
        # assertion, which is why it gets its own branch: a clock and a memory
        # limit are not fixed the same way.
        return "resource_kill"
    if returncode == _RC_POD_EXEC_TIMEOUT:
        return "exec_timeout"
    if not tests_ran:
        return "no_tests"
    return "passed" if returncode == 0 else "tests_failed"


def _infra_outcome_note(outcome: str, returncode: int, *, suite_timeout_seconds: int) -> str:
    """The first thing the Coder reads about a suite that produced no verdict.
    It says what was MEASURED and nothing more: the old timeout text asserted an
    open http server or a pending timer as fact, while a timeout cannot tell
    stuck from slow — so the Coder spent paid turns hunting a handle that may
    never have existed.

    `suite_timeout_seconds` is passed, not read from a constant: the budget is
    configuration now, and a note that names a number the run did not use is the
    same lie in a different font."""
    if outcome == "suite_hung":
        return (
            f"THE SUITE DID NOT TERMINATE within {suite_timeout_seconds}s and was killed.\n"
            "That is all that was measured. It may have been STUCK (nothing left to run and "
            "the process still up) or merely SLOW (still working when the clock ran out) — "
            "this cannot tell them apart, and no assertion failed here.\n"
            "If it is stuck, the usual cause is a resource a test opened and never closed: an "
            "http server with no after() that closes it, a pending timer, an open connection. "
            "If it is slow, make the suite cheaper — fewer cases, smaller fixtures.\n\n"
        )
    if outcome == "resource_kill":
        limit = os.environ.get("DSE_SANDBOX_MEM_LIMIT") or "(DSE_SANDBOX_MEM_LIMIT unset)"
        return (
            f"THE SUITE WAS KILLED FROM OUTSIDE (rc={returncode}, SIGKILL). THIS IS NOT A TEST "
            "FAILURE: the process died mid-run, no assertion was evaluated, and nothing below "
            "is a verdict on the code.\n"
            f"On this runtime that kill is the sandbox going over its MEMORY LIMIT of {limit}. "
            "The suite's own clock reports itself as rc=124, never 137, so time is not what "
            "ended this run.\n"
            "What helps: run less at once (smaller fixtures, fewer workers), or have an "
            "operator raise DSE_SANDBOX_MEM_LIMIT. Editing an assertion will not.\n\n"
        )
    if outcome == "exec_timeout":
        return (
            "THE POD EXEC WAS KILLED BY THE WORKER'S OWN TIMEOUT — the suite never reported a "
            "result. This is not a test failure; what follows is whatever had been printed "
            "before the kill, and the command and the limit that blew are at the end of it.\n\n"
        )
    return ""


def _timed_out_process(argv: list[str], exc: Any, limit: int) -> Any:
    """`subprocess.TimeoutExpired` → a CompletedProcess the caller can read.

    Uncaught, it leaves the activity with NO result at all: Temporal retries the
    whole Tester turn from zero — cold `npm install` included — and nothing
    durable says why. So the timeout becomes information instead: a returncode
    nobody else produces, the command, the limit that blew, and whatever the
    process had already printed."""
    import subprocess as _sp

    def _text(chunk: Any) -> str:
        # TimeoutExpired carries what was read before the kill: bytes when the
        # pipe was binary, None when nothing was read at all.
        if chunk is None:
            return ""
        return chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)

    # At the END of stderr on purpose: the caller keeps the TAIL of the output.
    note = f"\nTIMEOUT after {limit}s running: {' '.join(argv)[:200]}\n"
    return _sp.CompletedProcess(
        args=argv,
        returncode=_RC_POD_EXEC_TIMEOUT,
        stdout=_text(getattr(exc, "stdout", None)),
        stderr=_text(getattr(exc, "stderr", None)) + note,
    )


def _tester_pod_sync(
    inp: "RunTesterTurnInput",
    sandbox_id: str,
    headers: Any,
    virtual_key: str,
    push: bool,
) -> TesterTurnResult:
    """SYNCHRONOUS body of the Tester bridge on the K8s runtime (run under
    heartbeat).

    It operates INSIDE the sandbox Pod via `kubectl exec` — the workspace and the
    toolchain (node/git) live in the Pod (the Coder already ran there), NOT in the
    worker. Authoring (the model) needs the workspace context: `kubectl cp` pulls
    a READ-ONLY local clone to feed `_model_authored_test_script`; the writes, the
    suite run and the test commit happen in the Pod. Simplification versus the
    Docker/dev path: no infra-error loop and no re-run idempotency (1 authoring
    pass; empty authoring → tests_ran=False → the gate stops cleanly)."""
    import shlex as _shlex
    import subprocess as _sp

    ns = os.environ.get("DSE_SANDBOX_K8S_NAMESPACE", "dse-sandboxes")
    kubectl = os.environ.get("DSE_KUBECTL", "kubectl")
    kctx = os.environ.get("DSE_SANDBOX_KUBE_CONTEXT", "")
    kbase = [kubectl] + (["--context", kctx] if kctx else [])

    clocks = _tester_clocks()
    if clocks.clamped_from is not None:
        # The turn still runs — but a suite measured against a budget nobody
        # asked for is a result with a footnote, and the footnote has to be
        # somewhere a human can find it later.
        logger.error(
            "tester clocks CLAMPED to fit the activity: install %ds→%ds, suite %ds→%ds",
            clocks.clamped_from[0], clocks.install, clocks.clamped_from[1], clocks.suite,
        )
        audit_emit(
            actor="system:sandbox-runtime",
            action="tester_clock_clamped",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={
                "configured_install_seconds": clocks.clamped_from[0],
                "configured_suite_seconds": clocks.clamped_from[1],
                "install_seconds": clocks.install,
                "suite_seconds": clocks.suite,
                "pod_exec_seconds": clocks.pod_exec,
                "activity_budget_seconds": _outer_activity_budget_seconds(),
            },
        )

    def _pod_sh(
        script: str, *, timeout: int = _POD_CONTROL_TIMEOUT_SECONDS, input_text: str | None = None
    ) -> "_sp.CompletedProcess":
        argv = kbase + ["exec", "-i", sandbox_id, "-n", ns, "--", "sh", "-c", script]
        try:
            return _run_pod_command(argv, timeout=timeout, input_text=input_text)
        except _sp.TimeoutExpired as exc:
            # Every caller below reads a returncode; none of them survives an
            # exception (see _timed_out_process).
            return _timed_out_process(argv, exc, timeout)

    # git identity IN THE POD (where the repo actually is — the local path's
    # "git config exit 128" was exactly this running in the worker, which does
    # not have the workspace).
    _pod_sh("cd /workspace && git config user.name dse-tester && git config user.email tester@dse.local")

    # idempotency (retry): if a previous Tester round already authored tests
    # (a `tester(...)` commit) and they still exist in the Pod, RE-RUN instead of
    # re-authoring — a FIXED target so the Coder can converge (the local path
    # does the same with _tester_authored_files_in_history). Without this, each
    # retry authors a new file, the target moves and the Coder↔Tester loop never
    # closes.
    reused = [
        f for f in _pod_sh(
            "cd /workspace && git log --pretty=%H --grep='^tester(' -n 8 2>/dev/null | "
            'while read h; do git show --name-only --pretty=format: "$h" 2>/dev/null; done | '
            'sort -u | while read f; do [ -n "$f" ] && [ -f "$f" ] && echo "$f"; done'
        ).stdout.splitlines() if f.strip()
    ]

    test_files: list[str] = []
    authored_new = False
    # The Tester's own spend. Its authoring call goes through the gateway client,
    # which already writes it to model_call_ledger — but the RESULT reported a
    # hardcoded 0.0, so the workflow's per-work-item ceiling counted only the
    # Coder and the ledger had nothing to reconcile against.
    authoring_cost_usd = 0.0
    if reused:
        test_files = reused
        logger.info("tester k8s: reusing %d test(s) authored in a previous round", len(reused))
    else:
        # REAL authoring. The context is READ from the Pod, never copied out of
        # it: `kubectl cp` of /workspace dragged the post-`npm ci` tree into the
        # worker's /tmp, a 256Mi emptyDir, and kubelet evicted the Pod five
        # times in five minutes. Excluding node_modules would not have been
        # enough either — `.angular/cache` and `dist/` are in there too. The
        # prompt wants about 5 kB; `_pod_tester_context` reads exactly that,
        # each piece truncated inside the Pod.
        authoring_script: list[dict[str, Any]] | None = None
        try:
            ctx = _pod_tester_context(_pod_sh)
        except _TesterContextUnavailable as exc:
            logger.warning("tester k8s: could not read the workspace context: %.200s", str(exc)[:200])
            # The turn ends as "no tests authored" either way; without this row
            # nothing durable says the context read is the reason.
            audit_emit(
                actor="system:sandbox-runtime",
                action="tester_workspace_context_failed",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"error": f"{type(exc).__name__}: {str(exc)[:400]}"},
            )
        else:
            authoring_script, authoring_cost_usd = _model_authored_test_script(
                inp, ctx, headers, virtual_key
            )

        # write the test files IN THE POD (content via stdin — no quoting of the content)
        for s in (authoring_script or []):
            if s.get("tool") != "write_file":
                continue
            path, content = str(s.get("path") or ""), str(s.get("content") or "")
            if not path:
                continue
            w = _pod_sh(
                f'cd /workspace && mkdir -p "$(dirname {_shlex.quote(path)})" && cat > {_shlex.quote(path)}',
                input_text=content,
            )
            if w.returncode == 0:
                test_files.append(path)
                authored_new = True
            else:
                logger.warning("tester k8s: failed writing %s into the Pod: %.200s", path, (w.stderr or "")[:200])

    # run the suite IN THE POD (deterministic detection: package.json with a
    # "test" script → npm test; otherwise pytest). node/npm/pytest come from the
    # image (rebuild).
    # The suite runs UNDER `timeout` inside the Pod, and that is load-bearing.
    #
    # A test that leaves a handle open — an http server the test forgot to
    # close, a pending timer — makes the runner sit there forever. Without an
    # inner bound the only thing that ever ended the run was the 600s activity
    # timeout, which Temporal reports as TimeoutExpired with no output: the
    # Tester cannot see that its own test hangs, so on the next attempt it
    # authors ANOTHER test file, which hangs too. Observed live: six authored
    # files, one every ~10 minutes, each one a fresh name, none of them ever
    # terminating.
    #
    # Node's own guards do NOT rescue this — verified in the live sandbox:
    # `--test-timeout` and `--test-force-exit` both still hung, because they
    # bound the tests, not whatever is holding the loop open around them.
    # `timeout` from coreutils does, and it turns a hang into rc=124 with the
    # output the runner already produced.
    # The Tester's question is "do the tests I just wrote EXECUTE and pass?",
    # not "is this whole repository green" — that is L1's question, and L1 asks
    # it minutes later over the committed state (see the note below this exec).
    # Running the full suite to answer the smaller question cost the Angular
    # testbed ~400s of a ~24-minute round, and L1 then spent another ~400s on
    # the identical 4,975 tests.
    #
    # `--coverage=false` is not optional and not a weakening: this repo sets
    # `collectCoverage: true` with global thresholds, so ANY subset fails them —
    # measured at 9.83% against a floor of 80% while every test passed. Scoping
    # without it would turn a fast check into a guaranteed red one. L1 still
    # runs the whole suite WITH coverage.
    #
    # Only the npm branch is scoped. Maven's `-Dtest=` takes class names rather
    # than paths, and the Java testbed's whole suite runs in seconds, so the
    # translation would add a failure mode to buy nothing.
    scoped = " ".join(_shlex.quote(p) for p in test_files)
    npm_suite = (
        f"npm test --silent -- --coverage=false {scoped}" if scoped else "npm test --silent"
    )

    def _run_suite():
        return _pod_sh(
        'cd /workspace && '
        'if [ -f package.json ] && grep -q \'"test"\' package.json; then '
        # The install's output used to go to /dev/null under `|| true`. A
        # half-installed node_modules then broke the suite with
        # MODULE_NOT_FOUND, and the Coder was sent to fix an import the runner
        # had broken. Still non-fatal (a failed install can leave a usable
        # tree), but no longer invisible — and on stderr, which the tail below
        # keeps.
        #
        # Its own `timeout` too: sharing one exec with the suite meant the
        # install silently ate the suite's budget, and a run that died in the
        # install looked exactly like a run that died in the tests.
        f'timeout -k 10 {clocks.install} npm install --no-audit --no-fund '
        '>/tmp/dse-npm-install.log 2>&1 || '
        'echo "DSE: npm install FAILED (rc=$?). The suite ran against an INCOMPLETE '
        'node_modules — this is an install problem, not a test problem. '
        f'rc=124 means the install alone outlived its own {clocks.install}s budget. '
        'install tail: $(tail -c 800 /tmp/dse-npm-install.log)" >&2; '
        f'timeout -k 10 {clocks.suite} {npm_suite}; '
        # Maven, before falling through to pytest. Without this a Java
        # repository ran `python3 -m pytest` — which finds no tests, exits
        # non-zero, and reports as "the tests you wrote fail". Measured: the
        # backend work items died at `tester_retry_cap_exhausted` having never
        # run a single Java test, and the Coder was sent to fix an assertion
        # that never existed.
        #
        # `./mvnw` rather than `mvn`: the repo pins its own Maven in
        # .mvn/wrapper, and the image deliberately ships only a JDK so it
        # cannot disagree with that pin. `-o` is NOT used — the wrapper has to
        # fetch Maven on the first run.
        'elif [ -f pom.xml ] && [ -x ./mvnw ]; then '
        f'timeout -k 10 {clocks.suite} ./mvnw -B -q test; '
        'elif [ -f pom.xml ]; then '
        f'timeout -k 10 {clocks.suite} mvn -B -q test; '
        f'else timeout -k 10 {clocks.suite} python3 -m pytest -q; fi',
        timeout=clocks.pod_exec,
    )

    # O bloco de execução da ORDEM DE REESCRITA vivia aqui e SAIU em
    # 2026-08-10, com o reauthor inteiro. Ele existia porque o Coder não
    # podia consertar a spec do Tester: um humano (ou, na rc.76, o próprio
    # sistema) julgava que a asserção estava errada e mandava o TESTER
    # reescrevê-la. Hoje o Coder edita qualquer teste e o desvio não tem
    # razão de ser — a ordem era ceremônia para contornar uma proteção que
    # não existe mais.
    # ---- A régua do REPO, aplicada dentro do turno ------------------------
    # As specs são transpiladas pelo jest com a config DELAS
    # (`tsconfig.spec.json`: strict:false), então um erro de tipo no que o
    # Tester acabou de escrever passa aqui e só aparece um round de L1 depois —
    # ao custo de um turno de Coder para descobrir. O comando vem do
    # `.dse/validation.json`, que é a mesma régua que o L1 aplica (a do build,
    # nunca a das specs), e ausência declarada não vira veredito: repo sem
    # manifesto ou sem `typecheck` segue exatamente como antes.
    typecheck_output = ""
    tc_cmd, tc_timeout = _pod_manifest_typecheck(_pod_sh)
    if tc_cmd:
        # A instalação vem ANTES, e não é detalhe: quem instala `node_modules` é
        # o script da suíte, que roda depois daqui. Sem dependência, `npx tsc`
        # não acha o compilador local, BAIXA um pacote homônimo do npm
        # ("This is not the tsc command you are looking for") e sai != 0 — que o
        # turno reportava como erro de tipo. Medido em produção (wi_aa119e7c):
        # todo item de repo npm morreu no teto do Tester por isto.
        tc = _pod_sh(
            "cd /workspace && "
            "if [ -f package.json ] && [ ! -d node_modules ]; then "
            f"timeout -k 10 {clocks.install} npm install --no-audit --no-fund "
            ">/tmp/dse-npm-install.log 2>&1 || true; fi; "
            + _shlex.join(tc_cmd),
            timeout=tc_timeout + clocks.install,
        )
        if tc.returncode != 0:
            typecheck_output = _tail_both(tc)

    if typecheck_output:
        # Falha-rápido: uma suíte inteira depois de um erro de tipo custa
        # minutos para dizer o que o compilador já disse em segundos.
        run = tc
    else:
        run = _run_suite()

    # ---- Porta 5: o alvo fixo vale apenas para alvo que produz VEREDITO ----
    # O racional do reuso fica de pé (alvo estável para o Coder convergir; ver
    # o comentário do `reused` acima). Mas um alvo cuja suite morre na CARGA ou
    # na COMPILAÇÃO — zero asserções avaliadas — não é alvo: reprova `test` e
    # `build` para sempre, o Coder não pode tocá-lo (revert determinístico) e
    # cada re-run custa uma rodada inteira (medido: wi_5620d2c1 @MockBean,
    # wi_8edaef39 @ngx-translate herdado — ambos mortos no teto). Só nesse
    # caso o Tester re-autora: IN-PLACE, POR ARQUIVO, no máximo uma vez por
    # turno. Asserção falhando nunca entra aqui —
    # é veredito, e veredito é do L1/humano (deferral inalterado).
    repaired_files: list[str] = []
    if reused and run.returncode != 0:
        _suite_out = (run.stdout or "") + (run.stderr or "")
        # A checagem de posse por git SAIU em 2026-08-10 com o oráculo de
        # autoria. Ela perguntava "algum sujeito humano na história?" como
        # PROXY para "este arquivo é meu" — e o escopo já era a resposta
        # direta: `test_files` é a lista de alvos DESTE turno do Tester. Além
        # de redundante, o proxy errava com clone raso (`--depth 50`), em que
        # o histórico humano fica fora da janela.
        owned_broken = _zero_verdict_specs(_suite_out, test_files)
        if owned_broken:
            try:
                # A primitiva de evidência do defeito B: o "zero veredito" que
                # autorizou o reparo fica auditável em números, não em adjetivo.
                from dse_validation.l1.quality_checks import _test_counts
                _counts = _test_counts(_suite_out)
                executed_before = _counts.executed if _counts else 0
            except Exception:  # noqa: BLE001 — evidência é telemetria; nunca decide o reparo
                executed_before = 0
            feedback = (
                "Your OWN spec file(s) fail BEFORE running a single test — a broken "
                "import or compile error, NOT a failing assertion. Rewrite EXACTLY "
                f"these files, at these exact paths, fixing the load error: "
                f"{', '.join(owned_broken)}\n{_suite_out[-1500:]}"
            )
            try:
                repair_ctx = _pod_tester_context(_pod_sh)
            except _TesterContextUnavailable as exc:
                repair_ctx = None
                logger.warning("porta 5: contexto indisponível para o reparo: %.200s", str(exc)[:200])
            if repair_ctx is not None:
                repair_script, repair_cost = _model_authored_test_script(
                    inp, repair_ctx, headers, virtual_key, error_feedback=feedback
                )
                authoring_cost_usd += repair_cost
                for s in (repair_script or []):
                    if s.get("tool") != "write_file":
                        continue
                    path, content = str(s.get("path") or ""), str(s.get("content") or "")
                    # Filtro determinístico (P1): SÓ os arquivos quebrados, nos
                    # caminhos exatos — um arquivo novo aqui seria a regressão
                    # das cópias -dse voltando pela porta da frente.
                    if path not in owned_broken or not content:
                        continue
                    w = _pod_sh(
                        f'cd /workspace && cat > {_shlex.quote(path)}',
                        input_text=content,
                    )
                    if w.returncode == 0:
                        repaired_files.append(path)
                if repaired_files:
                    audit_emit(
                        actor="system:sandbox-runtime",
                        action="tester_spec_repaired",
                        tenant_id=inp.tenant_id,
                        work_item_id=inp.work_item_id,
                        details={"files": repaired_files, "reason": "zero_verdict",
                                 "executed_before": executed_before,
                                 "cost_usd": repair_cost},
                    )
                    run = _run_suite()

    tests_ran = bool(test_files)
    # The suite result is INFORMATION here, not a verdict.
    #
    # L1's `test` gate runs the same command, in the same Pod, over the same
    # workspace, minutes later — and it judges the COMMITTED state, which is
    # what a pull request contains. Two gates over one suite is not twice the
    # safety: the first one fires earlier and spends the SHARED
    # `coder_retry_count`, so the second never runs at all.
    #
    # Measured across four work items: every single one died at
    # `tester_retry_cap_exhausted` with `l1_completed` = 0. The real gate never
    # saw the code. The one item that got past the Tester reached L1 on its
    # first attempt, and L1 immediately did its job — 4 type errors and a
    # failing build in the generated code.
    #
    # `suite_deferred` rather than `tests_passed=True`: a forced green is
    # indistinguishable from a suite that really passed, and would be a lie the
    # ledger keeps. This says no verdict was taken here, which is true.
    returncode = run.returncode
    # `typecheck_failed` é veredito sobre o CÓDIGO (compilador), não sobre a
    # suíte: não entra em _SUITE_OUTCOME_INFRA (não é o runtime morrendo) e não
    # é deferível (o deferral cobre só `tests_failed`, o desacordo entre teste e
    # código que o L1 re-julga sobre o estado commitado).
    outcome = (
        "typecheck_failed" if typecheck_output
        else _suite_outcome(tests_ran=tests_ran, returncode=returncode)
    )
    # Deferral covers ONLY a plain test failure — the tests disagreeing with the
    # code, which is precisely what L1 will re-judge over the committed state.
    #
    # An infrastructure ending is not deferred and never was: `suite_hung`
    # (rc=124), `resource_kill` (rc=137) and `exec_timeout` mean the runtime
    # died, and the Tester's clock is the only one that catches a hang before
    # L1's much longer one. Those still escalate, and `tests_passed` still
    # reads False, because writing True there would put a lie in the ledger.
    suite_deferred = _suite_verdict_deferred() and outcome == "tests_failed"
    tests_passed = tests_ran and (suite_deferred or returncode == 0)
    suite_hung = outcome == "suite_hung"
    # Kept, not just logged. The workflow feeds this back to the Coder on the
    # next attempt; without it the retry repeats the original instruction
    # verbatim and the same test fails again. Tail, because the useful part of a
    # test runner's output is the end.
    failure_output = ""
    # An infra kill is reported even with no authored test: it is the process
    # that died, and "no tests ran" on its own would read like the model simply
    # produced nothing.
    # `suite_deferred` is in the condition because a deferred run reports
    # tests_passed=True — and the output is still what an operator reads to see
    # what the suite actually said. Deferring the VERDICT must not delete the
    # EVIDENCE.
    if (not tests_passed or suite_deferred) and (tests_ran or outcome in _SUITE_OUTCOME_INFRA):
        failure_output = ((run.stdout or "") + (run.stderr or ""))[-_FAILURE_OUTPUT_CHARS:]
        # Say it in words the next round will act on. The raw output of a killed
        # suite ends mid-run and reads like a pass, which is precisely how six
        # non-terminating files got authored in a row.
        failure_output = _infra_outcome_note(
            outcome, returncode, suite_timeout_seconds=clocks.suite
        ) + failure_output
        # 1200, not 300: at 300 the cut landed mid-token ("e: 'suite'") and the
        # actual assertion never appeared.
        logger.warning("tester k8s: suite %s (rc=%s): %s", outcome, returncode,
                       failure_output[-_FAILURE_OUTPUT_AUDIT_CHARS:])

    # commit the test files IN THE POD (current branch; the post-tester checkpoint
    # picks them up and finalize pushes). No push here.
    head_sha = None
    # Reparo (porta 5) também commita: o L1 julga o estado COMMITADO — um
    # reparo só no working tree passaria no re-run do Tester e reprovaria no L1.
    # Re-autoria por ordem humana idem, pela mesma razão.
    if (authored_new or repaired_files) and test_files:
        files_arg = " ".join(_shlex.quote(p) for p in test_files)
        msg = f"tester({inp.work_item_id}): {(inp.instruction or '')[:60]}"
        _pod_sh(
            f"cd /workspace && git add {files_arg} && git commit -m {_shlex.quote(msg)} || true",
            timeout=_POD_CONTROL_TIMEOUT_SECONDS,
        )
    h = _pod_sh("cd /workspace && git rev-parse HEAD", timeout=_HEAD_READ_TIMEOUT_SECONDS)
    head_sha = (h.stdout or "").strip() or None

    audit_emit(
        actor="system:sandbox-runtime",
        action="tester_turn_completed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "stage": "tester", "runtime": "k8s", "test_files": test_files,
            "tests_ran": tests_ran, "tests_passed": tests_passed, "returncode": returncode,
            # The fact that reconciles tests_passed=True with outcome=
            # tests_failed in the SAME row. Without it the ledger reads as a
            # contradiction (7/7 events in two production runs did).
            "suite_deferred": suite_deferred,
            # WHY it failed, not just THAT it failed. Empty on success.
            "failure_output": failure_output[-_FAILURE_OUTPUT_AUDIT_CHARS:],
            "suite_hung": suite_hung,
            # The class of the ending, queryable: `WHERE details->>'outcome' =
            # 'resource_kill'` is how an OOM is told from a broken assertion
            # without reading 4 KB of runner output.
            "outcome": outcome,
            # The budget this run was actually measured against. It is
            # configuration now, so `suite_hung` without it is unreadable a week
            # later: nobody can tell a hang from a ConfigMap that was too tight.
            "suite_timeout_seconds": clocks.suite,
            "cost_usd": authoring_cost_usd,
        },
    )
    return TesterTurnResult(
        sandbox_id=inp.work_item_id,
        test_files=test_files,
        tests_ran=tests_ran,
        tests_passed=tests_passed,
        suite_deferred=suite_deferred,
        returncode=returncode,
        head_sha=head_sha,
        cost_usd=authoring_cost_usd,
        failure_output=failure_output,
        # An infra ending is ERROR, not FAIL: FAIL is a verdict on the code under
        # test, and it is what makes the failure read as an assertion to fix.
        # The contract's validator derives the rest (PASS/NOT_CONFIGURED).
        status=GateStatus.ERROR if outcome in _SUITE_OUTCOME_INFRA else None,
    )


async def _run_tester_turn_pod(
    inp: "RunTesterTurnInput", *, sandbox_id: str, headers: Any, virtual_key: str, push: bool = True,
) -> TesterTurnResult:
    """Tester on the K8s runtime (Pod). Runs the synchronous body under heartbeat
    — the model's authoring + `npm install`/`npm test` can take minutes."""
    return await run_sync_with_heartbeat(
        lambda _c: _tester_pod_sync(inp, sandbox_id, headers, virtual_key, push),
        None,
        stage=Stage.tester.value,
        work_item_id=inp.work_item_id,
        operation="tester_pod_bridge",
    )


async def _run_tester_turn_impl(
    inp: RunTesterTurnInput,
    *,
    retrieval: RetrievalService | None = None,
    authoring_script: list[dict[str, Any]] | None = None,
    push: bool = True,
) -> TesterTurnResult:
    """Tester session (WSC-E3-T4): test authoring + runners. Edits are allowed
    ONLY under test paths (`TesterToolset` refuses writes outside them). The
    written tests actually EXECUTE (`run_tests` → real pytest in the workspace),
    they are not merely generated. The commit/push of the test files is
    deterministic (`ScopedGitSession`), never done by the LLM (P1)."""
    branch = inp.branch or _default_branch(inp.work_item_id)
    workspace_dir, _bare = _paths_for(inp.work_item_id)

    headers = GatewayCallHeaders(
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        stage=Stage.tester,
        task_class=inp.task_class,
        data_class=inp.data_class,
    )
    vk = mint_virtual_key(headers)

    # K8s runtime: the workspace lives INSIDE the Pod (the Coder ran there via
    # RemoteSubstrate), not on the worker's FS. The local path below
    # (ScopedGitSession/ScriptedAgentSession) assumes a host-visible workspace and
    # breaks on K8s (git config exit 128, no workspace). Route to the bridge that
    # operates INSIDE the Pod via kubectl exec.
    try:
        _driver = select_sandbox_driver()
        _host_visible = _driver.workspace_is_host_visible
    except Exception:  # noqa: BLE001 — no resolvable driver → keep the local path
        _driver, _host_visible = None, True
    if _driver is not None and not _host_visible:
        return await _run_tester_turn_pod(
            inp,
            sandbox_id=_driver.sandbox_id_for(inp.work_item_id),
            headers=headers,
            virtual_key=vk.virtual_key,
            push=push,
        )

    # Beco 1: a ordem de re-autoria só é executada no runtime K8s (o parque e
    # o veredito são de produção). DAQUI para baixo é o caminho genuinamente
    # local — aqui ela seria descartada em silêncio, e o ledger tem que dizer
    # por que a spec voltou intacta. (A primeira versão deste guard ficava
    # ANTES do dispatch e disparava mislabeled em turno K8s — telemetria que
    # mente custou vinte minutos de caça no wi_6f00bf0a.)

    # REAL authoring (same selector as the planner, P1 by config): with no
    # explicit script (tests) and a real substrate, the MODEL writes the tests.
    # A failure at any point → empty script → tests_ran=False → the gate stops.
    #
    # INFRA validation with 1 re-authoring pass (found in the real run: the model
    # wrote Jest in a node:test repo — the test never executed and the fix cycle
    # kept re-running the CODER, which cannot even touch tests → a loop with no
    # exit): write, run ONLY the new files; an import/syntax error → re-author
    # ONCE with the error in the prompt; if it persists → remove the files and
    # return tests_ran=False (the gate stops cleanly instead of burning Coder
    # turns).
    #
    # The Tester's own spend, summed over BOTH authoring attempts: the gateway
    # client already wrote each call to model_call_ledger, but the result
    # reported a hardcoded 0.0, so the workflow's per-work-item ceiling counted
    # only the Coder.
    authoring_cost_usd = 0.0
    if authoring_script is None and os.environ.get(SUBSTRATE_ENV_VAR, "fake").strip().lower() != "fake":
        # Tester idempotency (2nd real run): tests already authored in a previous
        # cycle are RE-RUN, never re-authored — the fix cycle needs a fixed target
        # for the Coder to converge.
        reused = _tester_authored_files_in_history(workspace_dir)
        if reused:
            audit_emit(
                actor="system:sandbox-runtime",
                action="tester_reused_authored_tests",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"test_files": reused[:10]},
            )
            authoring_script = [{"tool": "run_tests"}]
    if authoring_script is None and os.environ.get(SUBSTRATE_ENV_VAR, "fake").strip().lower() != "fake":
        error_feedback = ""
        for attempt in (1, 2):
            authoring_script, attempt_cost_usd = await run_sync_with_heartbeat(
                lambda _c: _model_authored_test_script(
                    inp, _local_tester_context(workspace_dir), headers, vk.virtual_key,
                    error_feedback=error_feedback,
                ),
                None,
                stage=Stage.tester.value,
                work_item_id=inp.work_item_id,
                operation=f"tester_authoring_{attempt}",
            )
            # Before the break: an attempt that produced nothing usable was
            # still billed.
            authoring_cost_usd += attempt_cost_usd
            if not authoring_script:
                break
            new_paths = [s["path"] for s in authoring_script if s.get("tool") == "write_file"]
            # write directly (the same paths the toolset would accept — already filtered)
            for s in authoring_script:
                if s.get("tool") == "write_file":
                    dest = os.path.join(workspace_dir, s["path"])
                    os.makedirs(os.path.dirname(dest) or workspace_dir, exist_ok=True)
                    with open(dest, "w") as fh:
                        fh.write(s["content"])
            infra_err = _authored_test_infra_error(workspace_dir, new_paths)
            if infra_err is None:
                # valid files: the step loop below only needs to register
                # test_files + run the suite (the writes already happened).
                authoring_script = (
                    [{"tool": "write_file", "path": p, "content": open(os.path.join(workspace_dir, p)).read()} for p in new_paths]
                    + [{"tool": "run_tests"}]
                )
                break
            logger.warning("authored test hit an INFRA error (attempt %d): %.200s", attempt, infra_err)
            for p in new_paths:  # remove the junk before re-authoring/giving up
                try:
                    os.remove(os.path.join(workspace_dir, p))
                except OSError:
                    pass
            error_feedback = infra_err
            authoring_script = None
        if authoring_script is None and error_feedback:
            audit_emit(
                actor="system:sandbox-runtime",
                action="tester_authoring_invalid",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"infra_error": error_feedback[:500]},
            )

    # Phase 3 (WSC-E3-T4b): the toolset is scoped to the work item — besides test
    # paths, `demos/<work_item_id>/` is an allowed write (the `@demo` test
    # convention); ANOTHER work item's `demos/` stays blocked.
    session = ScriptedAgentSession(
        toolset=TesterToolset(work_item_id=inp.work_item_id),
        workspace_dir=workspace_dir,
        retrieval=retrieval,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
    )

    test_files: list[str] = []
    tests_ran = False
    tests_passed = False
    returncode = -1
    failure_output = ""
    for index, step in enumerate(authoring_script or [], start=1):
        res = await run_sync_with_heartbeat(
            session.invoke,
            step["tool"],
            stage=Stage.tester.value,
            work_item_id=inp.work_item_id,
            operation=f"tester_tool_{index}_{step['tool']}",
            **{k: v for k, v in step.items() if k != "tool"},
        )
        if step["tool"] == "write_file":
            test_files.append(step["path"])
        if step["tool"] == "run_tests":
            tests_ran = True
            tests_passed = bool(res.detail.get("passed"))
            returncode = int(res.detail.get("returncode", -1))
            if not tests_passed:
                # Same reason as the k8s path: the retry is useless without it.
                # Whichever of these the toolset filled is what the Coder gets.
                raw = (
                    res.detail.get("output")
                    or (str(res.detail.get("stdout") or "") + str(res.detail.get("stderr") or ""))
                )
                failure_output = str(raw)[-_FAILURE_OUTPUT_CHARS:]

    # BEFORE the hygiene block: `post-checkout` fires on path-limited
    # checkouts too (measured), and the hygiene steps run
    # `git checkout -- <lockfile>` moments after the turn's own `npm install`
    # repointed core.hooksPath at `.husky/` — the same window the OOM loop
    # came out of, one call earlier.
    git_session = ScopedGitSession(workspace_dir=workspace_dir, branch=branch)
    git_session.ensure_identity(name="dse-tester", email="tester@dse.local")

    _restore_lockfile_churn_audited(workspace_dir, inp.tenant_id, inp.work_item_id, stage="tester")

    # Deterministic commit/push of the test files (only test paths were written —
    # the toolset guaranteed it). Git escapes live in the code, never in the LLM.
    if git_session.has_changes():
        git_session.commit(f"tester({inp.work_item_id}): {inp.instruction[:60]}")
        if push:
            try:
                git_session.push()
            except GitScopeViolation:
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="tester_push_rejected",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"branch": branch},
                )
                raise

    audit_emit(
        actor="system:sandbox-runtime",
        action="tester_turn_completed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "stage": "tester",
            "test_files": test_files,
            "tests_ran": tests_ran,
            "tests_passed": tests_passed,
            "returncode": returncode,
            "virtual_key_fixture": vk.fixture,
            # WHY it failed, not just THAT it failed. Empty on success.
            "failure_output": (failure_output or "")[-_FAILURE_OUTPUT_AUDIT_CHARS:],
            "cost_usd": authoring_cost_usd,
        },
    )
    return TesterTurnResult(
        sandbox_id=inp.work_item_id,
        test_files=test_files,
        tests_ran=tests_ran,
        tests_passed=tests_passed,
        returncode=returncode,
        cost_usd=authoring_cost_usd,
        failure_output=failure_output,
    )


# ---------------------------------------------------------------------------
# run_l2_review (WSC-E3-T5) — fresh-context Reviewer session, returns L2Verdict
# ---------------------------------------------------------------------------
# PROMOTED to the contract (addendum 02 §2.3) and HARDENED there: the canonical
# definition in `dse_contracts.activities` now has `extra="forbid"` — trying to
# pass any field beyond {work_item_id, tenant_id, plan, diff, task_class,
# data_class} (e.g. the Coder's history) fails at the Activity's DECODE, not
# just in a test. Structural P3 in the foundation.
from dse_contracts import RunL2ReviewInput  # noqa: E402


def _changed_files_from_diff(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:].strip())
        elif line.startswith("diff --git a/"):
            # "diff --git a/x b/x"
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.append(parts[1].strip())
    # dedup preserving order
    seen: set[str] = set()
    out = []
    for f in files:
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _default_reviewer_verdict(ctx: ReviewerContext):
    """Deterministic STAND-IN reviewer (a clearly flagged fixture, same spirit as
    FakeSubstrate). Judges the diff's adherence to the plan by objective rules:
    (a) no file changed outside the declared blast radius (`expected_files`, when
    non-empty); (b) no file under `forbidden_paths`. In production a FRESH
    OpenHands session (plan+diff only) replaces this and returns
    convention/logic objections with file/line — override via
    `_run_l2_review_impl(..., verdict_fn=...)`. See the README."""
    changed = _changed_files_from_diff(ctx.diff)
    objections: list[str] = []
    expected = set(ctx.plan.expected_files)
    for f in changed:
        if expected and f not in expected:
            objections.append(f"{f}: changed outside the blast radius declared in the plan (expected_files)")
        for fb in ctx.plan.forbidden_paths:
            if f.startswith(fb.rstrip("*")):
                objections.append(f"{f}: touches forbidden_path '{fb}' — requires the human path")
    return (len(objections) == 0, objections, 0.0)


@activity.defn(name=ACTIVITY_RUN_L2_REVIEW)
async def run_l2_review(inp: RunL2ReviewInput) -> L2Verdict:
    reject_local_agent_execution("reviewer")
    return await _run_l2_review_impl(inp)


async def _run_l2_review_impl(inp: RunL2ReviewInput, *, verdict_fn=None) -> L2Verdict:
    """Build the FRESH-context Reviewer session (WSC-E3-T5) and return the
    `L2Verdict`. The session receives ONLY `ReviewerContext(plan, diff)` — never
    the Coder's history (P3). The verdict is a RECOMMENDATION (it gates
    progression); the merge remains human (P1)."""
    context = ReviewerContext(work_item_id=inp.work_item_id, plan=inp.plan, diff=inp.diff)
    session = FreshReviewerSession(context)
    verdict = await run_sync_with_heartbeat(
        session.review,
        verdict_fn or _default_reviewer_verdict,
        stage=Stage.reviewer.value,
        work_item_id=inp.work_item_id,
        operation="reviewer_verdict",
    )

    audit_emit(
        actor="system:sandbox-runtime",
        action="l2_review_completed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "stage": "reviewer",
            "passed": verdict.passed,
            "objections": verdict.objections,
            "fresh_context": True,
            "context_fields": sorted(type(context).__dataclass_fields__.keys()),
        },
    )
    return verdict


# ===========================================================================
# Phase 4 — skill promotion pipeline (WSC-E4-T3). Activities registered as
# `@activity.defn` with the names/types from `dse_contracts.activities` (defined
# at the Phase 4 entry gate, before the build). The deterministic logic lives in
# `skill_promotion` (P1); here we only have the Activity wrapper + the
# translation into the contract's return models.
# ===========================================================================
from dse_contracts import (  # noqa: E402
    ACTIVITY_EVAL_SKILL_CANDIDATE,
    ACTIVITY_PROMOTE_SKILL,
    EvalSkillCandidateInput,
    EvalSkillCandidateResult,
    PromoteSkillInput,
    PromoteSkillResult,
)

from . import skill_promotion  # noqa: E402


@activity.defn(name=ACTIVITY_EVAL_SKILL_CANDIDATE)
async def eval_skill_candidate(inp: EvalSkillCandidateInput) -> EvalSkillCandidateResult:
    """Replay the candidate against the historical eval set
    (positives/negatives). Deterministic (P1) — it produces a SCORE and the
    counts, never a promotion decision. `negative_regressions>0` ⇒
    `passed=False`, which blocks the candidate→approved transition by
    construction (the gate is in `promote_skill`). Writes to `skill_eval` (P8)."""
    outcome = skill_promotion.evaluate_candidate(
        inp.tenant_id, inp.skill_key, inp.candidate_version
    )
    return EvalSkillCandidateResult(
        skill_key=inp.skill_key,
        candidate_version=inp.candidate_version,
        passed=outcome.passed,
        score=outcome.score,
        positive_hits=outcome.positive_hits,
        negative_regressions=outcome.negative_regressions,
        detail=outcome.detail,
    )


@activity.defn(name=ACTIVITY_PROMOTE_SKILL)
async def promote_skill(inp: PromoteSkillInput) -> PromoteSkillResult:
    """GOVERNED state transition (candidate→approved→canary→active + rollback).
    NON-NEGOTIABLE P1/P3: `to_status in {approved,active}` without a human
    `approver` raises `ApproverRequired` BEFORE any write — promotion without a
    named human is impossible by construction (there is no code path; the
    Activity propagates the exception and WS-B's workflow never "falls" into a
    silent promotion). Every transition → dse_audit.emit with the approver's
    identity."""
    outcome = skill_promotion.promote(
        inp.tenant_id,
        inp.skill_key,
        inp.version,
        inp.to_status,
        approver=inp.approver,
        reason=inp.reason,
    )
    detail = ""
    if outcome.superseded_version is not None:
        detail = f"superseded v{outcome.superseded_version}"
    if outcome.restored_version is not None:
        detail = f"rollback: restored v{outcome.restored_version} to active"
    return PromoteSkillResult(
        skill_key=outcome.skill_key,
        version=outcome.version,
        from_status=outcome.from_status,
        to_status=outcome.to_status,
        ok=True,
        detail=detail,
    )


# Preflight at the moment the worker imports/registers the Activities. In
# production, the current adapter honestly declares that it does not yet execute
# stages inside the sandbox; so the worker refuses to boot instead of operating
# on the local fallback. In dev/test the existing compatibility is preserved.
validate_runtime_startup(
    isolated_stage_execution_available=(
        DEFAULT_SANDBOX_DRIVER.supports_isolated_stage_execution
    )
)


# Consumed by the single worker's defensive loader (services/orchestrator/
# src/dse_orchestrator/worker.py:_load_cross_workstream_activities) — the name
# `ACTIVITIES` and the contract the integrator expects (see its docstring).
ACTIVITIES = [
    provision_sandbox,
    checkpoint_sandbox,
    rebuild_sandbox,
    teardown_sandbox,
    run_coder_turn,
    run_planner_turn,
    run_tester_turn,
    run_l2_review,
    eval_skill_candidate,
    promote_skill,
]
