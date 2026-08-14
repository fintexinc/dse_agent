"""Single normalized event (§10.2, FR-01). Every adapter (Slack/GitHub/Jira)
produces exclusively this — no code downstream of the intake knows the native
format of any platform.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, field_validator


class Platform(str, Enum):
    slack = "slack"
    github = "github"
    jira = "jira"
    # Ativado em 2026-08-14, junto com a migração 0043 que abre os quatro
    # CHECKs de plataforma. O adapter existe desde a Fase 4 e esperava só isto.
    teams = "teams"


class EventKind(str, Enum):
    task_request = "task_request"
    clarification_answer = "clarification_answer"
    approval = "approval"
    review_comment = "review_comment"
    steering = "steering"


class Actor(BaseModel):
    """Identity of whoever triggered the event — raw and (when available) resolved."""

    platform_user_id: str
    resolved_principal: str | None = None
    display_name: str | None = None


class ConversationEvent(BaseModel):
    """Normalized contract consumed by the ingest-gateway and the whole system.

    `event_id` is the event-level idempotency key (not to be confused with
    `WorkItem.idempotency_key`, which is task-level) — two retried webhooks of
    the same `platform+thread+message` must collide on `event_id`.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    event_id: str
    platform: Platform
    kind: EventKind
    source_ref: dict[str, Any]  # {thread_ts, channel} | {repo, issue_number} | {ticket_key}
    actor: Actor
    content_snapshot: str  # content frozen at mention time (TOCTOU defense, FR-03)
    received_at: datetime
    signature_verified: bool

    @field_validator("received_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @staticmethod
    def compute_event_id(platform: str, thread_key: str, message_id: str) -> str:
        raw = f"{platform}:{thread_key}:{message_id}".encode()
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def build(
        cls,
        *,
        platform: Platform,
        thread_key: str,
        message_id: str,
        kind: EventKind,
        source_ref: dict[str, Any],
        actor: Actor,
        content_snapshot: str,
        signature_verified: bool,
        received_at: datetime | None = None,
    ) -> "ConversationEvent":
        """Builds the event deriving `event_id` deterministically — never
        generate `event_id` by hand outside this helper (it is what guarantees
        the dedup defense)."""
        return cls(
            event_id=cls.compute_event_id(platform.value, thread_key, message_id),
            platform=platform,
            kind=kind,
            source_ref=source_ref,
            actor=actor,
            content_snapshot=content_snapshot,
            signature_verified=signature_verified,
            received_at=received_at or datetime.now(timezone.utc),
        )
