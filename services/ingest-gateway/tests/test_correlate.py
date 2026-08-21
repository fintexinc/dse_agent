"""WSA-E6-T1 — correlate(): Path A (new_task) vs Path B (signal) e a regra do
WorkItem terminal (provenance).

Os cinco testes da allowlist de DIREÇÃO saíram em 2026-08-21 com o mecanismo
(decisão do operador): quem está no canal fala com o DSE. O que substituiu está
em test_anyone_in_the_channel_can_steer.py, que pina o novo contrato — inclusive
que a aprovação de plano NÃO saiu junto.
ORIGINAL: WSA-E6-T1 — correlate(): Path A (new_task) vs Path B (signal), terminal
WorkItem rule (provenance), and the steering allowlist gate (WSA-E6-T2a)."""
from __future__ import annotations

import json
import uuid

import psycopg2
from dse_contracts import Actor, ConversationEvent, EventKind, Platform

from ingest_gateway.correlate import correlate
from ingest_gateway.db import get_connection

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _insert_work_item(tenant_id: str, source_ref: dict, status: str = "implementing") -> str:
    work_item_id = f"wi_test_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_items (id, tenant_id, source, source_ref, requester, status, idempotency_key)
            VALUES (%s, %s, 'slack', %s::jsonb, 'usr_original_requester', %s, %s)
            """,
            (work_item_id, tenant_id, json.dumps(source_ref), status, f"idem_{work_item_id}"),
        )
    conn.commit()
    conn.close()
    return work_item_id


def _event(kind: EventKind, source_ref: dict, principal: str = "usr_someone") -> ConversationEvent:
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=json.dumps(source_ref, sort_keys=True),
        message_id=uuid.uuid4().hex,
        kind=kind,
        source_ref=source_ref,
        actor=Actor(platform_user_id="U999", resolved_principal=principal),
        content_snapshot="some content",
        signature_verified=True,
    )


def test_no_match_returns_new_task(tenant_id):
    conn = get_connection()
    ref = {"channel": "C1", "thread_ts": "999.999"}
    event = _event(EventKind.task_request, ref)

    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_someone")

    assert result.kind == "new_task"
    assert result.work_item_id is None


def test_match_on_active_work_item_returns_signal(tenant_id):
    ref = {"channel": "C1", "thread_ts": "111.111"}
    wi_id = _insert_work_item(tenant_id, ref, status="implementing")

    # clarification_answer is steering-gated (§F F4): the legitimate requester is
    # on the allowlist; here we authorize so the test focuses on Path B correlation.
    allow_conn = psycopg2.connect(DSN)
    with allow_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) VALUES (%s, %s)",
            (tenant_id, "usr_someone"),
        )
    allow_conn.commit()
    allow_conn.close()

    conn = get_connection()
    event = _event(EventKind.clarification_answer, ref)
    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_someone")

    assert result.kind == "signal"
    assert result.work_item_id == wi_id


def test_match_on_terminal_work_item_returns_new_task_with_provenance(tenant_id):
    ref = {"channel": "C1", "thread_ts": "222.222"}
    old_wi_id = _insert_work_item(tenant_id, ref, status="done")

    conn = get_connection()
    event = _event(EventKind.task_request, ref)
    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_someone")

    assert result.kind == "new_task"
    assert result.work_item_id is None
    assert result.provenance_work_item_id == old_wi_id


def test_steering_by_authorized_principal_is_signal(tenant_id):
    ref = {"repo": "acme/widgets", "number": 43}
    wi_id = _insert_work_item(tenant_id, ref, status="implementing")

    allow_conn = psycopg2.connect(DSN)
    with allow_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) VALUES (%s, %s)",
            (tenant_id, "usr_maintainer"),
        )
    allow_conn.commit()
    allow_conn.close()

    conn = get_connection()
    event = _event(EventKind.steering, ref, principal="usr_maintainer")
    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_maintainer")

    assert result.kind == "signal"
    assert result.work_item_id == wi_id


def test_clarification_answer_by_third_party_on_allowlist_is_signal(tenant_id):
    """A third party who is NOT the requester but is authorized (allowlist) can
    answer as well."""
    ref = {"channel": "C1", "thread_ts": "555.555"}
    wi_id = _insert_work_item(tenant_id, ref, status="needs_clarification")

    allow_conn = psycopg2.connect(DSN)
    with allow_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) VALUES (%s, %s)",
            (tenant_id, "usr_helper"),
        )
    allow_conn.commit()
    allow_conn.close()

    conn = get_connection()
    event = _event(EventKind.clarification_answer, ref, principal="usr_helper")
    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_helper")

    assert result.kind == "signal"
    assert result.work_item_id == wi_id


