"""F1, pins por adapter (GitHub/Jira) — o equivalente de "thread" em cada
canal, nomeado e provado no nível do correlate:

  - GitHub: a conversa é a ISSUE — `source_ref = {repo, number}`. O comentário
    de status do bot é um comment NA MESMA issue; o comment humano seguinte
    carrega o mesmo ref por construção. Não existe o conceito de "rootar
    thread nova".
  - Jira: a conversa é o TICKET — `source_ref = {ticket_key}`. Comment-chain
    plano; todo comment carrega o ticket_key.

O Slack era o único canal onde o bot podia criar um identificador de conversa
novo (mensagem-raiz) — a raiz medida nos fantasmas 1611/1612. Estes pins
congelam a construção dos outros dois: se um dia o ref de comentário deixar de
conter o ref do item, quebra AQUI, não com um humano respondendo no escuro.
"""
from __future__ import annotations

import json
import uuid

import psycopg2

from dse_contracts import Actor, ConversationEvent, EventKind, Platform
from ingest_gateway import correlate

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _mk_item(conn, *, source: str, source_ref: dict, requester: str) -> str:
    wi = f"wi_pin_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, "
            "idempotency_key, status) VALUES (%s,'dev-tenant',%s,%s,%s,%s,'needs_clarification')",
            (wi, source, json.dumps(source_ref), requester, f"idem_{wi}"),
        )
    conn.commit()
    return wi


def _answer(platform: Platform, *, source_ref: dict, principal: str) -> ConversationEvent:
    return ConversationEvent.build(
        platform=platform,
        thread_key=f"pin:{uuid.uuid4().hex[:8]}",
        message_id=uuid.uuid4().hex[:10],
        kind=EventKind.clarification_answer,
        source_ref=source_ref,
        actor=Actor(platform_user_id="pin-user", resolved_principal=principal),
        content_snapshot="main",
        signature_verified=True,
    )


def test_github_reply_after_bot_comment_correlates_by_issue():
    conn = psycopg2.connect(DSN)
    try:
        ref = {"repo": "acme/app", "number": 77}
        wi = _mk_item(conn, source="github", source_ref=ref, requester="usr_gh_req")
        result = correlate(
            conn, tenant_id="dev-tenant",
            event=_answer(Platform.github, source_ref=ref, principal="usr_gh_req"),
            requester_principal="usr_gh_req",
        )
        conn.commit()
        assert result.kind == "signal" and result.work_item_id == wi
    finally:
        conn.close()


def test_jira_reply_after_bot_comment_correlates_by_ticket():
    conn = psycopg2.connect(DSN)
    try:
        ref = {"ticket_key": "BD-42"}
        wi = _mk_item(conn, source="jira", source_ref=ref, requester="usr_jira_req")
        result = correlate(
            conn, tenant_id="dev-tenant",
            event=_answer(Platform.jira, source_ref=ref, principal="usr_jira_req"),
            requester_principal="usr_jira_req",
        )
        conn.commit()
        assert result.kind == "signal" and result.work_item_id == wi
    finally:
        conn.close()
