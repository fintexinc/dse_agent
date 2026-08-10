"""Real agent-turn executor INSIDE the sandbox (plan 09, Phase 1).

This module runs in the hardened container/pod — never in the worker. It takes
an `AgentTurnRequest` (typed dse_contracts contract), runs the requested
substrate against the local `/workspace` and returns an `AgentTurnResult`.
Rules:

  - P1: the substrate ONLY edits files. No git/PR/bash tool goes into the
    toolset; commit/push stay deterministic in the worker (ScopedGitSession
    over the workspace bind/emptyDir).
  - Gateway-only: the CLI/SDK talks exclusively to the model-gateway through
    the request's ephemeral virtual key. No long-lived credential exists in
    this process; and even if a route existed, the egress-proxy/NetworkPolicy
    blocks reaching a provider directly.
  - P6: every failure becomes a structured `AgentTurnResult` (error_kind from a
    closed vocabulary), never truncated stdout nor a raw exception in the exec.

v1 substrates: "fake" (conformance/tests) and "claude-agent" (the substrate for
real runs). "openhands" returns unsupported_substrate until the
RemoteWorkspace/agent-server is packaged into this image — a clean error, never
a silent fallback.
"""
from __future__ import annotations

import asyncio
import os

from dse_contracts import AgentTurnRequest, AgentTurnResult

# Diagnostic caps: the result crosses exec/JSON — never let a chatty turn blow
# up the channel (the audit stores a sample, not a transcript).
_MAX_LOG_ITEMS = 50
_MAX_ITEM_CHARS = 2000


def _capped(items: list[str]) -> list[str]:
    return [i[:_MAX_ITEM_CHARS] for i in items[:_MAX_LOG_ITEMS]]


def _run_fake(req: AgentTurnRequest) -> AgentTurnResult:
    """Replays the script (same semantics as FakeSubstrate, collapsed into a
    single exec: the runner is stateless between execs, so it consumes the whole
    script and reports the `done` of the last step)."""
    thoughts: list[str] = []
    done = True
    for step in req.fake_script or []:
        for rel_path, content in (step.get("write_files") or {}).items():
            target = os.path.join(req.workspace_dir, rel_path)
            os.makedirs(os.path.dirname(target) or req.workspace_dir, exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(content)
        if step.get("thought"):
            thoughts.append(str(step["thought"]))
        done = bool(step.get("done", True))
    return AgentTurnResult(done=done, thoughts=_capped(thoughts))


def build_claude_gateway_env(req: AgentTurnRequest) -> dict[str, str]:
    """Pure, testable core of the gateway-only wiring (mirrors the worker's
    ClaudeAgentSubstrate — if one changes, change both)."""
    custom_headers = "\n".join(f"{k}: {v}" for k, v in req.gateway.headers.items())
    return {
        "ANTHROPIC_BASE_URL": req.gateway.base_url,
        "ANTHROPIC_API_KEY": req.gateway.virtual_key,
        "ANTHROPIC_CUSTOM_HEADERS": custom_headers,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def _provider_error_text(message: object) -> str:
    """O que um ResultMessage com `is_error` sabe sobre a falha, em uma linha.

    Tudo por `getattr`: o SDK evolui e um campo que sumiu não pode derrubar o
    turno — perder o motivo já é ruim, trocar o motivo por um AttributeError
    seria pior."""
    parts = [
        str(getattr(message, "subtype", "") or ""),
        str(getattr(message, "api_error_status", "") or ""),
        str(getattr(message, "terminal_reason", "") or ""),
        str(getattr(message, "result", "") or ""),
        str(getattr(message, "errors", "") or ""),
    ]
    return " | ".join(p for p in parts if p and p != "None")[:600]


def _with_provider_context(error: str, provider_error: str) -> str:
    """Cola o motivo do provedor na mensagem que o CLI mascarou.

    A máscara é literal: em 2026-08-10 o crédito da Anthropic acabou, o
    LiteLLM devolveu `"Your credit balance is too low…"` e o que chegou ao
    worker foi `Exception: Claude Code returned an error result: success` —
    uma frase que termina na palavra `success`. Sem marcador,
    `_raise_if_permanent_provider_error` (sandbox_runtime/activities.py) não
    casa nada, a falha é tratada como transitória e a atividade reentrega:
    15 tentativas no wi_957b9aad, ~1h47 de silêncio até o prazo da atividade.

    Aqui NÃO se classifica nada — só se para de jogar fora a evidência. Quem
    decide "isto é fatura, não tente de novo" continua sendo o classificador
    de sempre, que agora recebe o texto que precisa."""
    if not provider_error or provider_error in error:
        return error
    return f"{error} | provider: {provider_error}"


def _run_claude_agent(req: AgentTurnRequest) -> AgentTurnResult:
    try:
        import claude_agent_sdk as sdk
    except ImportError:
        return AgentTurnResult(
            done=False,
            error="claude-agent-sdk is not installed in the agent-runner image",
            error_kind="substrate_error",
        )

    options = sdk.ClaudeAgentOptions(
        model=req.model,
        cwd=req.workspace_dir,
        allowed_tools=list(req.allowed_tools),
        permission_mode="acceptEdits",
        # "project" = ONLY the workspace: skills materialized/committed in the
        # target repo. Inside the sandbox there is no host "user" scope to leak
        # from, but the explicit list keeps parity with the worker substrate.
        setting_sources=["project"],
        env=build_claude_gateway_env(req),
    )

    thoughts: list[str] = []
    tool_calls: list[str] = []
    totals = {"cost": 0.0, "tin": 0, "tout": 0}
    provider_error = [""]  # lista: o closure de _consume escreve aqui

    async def _consume() -> None:
        async for message in sdk.query(prompt=req.instruction, options=options):
            if isinstance(message, sdk.AssistantMessage):
                for block in message.content:
                    if isinstance(block, sdk.TextBlock):
                        thoughts.append(block.text)
                    elif isinstance(block, sdk.ToolUseBlock):
                        tool_calls.append(block.name)
            elif isinstance(message, sdk.ResultMessage):
                totals["cost"] += float(message.total_cost_usd or 0.0)
                usage = message.usage or {}
                totals["tin"] += int(usage.get("input_tokens", 0) or 0)
                totals["tout"] += int(usage.get("output_tokens", 0) or 0)
                # O MESMO objeto carrega o motivo, e ele era descartado aqui.
                # Em 2026-08-10 o crédito da Anthropic acabou: o LiteLLM
                # devolveu "Your credit balance is too low…", o CLI mascarou
                # como `Exception: Claude Code returned an error result:
                # success` e o que chegou ao worker não tinha marcador algum —
                # `_raise_if_permanent_provider_error` não casou, e a atividade
                # reentregou 15 vezes enquanto o operador auditava o produto.
                # `result`/`errors`/`api_error_status` são onde o texto do
                # provedor viaja; `subtype` é a única pista quando não há texto.
                if getattr(message, "is_error", False):
                    provider_error[0] = _provider_error_text(message)

    try:
        asyncio.run(asyncio.wait_for(_consume(), timeout=req.timeout_seconds))
    except (TimeoutError, asyncio.TimeoutError):
        return AgentTurnResult(
            done=False,
            thoughts=_capped(thoughts),
            tool_calls=_capped(tool_calls),
            cost_usd=totals["cost"],
            tokens_in=totals["tin"],
            tokens_out=totals["tout"],
            error=f"turn exceeded {req.timeout_seconds:.0f}s without finishing",
            error_kind="timeout",
        )
    except Exception as exc:  # noqa: BLE001 — P6: structured failure, never raw
        return AgentTurnResult(
            done=False,
            thoughts=_capped(thoughts),
            tool_calls=_capped(tool_calls),
            cost_usd=totals["cost"],
            tokens_in=totals["tin"],
            tokens_out=totals["tout"],
            error=_with_provider_context(
                f"{type(exc).__name__}: {str(exc)[:500]}", provider_error[0]
            ),
            error_kind="substrate_error",
        )

    return AgentTurnResult(
        done=True,
        thoughts=_capped(thoughts),
        tool_calls=_capped(tool_calls),
        cost_usd=totals["cost"],
        tokens_in=totals["tin"],
        tokens_out=totals["tout"],
    )


def _run_openhands(req: AgentTurnRequest) -> AgentTurnResult:
    """OpenHands INSIDE the sandbox: here `LocalWorkspace` is the RIGHT design —
    the whole SDK (and its tool execution) is already confined to the
    container/pod; it was running it in the worker that broke the threat model.
    Gateway-only wiring identical to the worker's (base_url + virtual key +
    contract headers). Installation is opt-in per image
    (requirements-openhands.txt / build-arg INSTALL_OPENHANDS=1)."""
    try:
        import openhands.sdk as sdk
    except ImportError:
        return AgentTurnResult(
            done=False,
            error=(
                "openhands-sdk is not installed in this agent-runner image "
                "(build with INSTALL_OPENHANDS=1)"
            ),
            error_kind="substrate_error",
        )

    def _consume() -> AgentTurnResult:
        llm = sdk.LLM(
            model=req.model,
            base_url=req.gateway.base_url,
            api_key=req.gateway.virtual_key,
            extra_headers=dict(req.gateway.headers),
        )
        agent = sdk.Agent(llm=llm)
        conversation = sdk.Conversation(
            agent=agent,
            workspace=sdk.LocalWorkspace(working_dir=req.workspace_dir),
        )
        conversation.send_message(req.instruction)
        conversation.run()
        stats = getattr(conversation, "conversation_stats", None)
        return AgentTurnResult(
            done=True,
            cost_usd=float(getattr(stats, "total_cost_usd", 0.0) or 0.0),
            tokens_in=int(getattr(stats, "total_tokens_in", 0) or 0),
            tokens_out=int(getattr(stats, "total_tokens_out", 0) or 0),
        )

    # Hard per-turn timeout (same discipline as claude-agent): the run is
    # synchronous, so it goes on a watched thread — an overrun becomes a
    # structured result and the ephemeral exec process dies right after.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_consume)
        try:
            return future.result(timeout=req.timeout_seconds)
        except concurrent.futures.TimeoutError:
            return AgentTurnResult(
                done=False,
                error=f"openhands turn exceeded {req.timeout_seconds:.0f}s",
                error_kind="timeout",
            )
        except Exception as exc:  # noqa: BLE001 — P6
            return AgentTurnResult(
                done=False,
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
                error_kind="substrate_error",
            )


def run_agent_turn(req: AgentTurnRequest) -> AgentTurnResult:
    if req.substrate == "fake":
        try:
            return _run_fake(req)
        except OSError as exc:
            # e.g. an attempt to write outside /workspace on a read-only
            # rootfs — the OS denies it and the denial becomes a structured
            # result.
            return AgentTurnResult(
                done=False,
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                error_kind="substrate_error",
            )
    if req.substrate == "claude-agent":
        return _run_claude_agent(req)
    if req.substrate == "openhands":
        return _run_openhands(req)
    return AgentTurnResult(
        done=False,
        error=f"unknown substrate: {req.substrate!r}",
        error_kind="unsupported_substrate",
    )
