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


def event_kind(activity: dict[str, Any]) -> EventKind:
    """Mention of the bot -> task_request; a plain message in an existing
    conversation -> clarification_answer (same convention as Slack for messages in
    a thread; the steering gate in `correlate` handles review/steering)."""
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
