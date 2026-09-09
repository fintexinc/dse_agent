"""WSA-E1-T3 — admit_work_item: transactional write (work_items +
ingest_events in the same transaction), idempotency by event_id, and the
channel kill switch blocking admission (with an audit row)."""
from __future__ import annotations

import uuid

import psycopg2
import pytest
from dse_contracts import Actor, ConversationEvent, EventKind, Platform

from ingest_gateway.gateway import AdmissionBlocked, admit_work_item
from ingest_gateway.db import get_connection


def _make_event(thread_key: str, message_id: str, content: str = "please fix the bug") -> ConversationEvent:
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=thread_key,
        message_id=message_id,
        kind=EventKind.task_request,
        source_ref={"channel": "C123", "thread_ts": thread_key},
        actor=Actor(platform_user_id="U123", resolved_principal="usr_test_requester"),
        content_snapshot=content,
        signature_verified=True,
    )


def test_admit_work_item_creates_work_item_and_ingest_event(tenant_id):
    conn = get_connection()
    thread_key = f"thread_{uuid.uuid4().hex[:8]}"
    event = _make_event(thread_key, "m1")

    work_item_id = admit_work_item(
        event,
        tenant_id=tenant_id,
        source="slack",
        channel="C123",
        requester_principal="usr_test_requester",
        conn=conn,
    )

    assert work_item_id == f"wi_{event.event_id}"

    check_conn = psycopg2.connect(
        "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
    )
    with check_conn.cursor() as cur:
        cur.execute("SELECT tenant_id, source, idempotency_key FROM work_items WHERE id = %s", (work_item_id,))
        row = cur.fetchone()
        assert row == (tenant_id, "slack", event.event_id)

        cur.execute("SELECT event_id, kind, processed FROM ingest_events WHERE work_item_id = %s", (work_item_id,))
        ie_row = cur.fetchone()
        assert ie_row == (event.event_id, "task_request", False)

        cur.execute(
            "SELECT action FROM audit_log WHERE work_item_id = %s AND action = 'work_item_admitted'",
            (work_item_id,),
        )
        assert cur.fetchone() is not None
    check_conn.close()


def test_admit_work_item_is_idempotent_on_replay(tenant_id):
    thread_key = f"thread_{uuid.uuid4().hex[:8]}"
    event = _make_event(thread_key, "m1")

    conn1 = get_connection()
    id1 = admit_work_item(
        event, tenant_id=tenant_id, source="slack", channel="C123",
        requester_principal="usr_test_requester", conn=conn1,
    )

    # Redelivery of the SAME webhook (same deterministic event_id) —
    # must not create a second row in work_items or ingest_events.
    conn2 = get_connection()
    id2 = admit_work_item(
        event, tenant_id=tenant_id, source="slack", channel="C123",
        requester_principal="usr_test_requester", conn=conn2,
    )

    assert id1 == id2

    check_conn = psycopg2.connect("postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")
    with check_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_items WHERE id = %s", (id1,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM ingest_events WHERE work_item_id = %s", (id1,))
        assert cur.fetchone()[0] == 1
        # The ledger has to say the same thing the tables say. `audit_emit` used
        # to fire unconditionally, so a card whose label sits on it produced one
        # `work_item_admitted` PER SWEEP — 13 of them in twelve minutes on
        # BFA-1132 (2026-09-09), with zero dispatches behind them, which reads
        # like thirteen admissions that never ran. The sibling
        # `record_signal_event` has always done this right, with `RETURNING id`.
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE work_item_id = %s AND action = 'work_item_admitted'",
            (id1,),
        )
        assert cur.fetchone()[0] == 1, "a replayed admission wrote a second admitted row"
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE work_item_id = %s "
            "AND action = 'work_item_admission_duplicate_ignored'",
            (id1,),
        )
        assert cur.fetchone()[0] == 1, "the replay left no trace at all — it must be named"
    check_conn.close()


def test_kill_switch_blocks_admission_and_audits(tenant_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO channel_kill_switches (tenant_id, channel, active, reason) VALUES (%s,%s,true,%s)",
            (tenant_id, "C_KILLED", "incident_123"),
        )
    conn.commit()
    conn.close()

    thread_key = f"thread_{uuid.uuid4().hex[:8]}"
    event = _make_event(thread_key, "m1")

    conn2 = get_connection()
    with pytest.raises(AdmissionBlocked):
        admit_work_item(
            event, tenant_id=tenant_id, source="slack", channel="C_KILLED",
            requester_principal="usr_test_requester", conn=conn2,
        )

    check_conn = psycopg2.connect("postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")
    with check_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_items WHERE tenant_id = %s", (tenant_id,))
        assert cur.fetchone()[0] == 0

        cur.execute(
            "SELECT action, details FROM audit_log WHERE tenant_id = %s AND action = 'admission_blocked_kill_switch'",
            (tenant_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "admission_blocked_kill_switch"
    check_conn.close()
