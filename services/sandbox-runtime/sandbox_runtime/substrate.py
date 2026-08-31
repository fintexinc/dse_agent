"""Substrate orchestrator interface + adapters (WSC-E3-T1).

`AgentSubstrate` is the contract any agent runtime (OpenHands today; another
substrate tomorrow) must implement to be plugged into the `run_coder_turn`
Activity. Important (P1 — no flow decision made by an LLM): the substrate ONLY
EDITS FILES in the workspace. It never receives a git push/PR tool — deciding to
commit and where to push is deterministic code in `activities.py` (via
`scoped_git.ScopedGitSession`), after the turn ends.

Every model call from the substrate must go out through the model-gateway
(`dse_contracts.gateway_contract.GatewayCallHeaders` + the virtual key minted by
`model_gateway_client.mint_virtual_key`) — never a direct provider SDK. This is
enforced both by the `OpenHandsSubstrate` wiring (the LLM is always constructed
with `base_url=<gateway>`) and by the egress-proxy (WS-C E2), which would block
it even if someone tried to work around that.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from dse_contracts import CoderTurnResult, GatewayCallHeaders

from .runtime_profile import validate_runtime_profile


@dataclass
class TurnLog:
    instruction: str
    thoughts: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    done: bool = True


@runtime_checkable
class AgentSubstrate(Protocol):
    """Minimal contract every Coder agent runtime must fulfill."""

    def create_session(
        self,
        *,
        work_item_id: str,
        workspace_dir: str,
        gateway_headers: GatewayCallHeaders,
        virtual_key: str,
        gateway_base_url: str,
    ) -> None:
        ...

    def run_turn(self, instruction: str) -> TurnLog:
        ...

    def collect_artifacts(self) -> CoderTurnResult:
        ...


class FakeSubstrate:
    """Deterministic in-memory substrate for tests (WSC-E3-T1).

    It calls no LLM and makes no network request — it applies a pre-scripted list
    of file edits (`script`), turn by turn. It exists to test all of
    `run_coder_turn`'s plumbing (real sandbox + real scope-limited git) without
    depending on WS-D's model-gateway being up, and without spending real
    inference.
    """

    def __init__(self, script: list[dict[str, Any]]):
        self._script = script
        self._turn_idx = 0
        self.workspace_dir: str | None = None
        self.sandbox_id: str = ""
        self._cost_usd = 0.0
        self._tokens_in = 0
        self._tokens_out = 0
        self._files_changed: set[str] = set()

    def create_session(
        self,
        *,
        work_item_id: str,
        workspace_dir: str,
        gateway_headers: GatewayCallHeaders,
        virtual_key: str,
        gateway_base_url: str,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.sandbox_id = work_item_id
        # Note: FakeSubstrate deliberately does NOT use
        # virtual_key/gateway_base_url (it makes no model call at all) — but it
        # stores them so tests can prove the Activity always supplies them, even
        # when the substrate does not use them.
        self._gateway_headers = gateway_headers
        self._virtual_key = virtual_key
        self._gateway_base_url = gateway_base_url

    def run_turn(self, instruction: str) -> TurnLog:
        if self._turn_idx >= len(self._script):
            return TurnLog(instruction=instruction, done=True)
        step = self._script[self._turn_idx]
        self._turn_idx += 1
        assert self.workspace_dir is not None, "create_session must be called before run_turn"
        for rel_path, content in step.get("write_files", {}).items():
            p = Path(self.workspace_dir) / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            self._files_changed.add(rel_path)
        self._cost_usd += float(step.get("cost_usd", 0.01))
        self._tokens_in += int(step.get("tokens_in", 100))
        self._tokens_out += int(step.get("tokens_out", 50))
        return TurnLog(
            instruction=instruction,
            thoughts=[step.get("thought", "")],
            tool_calls=[f"write_file:{p}" for p in step.get("write_files", {})],
            done=step.get("done", True),
        )

    def collect_artifacts(self) -> CoderTurnResult:
        return CoderTurnResult(
            sandbox_id=self.sandbox_id,
            diff_summary=f"{len(self._files_changed)} file(s) changed by FakeSubstrate",
            files_changed=sorted(self._files_changed),
            cost_usd=round(self._cost_usd, 4),
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )


class SubstrateUnavailable(Exception):
    """Raised when the requested substrate's SDK is not installed in the current
    venv (base class for the per-adapter variants)."""


class OpenHandsSubstrateUnavailable(SubstrateUnavailable):
    """Raised when `openhands-sdk` is not installed in the current venv."""


class ClaudeAgentSubstrateUnavailable(SubstrateUnavailable):
    """Raised when `claude-agent-sdk` is not installed in the current venv."""


class OpenHandsSubstrate:
    """Real adapter over the OpenHands `software-agent-sdk` (PyPI package
    `openhands-sdk`, verified available in this session via
    `pip install openhands-sdk` — v1.21.0 at the time this code was written).

    Production wiring (P1: never a direct provider SDK):
      `openhands.sdk.LLM(base_url=<model-gateway>, api_key=<virtual_key>,
       extra_headers=GatewayCallHeaders(...).to_http_headers())`
    — the OpenHands LLM never points at a provider (Anthropic/OpenAI/Bedrock)
    directly; always at WS-D's model-gateway. Even if the code tried to, the
    egress-proxy (E2) would block the network route.

    NOT exercised through a full turn in this test suite: actually running
    `Conversation.run()` triggers real inference, which requires (a) WS-D's
    model-gateway up and answering, and (b) a valid virtual key pointing at a
    real provider configured in LiteLLM — neither is available in this parallel
    development session. The test
    `test_substrate.py::test_openhands_substrate_wiring` covers only
    CONSTRUCTION (LLM/Agent/Workspace pointing at the right places), using
    `create_session`, which performs no network I/O by itself.

    Production should also swap `LocalWorkspace` (which executes tools on the
    local filesystem of the process running the SDK) for a `RemoteWorkspace`
    pointing at an `openhands-agent-server` running INSIDE the container
    provisioned by `docker_driver.py` — so that the agent's tool execution
    (bash, file editing) genuinely happens inside the isolated sandbox rather
    than in the Temporal worker's process. See README.md, section "What is
    missing for production".
    """

    def __init__(self, *, model: str | None = None):
        self._model = model or os.environ.get("DSE_CODER_MODEL", "gateway/coder-default")
        self._llm = None
        self._agent = None
        self._conversation = None
        self.workspace_dir: str | None = None
        self.sandbox_id = ""
        self._turns: list[TurnLog] = []

    def create_session(
        self,
        *,
        work_item_id: str,
        workspace_dir: str,
        gateway_headers: GatewayCallHeaders,
        virtual_key: str,
        gateway_base_url: str,
    ) -> None:
        try:
            import openhands.sdk as sdk
        except ImportError as exc:  # pragma: no cover - only exercised when the package is missing
            raise OpenHandsSubstrateUnavailable(
                "openhands-sdk not installed in this venv. "
                "`pip install openhands-sdk` (verified working in 2026-07 "
                "in this session, v1.21.0). Until then, use FakeSubstrate."
            ) from exc

        self.workspace_dir = workspace_dir
        self.sandbox_id = work_item_id
        self._llm = sdk.LLM(
            model=self._model,
            base_url=gateway_base_url,
            api_key=virtual_key,
            extra_headers=gateway_headers.to_http_headers(),
        )
        self._agent = sdk.Agent(llm=self._llm)
        self._conversation = sdk.Conversation(
            agent=self._agent,
            workspace=sdk.LocalWorkspace(working_dir=workspace_dir),
        )
        self._turns = []

    def run_turn(self, instruction: str) -> TurnLog:
        if self._conversation is None:
            raise RuntimeError("create_session must be called before run_turn")
        self._conversation.send_message(instruction)
        self._conversation.run()
        log = TurnLog(instruction=instruction, done=True)
        self._turns.append(log)
        return log

    def collect_artifacts(self) -> CoderTurnResult:
        stats = None
        if self._conversation is not None:
            stats = getattr(self._conversation, "conversation_stats", None)
        cost_usd = 0.0
        tokens_in = 0
        tokens_out = 0
        if stats is not None:
            cost_usd = float(getattr(stats, "total_cost_usd", 0.0) or 0.0)
            tokens_in = int(getattr(stats, "total_tokens_in", 0) or 0)
            tokens_out = int(getattr(stats, "total_tokens_out", 0) or 0)
        return CoderTurnResult(
            sandbox_id=self.sandbox_id,
            diff_summary=f"{len(self._turns)} turn(s) executed via OpenHandsSubstrate",
            files_changed=[],
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


class ClaudeAgentSubstrate:
    """Second substrate (WSC-E3-T6): a real adapter over the Claude Agent SDK
    (PyPI package `claude-agent-sdk` — `pip install claude-agent-sdk` **worked in
    this session**, v0.2.124; the wheel embeds the CLI, with no dependency on a
    global node). It is the insurance against OpenHands upstream volatility
    (risk 5 of the plan; the MinIO/RabbitMQ 2026 precedents): same
    `AgentSubstrate` interface, same conformance suite
    (`tests/test_substrate_conformance.py`), swapped by DEPLOYMENT CONFIG
    (`DSE_CODER_SUBSTRATE`, see `substrate_from_env`) — zero workflow code
    changes.

    Gateway-only wiring (P1 — never a direct provider SDK/endpoint): the Claude
    Agent SDK talks to the Anthropic API through the bundled CLI, which honors
    the `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`/`ANTHROPIC_CUSTOM_HEADERS` envs.
    `create_session` builds `ClaudeAgentOptions(env=...)` with base_url =
    model-gateway, api_key = the per-task minted virtual key, and the contract's
    mandatory headers (`GatewayCallHeaders`) — exactly the same triangle as
    `OpenHandsSubstrate`. Even if the process tried another endpoint, the
    egress-proxy (E2) would block the route.

    Toolset (P1): `allowed_tools` is restricted to file read/edit
    (`Read`/`Write`/`Edit`/`Glob`/`Grep`) — NO git/PR/bash tool; the commit/push
    remains deterministic in the Activity (`ScopedGitSession`), identical to the
    OpenHands substrate. `setting_sources=["project"]` loads ONLY the workspace's
    `.claude/` (skills ticked in the console + skills committed in the target
    repo, nativamente via setting_sources); the HOST user's settings/skills stay out (the
    session is hermetic with respect to the host).

    NOT exercised through a full turn in this suite (the same limitation declared
    for OpenHands): `run_turn` triggers real inference, which requires the
    model-gateway with a valid provider serving the virtual key. The conformance
    tests cover construction/wiring/selection — see the README."""

    # Claude Code's file-editing tool surface — deliberately without
    # Bash/WebFetch/Task and without any git/PR tool (P1).
    DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]

    def __init__(self, *, model: str | None = None, allowed_tools: list[str] | None = None):
        self._model = model or os.environ.get("DSE_CODER_MODEL", "gateway/coder-default")
        self._allowed_tools = list(allowed_tools or self.DEFAULT_ALLOWED_TOOLS)
        self._options = None
        self.workspace_dir: str | None = None
        self.sandbox_id = ""
        self._turns: list[TurnLog] = []
        self._cost_usd = 0.0
        self._tokens_in = 0
        self._tokens_out = 0

    def create_session(
        self,
        *,
        work_item_id: str,
        workspace_dir: str,
        gateway_headers: GatewayCallHeaders,
        virtual_key: str,
        gateway_base_url: str,
    ) -> None:
        try:
            import claude_agent_sdk as sdk
        except ImportError as exc:  # pragma: no cover - only exercised when the package is missing
            raise ClaudeAgentSubstrateUnavailable(
                "claude-agent-sdk not installed in this venv. "
                "`pip install claude-agent-sdk` (verified working in "
                "2026-07 in this session, v0.2.124). Until then, use "
                "FakeSubstrate or OpenHandsSubstrate."
            ) from exc

        self.workspace_dir = workspace_dir
        self.sandbox_id = work_item_id
        custom_headers = "\n".join(f"{k}: {v}" for k, v in gateway_headers.to_http_headers().items())
        self._options = sdk.ClaudeAgentOptions(
            model=self._model,
            cwd=workspace_dir,
            allowed_tools=list(self._allowed_tools),
            permission_mode="acceptEdits",
            # "project" = ONLY the workspace (cwd): loads the skills ticked in
            # the console and materialized under .claude/skills/ + the ones
            # committed in the target repo. Still hermetic with respect to the
            # HOST — "user" (the machine user's settings/skills) stays off the
            # list.
            setting_sources=["project"],
            env={
                # Gateway-only: the bundled CLI honors these envs — it never
                # points at api.anthropic.com with a provider credential.
                "ANTHROPIC_BASE_URL": gateway_base_url,
                "ANTHROPIC_API_KEY": virtual_key,
                "ANTHROPIC_CUSTOM_HEADERS": custom_headers,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )
        self._turns = []
        self._cost_usd = 0.0
        self._tokens_in = 0
        self._tokens_out = 0

    async def _run_turn_async(self, instruction: str) -> TurnLog:
        import asyncio as _asyncio

        import claude_agent_sdk as sdk

        # HARD per-turn timeout (found in the real 2026-07-22 run: the CLI hung
        # 45+ min with no progress — the Activity's heartbeat keeps beating while
        # the thread is stuck, so heartbeat ≠ progress; the 1h start_to_close
        # would only kill the whole activity). TimeoutError becomes a normal
        # Temporal retry.
        turn_timeout = float(os.environ.get("DSE_CODER_TURN_TIMEOUT_S", "900"))

        log = TurnLog(instruction=instruction, done=True)

        async def _consume() -> None:
            async for message in sdk.query(prompt=instruction, options=self._options):
                if isinstance(message, sdk.AssistantMessage):
                    for block in message.content:
                        if isinstance(block, sdk.TextBlock):
                            log.thoughts.append(block.text)
                        elif isinstance(block, sdk.ToolUseBlock):
                            log.tool_calls.append(block.name)
                elif isinstance(message, sdk.ResultMessage):
                    self._cost_usd += float(message.total_cost_usd or 0.0)
                    usage = message.usage or {}
                    self._tokens_in += int(usage.get("input_tokens", 0) or 0)
                    self._tokens_out += int(usage.get("output_tokens", 0) or 0)

        await _asyncio.wait_for(_consume(), timeout=turn_timeout)
        return log

    def run_turn(self, instruction: str) -> TurnLog:
        if self._options is None:
            raise RuntimeError("create_session must be called before run_turn")
        import asyncio
        import concurrent.futures

        coro = self._run_turn_async(instruction)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            log = asyncio.run(coro)
        else:
            # Inside an event loop (e.g. a Temporal async Activity): run the turn
            # in its own loop on a dedicated thread — the substrate interface is
            # synchronous by contract (parity with OpenHands).
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                log = ex.submit(asyncio.run, coro).result()
        self._turns.append(log)
        return log

    def collect_artifacts(self) -> CoderTurnResult:
        return CoderTurnResult(
            sandbox_id=self.sandbox_id,
            diff_summary=f"{len(self._turns)} turn(s) executed via ClaudeAgentSubstrate",
            files_changed=[],
            cost_usd=round(self._cost_usd, 6),
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )


# ---------------------------------------------------------------------------
# Substrate selection by DEPLOYMENT CONFIG (WSC-E3-T6) — swapping substrates
# never requires a workflow code change: the worker reads `DSE_CODER_SUBSTRATE`
# at deploy time and the Activity builds through this factory.
# ---------------------------------------------------------------------------
SUBSTRATE_ENV_VAR = "DSE_CODER_SUBSTRATE"

_SUBSTRATE_NAMES = ("fake", "openhands", "claude-agent")


def substrate_from_env(
    name: str | None = None, *, script: list[dict[str, Any]] | None = None
) -> AgentSubstrate:
    """Build the configured substrate: `fake` (default — no model), `openhands`
    or `claude-agent`. An unknown name is a CLEAN failure (P6), never a silent
    fallback to another substrate."""
    chosen = (name or os.environ.get(SUBSTRATE_ENV_VAR, "fake")).strip().lower()
    # Constructing the fake directly is still supported in dev/test, but it can
    # never be selected by a production deployment.
    validate_runtime_profile(require_real_substrate=True, substrate_name=chosen)
    if chosen == "fake":
        return FakeSubstrate(script or [])
    if chosen == "openhands":
        return OpenHandsSubstrate()
    if chosen == "claude-agent":
        return ClaudeAgentSubstrate()
    raise ValueError(
        f"{SUBSTRATE_ENV_VAR}={chosen!r} is not a known substrate "
        f"(valid: {', '.join(_SUBSTRATE_NAMES)})"
    )
