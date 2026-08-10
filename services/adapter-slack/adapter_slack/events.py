"""Slack Events API / Interactivity normalization -> `ConversationEvent`
(WSA-E3-T1). The 4 intake defenses run BEFORE anything reaches here
(signature verification happens in `app.py`, which only calls these
functions once `signature_verified=True`).

TOCTOU defense (WSA-E2-T2): `content_snapshot` is the text exactly as it
arrived in the webhook body — we never re-fetch the message via
`conversations.history`/`conversations.replies` afterwards. That is
automatic here by construction: we only read `event["text"]` from the
received payload.
"""
from __future__ import annotations

from typing import Any

from dse_contracts import Actor, ConversationEvent, EventKind, Platform


def _actor_from_user_id(user_id: str, resolved_principal: str, display_name: str | None = None) -> Actor:
    return Actor(platform_user_id=user_id, resolved_principal=resolved_principal, display_name=display_name)


def build_event_from_app_mention(event: dict[str, Any], *, resolved_principal: str) -> ConversationEvent:
    channel = event["channel"]
    ts = event["ts"]
    thread_ts = event.get("thread_ts", ts)  # no thread_ts -> this msg is the root of a new thread
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=f"{channel}:{thread_ts}",
        message_id=ts,
        kind=EventKind.task_request,
        source_ref={"channel": channel, "thread_ts": thread_ts},
        actor=_actor_from_user_id(event["user"], resolved_principal),
        content_snapshot=event.get("text", ""),
        signature_verified=True,
    )


def build_event_from_thread_message(event: dict[str, Any], *, resolved_principal: str) -> ConversationEvent:
    """Ordinary message (no mention) inside an existing thread. Phase 1:
    treated as `clarification_answer` by default — telling clarification
    apart from steering would require knowing whether the bot is waiting for
    an answer, and that state lives in the WS-B workflow, not in the adapter
    (documented as a known limitation in the README)."""
    channel = event["channel"]
    ts = event["ts"]
    thread_ts = event["thread_ts"]  # only called when thread_ts is present (see app.py)
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=f"{channel}:{thread_ts}",
        message_id=ts,
        kind=EventKind.clarification_answer,
        source_ref={"channel": channel, "thread_ts": thread_ts},
        actor=_actor_from_user_id(event["user"], resolved_principal),
        content_snapshot=event.get("text", ""),
        signature_verified=True,
    )


_REJECT_TOKENS = ("reject", "rejected", "deny", "denied", "changes", "re_plan", "replan")


def parse_slack_approval(action_id: str, value: str) -> tuple[str, str | None]:
    """C1 (report 07): derives a DETERMINISTIC verdict/route from the button
    click. The verdict must NOT live in the text alone — it has to become a
    marker (`approval_verdict`/`approval_route`) that the dispatcher reads,
    otherwise the default is `approved` and a "reject" would silently approve
    (gate security bug). Mirrors the Jira path (`ingest_status_approval`).

    Button convention (as posted in the approval Block Kit): `action_id` or
    `value` containing 'reject'/'deny'/'re_plan' => rejected; the `value` may
    carry the route after ':' (e.g. 'reject:re_plan'). Anything else =>
    approved (fail-safe: a rejection is never read as an approval, and an
    ambiguous click does NOT auto-approve something destructive — it just
    follows the normal approval flow, which still requires the gate)."""
    haystack = f"{action_id}|{value}".lower()
    is_reject = any(tok in haystack for tok in _REJECT_TOKENS)
    if not is_reject:
        return "approved", None
    # rejection route: the part after ':' in the value, else default re_plan.
    route = "re_plan"
    if ":" in value:
        candidate = value.split(":", 1)[1].strip()
        if candidate:
            route = candidate
    return "rejected", route


def build_event_from_block_action(payload: dict[str, Any], *, resolved_principal: str) -> ConversationEvent:
    """Button interaction (`block_actions`) -> kind=approval. `source_ref`
    uses the channel+thread_ts of the original status message (where the
    button showed up) to correlate back to the right WorkItem."""
    channel = payload["channel"]["id"]
    message = payload.get("message", {})
    thread_ts = message.get("thread_ts", message.get("ts"))
    action = payload["actions"][0]
    action_ts = payload.get("action_ts", action.get("action_ts", "0"))
    user_id = payload["user"]["id"]

    action_id = action.get("action_id", "unknown_action")
    value = action.get("value", "")

    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=f"{channel}:{thread_ts}",
        message_id=action_ts,
        kind=EventKind.approval,
        source_ref={"channel": channel, "thread_ts": thread_ts},
        actor=_actor_from_user_id(user_id, resolved_principal),
        content_snapshot=f"button:{action_id}={value}",
        signature_verified=True,
    )


#: A6 — botões de parque. O veredito é o VALUE do botão, validado contra o
#: conjunto fechado (marker determinístico, nunca texto livre): `retry` e
#: `escalate` existem em todo parque; `reauthor` só no de spec própria do
#: Tester (o render já não o oferece fora dele, e o dispatcher revalida).
PARK_VERDICTS = ("retry", "escalate", "reauthor")


def parse_slack_park(action_id: str, value: str) -> str | None:
    """Veredito de parque do clique — `dse_park_<verdict>`. Devolve None para
    qualquer coisa fora do conjunto fechado (o chamador ignora com audit em
    vez de adivinhar — P6)."""
    candidate = (value or "").strip().lower()
    if candidate in PARK_VERDICTS and action_id == f"dse_park_{candidate}":
        return candidate
    return None


def build_event_from_view_submission(
    payload: dict[str, Any], *, resolved_principal: str, content: str,
    channel: str, thread_ts: str,
) -> ConversationEvent:
    """Submissão do modal de direcionamento (Retry) -> kind=approval, o mesmo
    encanamento do clique. O `message_id` vem do id da view (único por
    abertura de modal — duas submissões humanas são dois eventos); `channel`/
    `thread_ts` vêm do private_metadata, porque o payload de view_submission
    não carrega a mensagem de origem."""
    view = payload.get("view") or {}
    message_id = view.get("id") or f"viewsubmit-{payload.get('trigger_id', '0')}"
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=f"{channel}:{thread_ts}",
        message_id=message_id,
        kind=EventKind.approval,
        source_ref={"channel": channel, "thread_ts": thread_ts},
        actor=_actor_from_user_id(payload["user"]["id"], resolved_principal),
        content_snapshot=content,
        signature_verified=True,
    )


def build_repo_select_signal_event(
    payload: dict[str, Any], action: dict[str, Any], *, resolved_principal: str, content: str
) -> ConversationEvent:
    """Repo static_select choice -> ConversationEvent kind=clarification_answer.
    `content` (e.g. 'repo=org/x branch=main') is the marker the dispatcher
    extracts (same regex as C4). message_id = action_ts (unique per click ->
    dedup by event_id; re-selecting produces a new event)."""
    channel = payload["channel"]["id"]
    message = payload.get("message", {})
    thread_ts = message.get("thread_ts", message.get("ts", "0"))
    action_ts = payload.get("action_ts", action.get("action_ts", "0"))
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=f"{channel}:{thread_ts}",
        message_id=action_ts,
        kind=EventKind.clarification_answer,
        source_ref={"channel": channel, "thread_ts": thread_ts},
        actor=_actor_from_user_id(payload["user"]["id"], resolved_principal),
        content_snapshot=content,
        signature_verified=True,
    )
