"""F2 — Path A não cria item de kind não-task, para nenhuma source. Medido
nos fantasmas 1611/1612 (2026-08-09): "main" (clarification_answer) e o clique
de Approve (approval) sem correlação viraram work_items status='new' — mas só
task_request é despachável: o approval morreu em DECLINED_UNEXPECTED_STATUS e
o clarification_answer ficou processed=false eternamente (signal para workflow
que nunca existirá). Um item nascido de kind não-task é estruturalmente
insolúvel. A recusa vive na camada COMUM (admit_work_item): kind != task_request
→ NonTaskAdmissionRefused, zero linhas. Vermelho antes do fix.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from dse_contracts import Actor, ConversationEvent, EventKind, Platform
from ingest_gateway import admit_work_item

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _event(kind: EventKind) -> ConversationEvent:
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=f"refuse:{uuid.uuid4().hex[:8]}",
        message_id=uuid.uuid4().hex[:10],
        kind=kind,
        source_ref={"channel": "C_REFUSE", "thread_ts": "999.001"},
        actor=Actor(platform_user_id="U_REFUSE", resolved_principal="usr_refuse"),
        content_snapshot="main",
        signature_verified=True,
    )


@pytest.mark.parametrize("kind", [EventKind.clarification_answer, EventKind.approval])
def test_admit_refuses_non_task_kinds_and_writes_nothing(kind):
    from ingest_gateway import NonTaskAdmissionRefused

    ev = _event(kind)
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM work_items")
            items_before = cur.fetchone()[0]
        with pytest.raises(NonTaskAdmissionRefused):
            admit_work_item(
                ev, tenant_id="dev-tenant", source="slack", channel="C_REFUSE",
                repo=None, base_branch=None, requester_principal="usr_refuse",
                sanitized_content="main", conn=conn,
            )
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM work_items")
            assert cur.fetchone()[0] == items_before, "recusa não escreve linha nenhuma"
            cur.execute("SELECT count(*) FROM ingest_events WHERE event_id = %s", (ev.event_id,))
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()
