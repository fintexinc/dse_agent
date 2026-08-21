"""O Adaptive Card de status — o Block Kit do Teams.

Mesmo layout do Slack (`adapter_slack.backend.status_blocks`), porque a etapa e
os gestos vêm da MESMA fonte (`dse_contracts.surface`); o que muda aqui é só a
gramática do desenho.

    [pequeno]  repo                    (só quando conhecido)
    [texto]    o corpo humanizado
    [pequeno]  a barra de etapas       (só em status em voo)
    [ações]    Details sempre; Approve/Reject SÓ no gate

`Action.Submit` e não `Action.Execute`: o Submit chega ao bot como uma activity
`message` COM `value` e SEM texto, ou seja, pela mesma porta assinada que já
existe — sem rota nova no endpoint e sem responder invoke dentro do timeout do
connector. O card é reescrito in-place pelo writer de sempre, então o refresh
que o Execute daria não é necessário.
"""
from __future__ import annotations

from typing import Any

from dse_contracts.surface import (
    ACTION_APPROVE,
    ACTION_DETAILS,
    ACTION_REJECT,
    DEFAULT_REJECT_ROUTE,
    progress_line,
)

#: Versão do schema. 1.4 é o piso que o Teams renderiza em desktop, web e
#: mobile; sem `version` o Teams recusa o card INTEIRO e o humano vê um espaço
#: em branco no lugar do status.
_SCHEMA_VERSION = "1.4"

#: O marcador que separa clique de conversa. Sem ele, `value` de qualquer
#: outra extensão instalada no tenant entraria como veredito.
MARKER = "dse"

STATUS_GATE = "awaiting_plan_approval"


def _texto(text: str, *, pequeno: bool = False, wrap: bool = True) -> dict[str, Any]:
    bloco: dict[str, Any] = {"type": "TextBlock", "text": text, "wrap": wrap}
    if pequeno:
        bloco["size"] = "Small"
        bloco["isSubtle"] = True
    return bloco


def _acao(action_id: str, titulo: str, valor: str, *, estilo: str | None = None) -> dict[str, Any]:
    acao: dict[str, Any] = {
        "type": "Action.Submit",
        "title": titulo,
        # `data` volta INTEIRO na activity — é o canal do veredito.
        "data": {MARKER: True, "action_id": action_id, "value": valor},
    }
    if estilo:
        acao["style"] = estilo
    return acao


def status_card(body: str, *, status: str = "", repo: str | None = None) -> dict[str, Any]:
    """O attachment pronto para `send_activity`/`update_activity`."""
    corpo: list[dict[str, Any]] = []
    if repo:
        corpo.append(_texto(repo, pequeno=True, wrap=False))
    corpo.append(_texto(body))
    barra = progress_line(status)
    if barra:
        corpo.append(_texto(barra, pequeno=True, wrap=False))

    acoes: list[dict[str, Any]] = []
    if status == STATUS_GATE:
        acoes.append(_acao(ACTION_APPROVE, "Approve", "approve", estilo="positive"))
        acoes.append(_acao(ACTION_REJECT, "Reject", f"reject:{DEFAULT_REJECT_ROUTE}",
                           estilo="destructive"))
    # Sem estilo, de propósito: quem decide tem cor, quem só mostra não — cor
    # igual convidaria o clique errado.
    acoes.append(_acao(ACTION_DETAILS, "Details", "details"))

    return {
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": _SCHEMA_VERSION,
            "body": corpo,
            "actions": acoes,
        },
    }
