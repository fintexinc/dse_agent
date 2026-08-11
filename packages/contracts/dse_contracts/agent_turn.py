"""Typed contract for isolated agent-turn execution (plan 09, Phase 1).

Invariant 2 of the REMEDIATION-CANONICAL-SPEC: "Agent SDK code and untrusted
tools execute only in a stage-scoped sandbox. The Temporal worker dispatches a
typed execution contract; it does not call the agent SDK in its own process."

This module IS that contract: the worker serializes an `AgentTurnRequest`, the
sandbox driver hands it to the agent-runner INSIDE the container/pod
(`docker exec -i` in dev, `kubectl exec -i` in the cluster), and the runner
returns an `AgentTurnResult`. Rules:

  - `extra="forbid"` on both sides: an unknown payload is a clean failure (P6),
    never a silently ignored field.
  - NO host path crosses the boundary: `workspace_dir` is the path INSIDE the
    sandbox (by convention `/workspace`, the driver's bind/emptyDir).
  - NO long-lived credential: `gateway.virtual_key` is the ephemeral per-task
    key minted by WS-D; the runner never sees the master key/provider key.
  - Shape evolution is ADDITIVE (spec §5): a new field has a safe default and
    `schema_version` allows refusing a future-version payload with a clean
    error.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

AGENT_TURN_SCHEMA_VERSION = 1

# Substrates the runner knows about. "fake" exists for conformance/tests — the
# runtime_profile still refuses fake in production (require_real_substrate).
KNOWN_SUBSTRATES = ("fake", "claude-agent", "openhands")


class AgentTurnGateway(BaseModel, extra="forbid"):
    """Gateway-only triangle (P1): model-gateway base_url REACHABLE FROM INSIDE
    the sandbox (internal network), the ephemeral virtual key, and the headers
    required by the WS-D contract (`GatewayCallHeaders.to_http_headers()`)."""

    base_url: str
    virtual_key: str
    headers: dict[str, str] = Field(default_factory=dict)


class AgentTurnRequest(BaseModel, extra="forbid"):
    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    work_item_id: str
    tenant_id: str
    stage: str  # "coder" | "tester" (Stage vocabulary from gateway_contract)
    substrate: str  # one of KNOWN_SUBSTRATES
    instruction: str
    model: str | None = None
    # File-editing-ONLY toolset (P1) — the default mirrors
    # ClaudeAgentSubstrate.DEFAULT_ALLOWED_TOOLS; git/PR/bash never get in.
    allowed_tools: list[str] = Field(
        default_factory=lambda: ["Read", "Write", "Edit", "Glob", "Grep"]
    )
    # Path INSIDE the sandbox (driver convention), never a host path.
    workspace_dir: str = "/workspace"
    timeout_seconds: float = 900.0
    gateway: AgentTurnGateway
    # ONLY for substrate="fake" (conformance/tests): the FakeSubstrate script.
    fake_script: list[dict] | None = None


class WorkspaceBootstrapRequest(BaseModel, extra="forbid"):
    """Lifecycle op executed INSIDE the sandbox (runner `--op bootstrap`):
    materializes the task's git workspace on the target runtime. On Docker the
    checkpoint is the bind mount `/checkpoint.git`; on K8s, the Pod volume.
    The scoping pre-receive hook (single branch, no force-push) is installed on
    the bare repo BEFORE the first push — the enforcement lives on the remote.

    `repo` (e.g. "andre2654/fintex-wallet"): when present, the bootstrap CLONES
    the real repo from inside the Pod, through the egress-proxy (HTTPS_PROXY),
    instead of starting an empty workspace — the token never enters the Pod
    (the proxy injects the credential; public repo via anonymous CONNECT).
    Absent → empty workspace (original mechanics / tests)."""

    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    work_item_id: str
    branch: str
    base_branch: str = "main"
    repo: str | None = None
    repo_host: str = "github.com"
    workspace_dir: str = "/workspace"
    checkpoint_path: str = "/checkpoint.git"
    provision_checkpoint: bool = True


class WorkspaceBootstrapResult(BaseModel, extra="forbid"):
    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    sha: str = ""
    created: bool = False  # False = workspace/branch already existed (idempotent)
    error: str | None = None
    error_kind: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_kind is not None


class CheckpointOpRequest(BaseModel, extra="forbid"):
    """Op `--op checkpoint`: commit (if there are changes) + push of the task
    branch to the checkpoint — fixed refspec, never force."""

    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    work_item_id: str
    branch: str
    phase: str
    workspace_dir: str = "/workspace"


class CheckpointOpResult(BaseModel, extra="forbid"):
    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    sha: str = ""
    phase: str = ""
    error: str | None = None
    error_kind: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_kind is not None


class PostTurnRequest(BaseModel, extra="forbid"):
    """Op `--op post_turn`: the deterministic hygiene + commit/push of the
    Coder turn executed INSIDE the sandbox (K8s runtime, where the worker
    cannot see the workspace). Same sequence as the worker on the Docker
    runtime: prune disposables → restore lockfile churn → revert test edits →
    scoped commit/push. The LLM never takes part (P1)."""

    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    work_item_id: str
    branch: str
    turn_start_sha: str
    commit_message: str
    expected_files: list[str] = Field(default_factory=list)
    workspace_dir: str = "/workspace"


class PostTurnResult(BaseModel, extra="forbid"):
    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    sha: str = ""
    files_changed: list[str] = Field(default_factory=list)
    pruned: list[str] = Field(default_factory=list)
    kept_out_of_plan: list[str] = Field(default_factory=list)
    restored_lockfiles: list[str] = Field(default_factory=list)
    error: str | None = None
    error_kind: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_kind is not None


class AgentTurnResult(BaseModel, extra="forbid"):
    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    done: bool
    # Compact turn diagnostics (never a flow decision — P1): a sample of
    # thoughts/tool calls for audit/debug, capped by the runner.
    thoughts: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    # Clean runner failure (P6): structured error, never truncated stdout.
    # error_kind is a closed vocabulary so the worker classifies without
    # substring matching:
    # "unsupported_substrate" | "invalid_payload" | "substrate_error" | "timeout"
    error: str | None = None
    error_kind: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_kind is not None
