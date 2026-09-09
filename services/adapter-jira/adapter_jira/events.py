"""Jira -> `ConversationEvent` normalization (WSA-E5-T1).

`source_ref` normalized as `{"ticket_key": "DSE-123"}` — correlation by ticket
key (the same issue serves for task, clarification and plan approval).

TOCTOU defense (WSA-E2-T2): `content_snapshot` comes straight from the received
payload (webhook) or from the issue state returned by the search (poller) — it
is never re-fetched afterwards.

IMPORTANT — webhook×poller idempotency (WSA-E5-T2): the `message_id`s are
derived from the issue STATE (issue id + status/column, comment id), NEVER from
the webhook changelog id. That way the webhook (best-effort) and the poller
(which only sees the current state, with no changelog) produce the SAME
deterministic `event_id` for the same fact — redelivery through either of the
two paths dedupes via the UNIQUE constraint on `ingest_events.event_id`, never
duplicating.
"""
from __future__ import annotations

from typing import Any

from dse_contracts import Actor, ConversationEvent, EventKind, Platform


def ticket_key(issue: dict[str, Any]) -> str:
    return issue["key"]


def project_key(issue: dict[str, Any]) -> str:
    """Used as the kill switch/admission `channel` (granularity per Jira
    project). Derived from `fields.project.key` or from the ticket key prefix."""
    proj = (issue.get("fields", {}).get("project") or {}).get("key")
    if proj:
        return proj
    key = issue.get("key", "")
    return key.split("-", 1)[0] if "-" in key else key


def issue_labels(issue: dict[str, Any]) -> list[str]:
    return list(issue.get("fields", {}).get("labels") or [])


def issue_type(issue: dict[str, Any]) -> str | None:
    """Name of the Jira issue type (Bug/Story/Task/...) — plan 08 §A: feeds the
    deterministic task_class classification at admission."""
    return (issue.get("fields", {}).get("issuetype") or {}).get("name")


def first_component(issue: dict[str, Any]) -> str | None:
    """Name of the 1st Jira Component (C2/report 07): Components map issues to
    subsystems/services — the finest-grained repo signal Jira has. None if the
    ticket has no component."""
    comps = issue.get("fields", {}).get("components") or []
    for c in comps:
        name = (c or {}).get("name")
        if name:
            return name
    return None


def issue_status_name(issue: dict[str, Any]) -> str:
    return (issue.get("fields", {}).get("status") or {}).get("name", "")


def has_trigger_label(issue: dict[str, Any], trigger_label: str) -> bool:
    return trigger_label in issue_labels(issue)


def _actor(account_id: str, resolved_principal: str, display_name: str | None) -> Actor:
    return Actor(platform_user_id=account_id, resolved_principal=resolved_principal, display_name=display_name)


def _issue_content(issue: dict[str, Any]) -> str:
    fields = issue.get("fields", {})
    summary = fields.get("summary", "") or ""
    description = fields.get("description")
    # API v3 returns the description as ADF (a dict); the webhook may carry
    # plain text. Flatten instead of dropping: the description is where people
    # write the acceptance criteria, so discarding it left the DSE with nothing
    # but the summary and it asked for clarification on a ticket that already
    # answered the question. On BD-40 that question then came back as its own
    # answer and took the whole task down with it.
    if isinstance(description, str):
        desc_text = description
    else:
        from .backend import _flatten_adf

        desc_text = _flatten_adf(description) if description else ""
    return f"{summary}\n\n{desc_text}".strip()


def build_task_event(
    issue: dict[str, Any],
    *,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
    generation: int = 0,
) -> ConversationEvent:
    """Issue marked with the trigger label -> task_request. `message_id`
    derived from the issue id (state), so create+label+poller converge on the
    same event_id.

    `generation` counts the times a human made the gesture — took the label off
    and put it back (see `trigger_state`). It is what lets a card be worked more
    than once: without it the id derived here is a lifetime constant of the card
    and every attempt after the first collides with the first.

    **Generation 0 is spelled without a suffix, and that is compatibility, not
    style.** `created:{issue id}` is the event_id of every card already ingested
    in the fleet. Any suffix here — `:0` included — changes all of them at once,
    and the first sweep after the deploy re-admits the fleet.
    """
    if generation < 0:
        raise ValueError(f"generation must be >= 0, got {generation}")
    suffix = "" if generation == 0 else f":{generation}"
    return ConversationEvent.build(
        platform=Platform.jira,
        thread_key=ticket_key(issue),
        message_id=f"created:{issue['id']}{suffix}",
        kind=EventKind.task_request,
        source_ref={"ticket_key": ticket_key(issue)},
        actor=_actor(actor_account_id, resolved_principal, display_name),
        content_snapshot=_issue_content(issue),
        signature_verified=True,
    )


def build_comment_event(
    *,
    key: str,
    comment_id: str,
    body: str,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
) -> ConversationEvent:
    """Comment on the issue -> clarification_answer by default (same convention
    as adapter-slack for an ordinary in-thread message; `correlate` decides
    Path A/B, and the steering gate handles review/steering). `message_id` is
    the comment id — the poller fetches comments and sees the same id."""
    return ConversationEvent.build(
        platform=Platform.jira,
        thread_key=key,
        message_id=f"comment:{comment_id}",
        kind=EventKind.clarification_answer,
        source_ref={"ticket_key": key},
        actor=_actor(actor_account_id, resolved_principal, display_name),
        content_snapshot=body,
        signature_verified=True,
    )


def build_status_approval_event(
    issue: dict[str, Any],
    *,
    target_status: str,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
) -> ConversationEvent:
    """Status transition into the configured approval column -> kind=approval
    (UC5 on the Jira surface, WSA-E5-T1). `message_id` derived from
    (issue id, target status) — state, not changelog — so the poller rebuilds
    the same event_id."""
    return ConversationEvent.build(
        platform=Platform.jira,
        thread_key=ticket_key(issue),
        message_id=f"status:{issue['id']}:{target_status}",
        kind=EventKind.approval,
        source_ref={"ticket_key": ticket_key(issue)},
        actor=_actor(actor_account_id, resolved_principal, display_name),
        content_snapshot=f"status transition -> {target_status}",
        signature_verified=True,
    )
