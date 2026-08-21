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


def _acao(action_id: str, titulo: str, valor: str, *, estilo: str | None = None,
          dialogo: bool = False, work_item_id: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {MARKER: True, "action_id": action_id, "value": valor}
    if work_item_id:
        # O item viaja NO card. Sem isto o bot teria de descobrir de qual item é
        # o clique a partir da activity — e conversa do Teams sem thread não dá
        # esse endereço de volta.
        data["work_item_id"] = work_item_id
    if dialogo:
        # Com este marcador o Teams manda um `invoke` (task/fetch) em vez de uma
        # mensagem, e o bot responde o diálogo no corpo da própria resposta.
        data["msteams"] = {"type": "task/fetch"}
    acao: dict[str, Any] = {
        "type": "Action.Submit",
        "title": titulo,
        # `data` volta INTEIRO na activity — é o canal do veredito.
        "data": data,
    }
    if estilo:
        acao["style"] = estilo
    return acao


def status_card(body: str, *, status: str = "", repo: str | None = None,
                work_item_id: str | None = None) -> dict[str, Any]:
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
    acoes.append(_acao(ACTION_DETAILS, "Details", "details",
                       dialogo=True, work_item_id=work_item_id))

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


# ---------------------------------------------------------------------------
# O diálogo (task module) — o modal do Slack, na gramática do Teams
# ---------------------------------------------------------------------------

#: Cortes. Diálogo que rola sem fim não é mais legível que a mensagem que ele
#: veio substituir, e corte silencioso é a versão discreta do mesmo problema —
#: por isso todo corte DIZ que cortou.
_MAX_ITENS = 25
_MAX_CHARS = 1200


def _lista(items: Any) -> str:
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return ""
    mostrados = [str(i) for i in items[:_MAX_ITENS]]
    omitidos = max(0, len(items) - len(mostrados))
    corpo = "\n".join(f"- {i}" for i in mostrados)
    if len(corpo) > _MAX_CHARS:
        corpo = corpo[:_MAX_CHARS] + "\n- …truncated"
    if omitidos:
        corpo += f"\n- …and {omitidos} more"
    return corpo


def _envelope(titulo: str, corpo: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "task": {
            "type": "continue",
            "value": {
                "title": titulo,
                "width": "medium",
                "height": "medium",
                "card": {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "type": "AdaptiveCard",
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "version": _SCHEMA_VERSION,
                        "body": corpo,
                    },
                },
            },
        }
    }


def refusal_dialog(mensagem: str) -> dict[str, Any]:
    """Recusa VISÍVEL. Um 200 vazio fecha o diálogo em branco, que é
    indistinguível de erro — e foi o que o operador viu na rc.111."""
    return _envelope("DSE", [_texto(mensagem)])


def plan_details_dialog(work_item_id: str, plan: dict | None, *, risk: str | None,
                        repo: str | None) -> dict[str, Any]:
    """O plano deste item, para leitura.

    `risk` é o risco EFETIVO (coluna `work_items.risk_class`), nunca o
    declarado dentro do plano: a política só escala PARA CIMA, e mostrar o
    declarado seria a tela que o humano abre para decidir argumentando na
    direção de aprovar."""
    corpo: list[dict[str, Any]] = []
    if repo:
        corpo.append(_texto(repo, pequeno=True, wrap=False))
    if not plan:
        corpo.append(_texto("_The plan is not available for this item._"))
        return _envelope("Plan", corpo)

    cabecalho = f"**Risk:** {str(risk or plan.get('risk_class') or 'unknown')[:80]}"
    est = plan.get("estimated_lines")
    if isinstance(est, int) and est > 0:
        cabecalho += f"    **Estimated diff:** ~{est} lines (planner's estimate)"
    corpo.append(_texto(cabecalho))
    if plan.get("no_code_change"):
        corpo.append(_texto("_This plan changes no code._"))

    for titulo, chave in (("Steps", "steps"), ("Expected files", "expected_files"),
                          ("Constraints", "constraints")):
        itens = plan.get(chave)
        if itens:
            corpo.append(_texto(f"**{titulo}**\n{_lista(itens)}"))
    if plan.get("test_plan"):
        corpo.append(_texto(f"**Test plan**\n{str(plan['test_plan'])[:_MAX_CHARS]}"))
    return _envelope("Plan", corpo)
