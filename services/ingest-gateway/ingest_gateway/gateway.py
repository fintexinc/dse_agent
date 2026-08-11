"""WSA-E1-T3 — Transactional gateway (outbox).

`admit_work_item` writes a new WorkItem + its ingest_event in the SAME
Postgres transaction. Task-level idempotency: `idempotency_key` and the
`work_item_id` itself are derived deterministically from
`ConversationEvent.event_id` (which is already a sha256 of
platform+thread+message) — redeliveries of the same webhook (same event_id)
converge to the same work_item_id via `ON CONFLICT ... DO NOTHING`, never
duplicating a row.

The channel kill switch is checked BEFORE any INSERT: an event from a disabled
channel creates neither WorkItem nor ingest_event, and emits
`dse_audit.emit(action="admission_blocked_kill_switch")`.
"""
from __future__ import annotations

import json
from typing import Any

from dse_audit import emit as audit_emit
from dse_contracts import ConversationEvent

from .kill_switch import is_channel_killed
from .db import get_connection


class AdmissionBlocked(Exception):
    """Raised when the channel/tenant kill switch blocks admission. This is not
    an infrastructure error — it is a valid deterministic decision (P1); the
    caller (adapter) should handle it by returning 200 with no side effects
    beyond the audit row this function already emitted."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NonTaskAdmissionRefused(Exception):
    """F2 (fantasmas 1611/1612, 2026-08-09): um evento de kind não-task que
    não correlacionou a item nenhum NÃO vira work item — só `task_request` é
    despachável. Admitir um `approval` produzia DECLINED_UNEXPECTED_STATUS;
    um `clarification_answer`, um signal para workflow que nunca existirá
    (processed=false eterno). Decisão determinística da camada comum, para
    todas as sources; o adapter responde no canal de origem com orientação
    ("responda na conversa da tarefa original")."""

    def __init__(self, kind: str):
        super().__init__(f"non_task_kind_without_correlation:{kind}")
        self.kind = kind


def _payload_json(
    event: ConversationEvent,
    sanitized_content: str | None,
    extra_payload: dict[str, Any] | None = None,
) -> str:
    """`ingest_events.payload`: the full `ConversationEvent` (with the ORIGINAL
    `content_snapshot` intact, frozen by the TOCTOU defense — WSA-E2-T2) plus,
    if provided, `sanitized_content` — the version that went through
    sanitization (WSA-E2-T3) and the one that must be used by any stage that
    involves a model. Never overwrites `content_snapshot`.

    `extra_payload` (Phase 2): deterministic routing keys the dispatcher reads
    to pick the right signal — e.g. `{"merged_by_human": True, "merged_by":
    <principal>, "pr_number": N}` for the merge webhook (WSA-E4-T3), or
    `{"approval_verdict": "rejected", "approval_route": "re_plan"}` for a plan
    approval transition/button (WSA-E6-T3). These are markers set by the
    ADAPTER (deterministic code, P1), never by a model. Never overwrites keys
    of the ConversationEvent itself."""
    data = event.model_dump(mode="json")
    if sanitized_content is not None:
        data["sanitized_content"] = sanitized_content
    if extra_payload:
        for k, v in extra_payload.items():
            data.setdefault(k, v)
    return json.dumps(data)


def admit_work_item(
    event: ConversationEvent,
    *,
    tenant_id: str,
    source: str,
    channel: str,
    requester_principal: str,
    repo: str | None = None,
    base_branch: str | None = None,
    repo_candidates: list[str] | None = None,
    data_class: str = "internal",
    task_class: str = "chore",
    sanitized_content: str | None = None,
    conn=None,
) -> str:
    """Returns the `work_item_id` (new, or the existing one on a redelivery).

    Raises `AdmissionBlocked` if the channel/tenant kill switch is active — in
    that case NO WorkItem/ingest_event is created. Raises
    `NonTaskAdmissionRefused` for any kind that is not `task_request` — a
    non-task event that failed correlation never becomes a WorkItem (F2).
    """
    if event.kind.value != "task_request":
        raise NonTaskAdmissionRefused(event.kind.value)
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    try:
        killed, reason = is_channel_killed(conn, tenant_id, channel)
        if killed:
            audit_emit(
                actor="system:ingest-gateway",
                action="admission_blocked_kill_switch",
                tenant_id=tenant_id,
                details={"channel": channel, "reason": reason, "event_id": event.event_id},
                conn=conn,
            )
            conn.commit()
            raise AdmissionBlocked(reason or "kill_switch_active")

        work_item_id = f"wi_{event.event_id}"
        idempotency_key = event.event_id

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_items
                    (id, tenant_id, source, source_ref, repo, base_branch,
                     requester, data_class, task_class, idempotency_key,
                     repo_candidates)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    work_item_id,
                    tenant_id,
                    source,
                    json.dumps(event.source_ref),
                    repo,
                    base_branch,
                    requester_principal,
                    data_class,
                    task_class,
                    idempotency_key,
                    # O recorte que a ORIGEM impôs, decidido aqui e não
                    # recalculado depois: o `component` de um issue do Jira não
                    # sobrevive em `source_ref`, então reconstruir isso adiante
                    # exigiria uma segunda cópia da montagem de sinais de cada
                    # adapter. Vazio = sem recorte, e o roteador segue vendo o
                    # catálogo do tenant inteiro.
                    list(repo_candidates or []),
                ),
            )
            cur.execute(
                """
                INSERT INTO ingest_events (work_item_id, event_id, kind, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    work_item_id,
                    event.event_id,
                    event.kind.value,
                    _payload_json(event, sanitized_content),
                ),
            )

        audit_emit(
            actor=requester_principal,
            action="work_item_admitted",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"source": source, "channel": channel, "event_id": event.event_id},
            conn=conn,
        )

        conn.commit()
        return work_item_id
    except AdmissionBlocked:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


def record_signal_event(
    event: ConversationEvent,
    *,
    tenant_id: str,
    channel: str,
    work_item_id: str,
    sanitized_content: str | None = None,
    extra_payload: dict[str, Any] | None = None,
    conn=None,
) -> bool:
    """Writes the ConversationEvent of a signal (Path B — correlated to an
    already existing WorkItem) into the same `ingest_events` outbox, WITHOUT
    creating a new `work_items` row. The dispatcher (WSA-E1-T3) drains this
    table and decides between `start_workflow` (kind == task_request, creates
    the workflow) and `signal_workflow` (other kinds, signals the workflow
    already in flight) — see `ingest_gateway.dispatcher`.

    Returns True if this is the first record of this `event_id` (a new signal
    to dispatch), False if it had already been recorded before (webhook
    redelivery — dedup by the UNIQUE `event_id`).
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    try:
        killed, reason = is_channel_killed(conn, tenant_id, channel)
        if killed:
            audit_emit(
                actor="system:ingest-gateway",
                action="admission_blocked_kill_switch",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"channel": channel, "reason": reason, "event_id": event.event_id},
                conn=conn,
            )
            conn.commit()
            raise AdmissionBlocked(reason or "kill_switch_active")

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest_events (work_item_id, event_id, kind, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING id
                """,
                (work_item_id, event.event_id, event.kind.value, _payload_json(event, sanitized_content, extra_payload)),
            )
            is_new = cur.fetchone() is not None

        audit_emit(
            actor="system:ingest-gateway",
            action="signal_recorded" if is_new else "signal_duplicate_ignored",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"channel": channel, "event_id": event.event_id, "kind": event.kind.value},
            conn=conn,
        )
        conn.commit()
        return is_new
    except AdmissionBlocked:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()
