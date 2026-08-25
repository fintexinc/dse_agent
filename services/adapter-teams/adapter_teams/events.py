"""Microsoft Teams normalization (Bot Framework Activity) -> `ConversationEvent`.

Mirrors `adapter_slack.events` / `adapter_jira.events`. A Teams Activity
(received via outgoing webhook or via the Bot Framework channel) has the shape:

    {
      "type": "message",
      "id": "<activity id>",
      "from": {"id": "29:<aad>", "name": "Jane Doe", "aadObjectId": "..."},
      "conversation": {"id": "19:<channel>@thread.tacv2", "conversationType": "channel"},
      "recipient": {"id": "28:<bot>", "name": "DSE Bot"},
      "text": "<at>DSE Bot</at> please fix the login bug",
      "replyToId": "<parent activity id>",           # present on thread replies
      "channelData": {"tenant": {"id": "<aad-tenant-guid>"}},
      "serviceUrl": "https://smba.trafficmanager.net/emea/"
    }

TOCTOU defense (WSA-E2-T2): `content_snapshot` comes from the `text` of the
received payload itself — we never re-fetch the message via Graph/connector
afterwards.

Correlation: `source_ref = {"conversation_id": <conversation.id>}` (analogous to
Slack's `{channel, thread_ts}`). `message_id` is the Activity id (state), so
redeliveries of the same webhook converge on the same deterministic `event_id`
(idempotency defense #4).

Building the typed `ConversationEvent` goes through `platform_compat.teams_platform()`,
which raises `TeamsNotActivated` while the foundation does not expose
`Platform.teams` (see platform_compat.py / README) — the pure extractors below do
NOT depend on that and are already fully testable.
"""
from __future__ import annotations

import re
from typing import Any

from dse_contracts import Actor, ConversationEvent, EventKind

from .platform_compat import teams_platform
from dse_contracts.surface import ACTION_DETAILS, parse_approval_click

#: O marcador que este bot põe em todo `Action.Submit` que desenha — é o
#: que separa clique de conversa e barra `value` de outra extensão.
CARD_MARKER = "dse"


_AT_TAG = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)

# Platform string for the deterministic event_id. Matches the VALUE that
# `Platform.teams` will have once the foundation is extended (the enum is a
# StrEnum: Platform.teams.value == "teams") — so the event_id computed now is the
# same one the typed pipeline will produce post-activation.
PLATFORM_STR = "teams"


def conversation_id(activity: dict[str, Any]) -> str:
    return (activity.get("conversation") or {}).get("id", "")


def activity_id(activity: dict[str, Any]) -> str:
    return activity.get("id", "")


def service_url(activity: dict[str, Any]) -> str:
    return activity.get("serviceUrl", "")


def aad_tenant_id(activity: dict[str, Any]) -> str | None:
    """AAD tenant guid — the `binding_key` for resolving Teams tenant -> DSE
    tenant (WSA-E1-T5) once activated."""
    tenant = ((activity.get("channelData") or {}).get("tenant") or {})
    return tenant.get("id")


def actor_of(activity: dict[str, Any]) -> tuple[str, str | None]:
    """(platform_user_id, display_name). The Teams user id is `from.id`
    (`29:<aad>`); we prefer the stable `aadObjectId` when present."""
    frm = activity.get("from") or {}
    user_id = frm.get("aadObjectId") or frm.get("id", "")
    return user_id, frm.get("name")


def clean_text(activity: dict[str, Any]) -> str:
    """Strips the `<at>...</at>` mention tags, leaving the user's text — the same
    thing Slack does when removing the leading `<@U…>` from an app_mention."""
    text = activity.get("text", "") or ""
    return _AT_TAG.sub("", text).strip()


def is_mention(activity: dict[str, Any]) -> bool:
    """True if the Activity mentions the bot (an `<at>` tag in the text OR a
    mention entity whose mentioned.id matches the recipient/bot). Outgoing
    webhooks always @mention the bot; the Bot Framework channel uses entities."""
    if _AT_TAG.search(activity.get("text", "") or ""):
        return True
    recipient_id = (activity.get("recipient") or {}).get("id")
    for ent in activity.get("entities", []) or []:
        if ent.get("type") == "mention":
            mentioned = (ent.get("mentioned") or {}).get("id")
            if recipient_id is None or mentioned == recipient_id:
                return True
    return False


def thread_key(activity: dict[str, Any]) -> str:
    return conversation_id(activity)


def card_data(activity: dict[str, Any]) -> dict[str, Any] | None:
    """O `data` do `Action.Submit` que ESTE bot desenhou, ou None.

    O Teams entrega o clique como activity `message` com `value` preenchido e
    sem texto — mesma porta assinada das mensagens. O marcador é o que separa
    clique de conversa: sem ele, o `value` de qualquer outra extensão instalada
    no tenant entraria aqui como veredito."""
    value = activity.get("value")
    if isinstance(value, dict) and value.get(CARD_MARKER) is True:
        return value
    return None


def card_verdict(activity: dict[str, Any]) -> tuple[str, str | None] | None:
    """(veredito, rota) de um clique de card, ou None se não for clique.

    Deriva pela MESMA função que o Slack usa (`dse_contracts.surface`): recusa
    jamais lida como aprovação."""
    data = card_data(activity)
    if data is None:
        return None
    return parse_approval_click(str(data.get("action_id") or ""), str(data.get("value") or ""))


def is_details_click(activity: dict[str, Any]) -> bool:
    """Details vive em TODA mensagem, inclusive fora do gate. Se ele caísse no
    fallthrough de veredito, um clique curioso aprovaria o plano — o Slack o
    desvia antes, e aqui é igual."""
    data = card_data(activity)
    return bool(data) and data.get("action_id") == ACTION_DETAILS


def event_kind(activity: dict[str, Any]) -> EventKind:
    """Clique num card -> approval; menção ao bot -> task_request; mensagem
    simples numa conversa existente -> clarification_answer (mesma convenção do
    Slack para mensagens numa thread; o gate de direção em `correlate` cuida de
    review/steering)."""
    if card_data(activity) is not None:
        return EventKind.approval
    return EventKind.task_request if is_mention(activity) else EventKind.clarification_answer


def compute_event_id(activity: dict[str, Any]) -> str:
    """Deterministic event_id (defense #4). Pure — does not depend on activation."""
    return ConversationEvent.compute_event_id(
        PLATFORM_STR, thread_key(activity), activity_id(activity)
    )


def source_ref(activity: dict[str, Any]) -> dict[str, str]:
    """O endereço COMPLETO da resposta. O `service_url` entra junto porque
    endereçar no connector exige os dois: ele é regional (varia por tenant) e
    só chega aqui, na Activity recebida — o orchestrator responde horas depois,
    lendo esta linha. Sem ele, `_resolve_comment_target` devolve None e o
    status some em silêncio."""
    ref = {"conversation_id": conversation_id(activity)}
    url = str(activity.get("serviceUrl") or "").strip()
    if url:
        ref["service_url"] = url
    return ref


def build_conversation_event(
    activity: dict[str, Any], *, resolved_principal: str, sanitized_text: str | None = None
) -> ConversationEvent:
    """Builds the typed `ConversationEvent`. Raises `TeamsNotActivated` if the
    foundation does not expose `Platform.teams` yet (activation blocker). The
    `content_snapshot` is the ORIGINAL frozen text (TOCTOU); `sanitized_text`,
    when provided, is applied by the gateway in `admit`/`record_signal`, not here.
    """
    user_id, display = actor_of(activity)
    return ConversationEvent.build(
        platform=teams_platform(),
        thread_key=thread_key(activity),
        message_id=activity_id(activity),
        kind=event_kind(activity),
        source_ref=source_ref(activity),
        actor=Actor(platform_user_id=user_id, resolved_principal=resolved_principal, display_name=display),
        content_snapshot=clean_text(activity),
        signature_verified=True,
    )


def is_task_fetch(activity: dict[str, Any]) -> bool:
    """A activity é o pedido de diálogo do Teams?

    `task/fetch` chega como `invoke`, não como `message` — por isso a checagem
    existe: o endpoint recusava tudo que não fosse mensagem, e o clique de
    Details morria ali."""
    return activity.get("type") == "invoke" and activity.get("name") == "task/fetch"


def task_fetch_work_item(activity: dict[str, Any]) -> str | None:
    """O work item que o CARD carregava. O Teams aninha o `data` da ação em
    `value.data`; um invoke sem ele não tem item a mostrar."""
    value = activity.get("value")
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if not isinstance(data, dict):
        return None
    item = data.get("work_item_id")
    return str(item) if item else None


def task_fetch_action_id(activity: dict[str, Any]) -> str:
    """Qual diálogo o card pediu — o `action_id` viaja no MESMO `data` que o
    work_item_id. Ausente = o Details de sempre (cards antigos ainda vivos
    nas conversas não carregam o campo... carregam, mas a rota não pode
    depender disso para o comportamento legado)."""
    value = activity.get("value")
    data = value.get("data") if isinstance(value, dict) else None
    if not isinstance(data, dict):
        return ""
    return str(data.get("action_id") or "")


def correlation_ref(activity: dict[str, Any]) -> dict[str, str]:
    """O ref que a correlação usa para achar (ou não) uma tarefa existente.

    NÃO é o `source_ref`: aquele é o endereço de RESPOSTA, gravado no banco e
    igual para toda a conversa. Este decide *com o que a mensagem casa*.

      menção        -> conversa + id DESTA activity. Nenhum item existente tem
                       essa chave no `source_ref`, então nada casa e o pedido
                       vira tarefa nova. É o análogo do `thread_ts` próprio que
                       uma mensagem-raiz tem no Slack.
      sem menção    -> só a conversa, casando com a tarefa mais recente dela:
                       falar sem chamar o bot é dirigir o que ele já faz.

    `service_url` fica de fora dos dois: ele varia por região e por tenant, e
    entrar aqui transformaria uma troca de região em "nenhuma tarefa casou"."""
    ref = {"conversation_id": conversation_id(activity)}
    if is_mention(activity):
        ref["root_activity_id"] = activity_id(activity)
    return ref
