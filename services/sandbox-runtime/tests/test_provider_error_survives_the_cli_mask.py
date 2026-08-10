"""O erro do PROVEDOR não pode morrer na máscara do CLI.

Incidente de 2026-08-10, medido: o crédito da Anthropic acabou no meio de uma
rodada. O LiteLLM devolveu, palavra por palavra:

    {"type":"error","error":{"type":"invalid_request_error",
     "message":"Your credit balance is too low to access the Anthropic API..."}}

e o que chegou ao worker foi:

    RemoteTurnError: [substrate_error] Exception: Claude Code returned an error
    result: success

`_raise_if_permanent_provider_error` existe justamente para transformar isso em
falha limpa e NÃO-retryable (`_PERMANENT_PROVIDER_MARKERS` tem "credit balance
is too low" desde o incidente de 2026-07-22). Ele não disparou porque a string
que lhe entregaram não tem nenhum marcador: o `run_coder_turn` do wi_957b9aad
ficou 15 tentativas em retry e o operador levou uma auditoria inteira para
descobrir que o problema era a fatura.

A causa é estreita e verificável: `_run_claude_agent` consome o `ResultMessage`
do SDK lendo APENAS `total_cost_usd` e `usage`. O mesmo objeto carrega
`is_error`, `subtype`, `result`, `errors` e `api_error_status` — conferido no
SDK real da imagem rc.76 — e é lá que o texto do provedor viaja. A informação
estava na mão do runner e era descartada uma linha antes de fazer falta.
"""
from __future__ import annotations

import os
import sys
import types

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.executor import run_agent_turn  # noqa: E402
from dse_contracts import AgentTurnRequest  # noqa: E402

_BILLING = (
    '{"type":"error","error":{"type":"invalid_request_error","message":'
    '"Your credit balance is too low to access the Anthropic API. Please go to '
    'Plans & Billing to upgrade or purchase credits."}}'
)


def _install_fake_sdk(monkeypatch, *, result_message, raises: Exception | None):
    """SDK falso com a forma REAL do incidente: um ResultMessage chega (é dele
    que sai o custo), e logo depois a query estoura com a exceção mascarada."""

    class _TextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class _ToolUseBlock:
        def __init__(self, name: str) -> None:
            self.name = name

    class _AssistantMessage:
        def __init__(self, content: list) -> None:
            self.content = content

    class _ResultMessage:
        def __init__(self, **kw) -> None:
            for k, v in kw.items():
                setattr(self, k, v)

    async def _query(prompt, options):  # noqa: ARG001
        yield _ResultMessage(**result_message)
        if raises is not None:
            raise raises

    class _Options:
        def __init__(self, **kw) -> None:
            for k, v in kw.items():
                setattr(self, k, v)

    sdk = types.ModuleType("claude_agent_sdk")
    sdk.query = _query
    sdk.ClaudeAgentOptions = _Options
    sdk.AssistantMessage = _AssistantMessage
    sdk.TextBlock = _TextBlock
    sdk.ToolUseBlock = _ToolUseBlock
    sdk.ResultMessage = _ResultMessage
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)


def _req(tmp_path, **overrides):
    base = {
        "work_item_id": "wi_mask",
        "tenant_id": "tenant-a",
        "stage": "coder",
        "substrate": "claude-agent",
        "instruction": "implement it",
        "workspace_dir": str(tmp_path),
        "gateway": {"base_url": "http://model-gateway:4000", "virtual_key": "vk-eph"},
    }
    base.update(overrides)
    return AgentTurnRequest.model_validate(base)


def test_the_provider_message_reaches_the_error_instead_of_the_cli_mask(tmp_path, monkeypatch):
    """O caso literal do wi_957b9aad."""
    _install_fake_sdk(
        monkeypatch,
        result_message={
            "total_cost_usd": 1.37,
            "usage": {"input_tokens": 205, "output_tokens": 9206},
            "is_error": True,
            "subtype": "success",
            "result": _BILLING,
        },
        raises=Exception("Claude Code returned an error result: success"),
    )

    out = run_agent_turn(_req(tmp_path))

    assert out.failed, "a rodada falhou; o resultado tem que dizer isso"
    assert "credit balance is too low" in (out.error or "").lower(), (
        "sem o texto do provedor, `_raise_if_permanent_provider_error` não casa "
        "marcador nenhum e a atividade reentrega para sempre — foi o que "
        "aconteceu por 15 tentativas no wi_957b9aad. O erro tem que CARREGAR o "
        f"motivo. Veio: {out.error!r}"
    )
    assert out.cost_usd == 1.37, "o que já foi pago continua contabilizado"


def test_an_error_result_without_provider_text_still_says_it_was_masked(tmp_path, monkeypatch):
    """Nem todo `error result` é fatura. Quando não há texto de provedor, não
    inventamos um: mas o operador precisa ver que o CLI mascarou algo, senão a
    mensagem sozinha ("error result: success") mente dizendo `success`."""
    _install_fake_sdk(
        monkeypatch,
        result_message={
            "total_cost_usd": 0.0,
            "usage": {},
            "is_error": True,
            "subtype": "error_during_execution",
            "result": "",
            "terminal_reason": "unknown",
        },
        raises=Exception("Claude Code returned an error result: error_during_execution"),
    )

    out = run_agent_turn(_req(tmp_path))

    assert out.failed
    low = (out.error or "").lower()
    assert "credit" not in low, "não classificar como fatura o que não é"
    assert "error_during_execution" in low, (
        "o subtype é a única pista que sobra quando o CLI mascara; ele tem que "
        f"aparecer. Veio: {out.error!r}"
    )


def test_a_healthy_turn_carries_no_error_noise(tmp_path, monkeypatch):
    """PIN de polaridade: rodada boa não ganha ruído de erro nenhum."""
    _install_fake_sdk(
        monkeypatch,
        result_message={
            "total_cost_usd": 0.5,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "is_error": False,
            "subtype": "success",
            "result": "done",
        },
        raises=None,
    )

    out = run_agent_turn(_req(tmp_path))

    assert not out.failed
    assert not out.error
    assert out.cost_usd == 0.5
