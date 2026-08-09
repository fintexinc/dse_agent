"""F1 — toda mensagem que o bot posta sobre um item entra na correlação do
item. A raiz medida (ingest 1611/1612, 2026-08-09): a pergunta de clarificação
e o prompt de Approve foram postados como MENSAGENS-RAIZ no canal (ts …843 e
…840); o humano respondeu nelas; a correlação só conhece o thread original
(…837) — e "main" e o clique viraram DOIS WORK ITEMS NOVOS, indespacháveis.

Os testes dirigem o caminho REAL de postagem (/internal/status-comment) e
derivam a resposta humana da semântica verdadeira do Slack, via o registro de
thread do FakeSlackClient: responder a mensagem THREADED carrega o thread_ts
da raiz; responder a mensagem de canal cria thread nova. Vermelho antes do
fix; depois do fix (a: post threaded na conversa original; b: bot_ts do prompt
registrado no source_ref para o containment do correlate casar), os mesmos
testes provam consumo pelo item CERTO — inclusive com dois irmãos
compartilhando a mesma thread, onde o mais novo NÃO é o dono do botão.
"""
from __future__ import annotations

import json
import uuid

import psycopg2
from fastapi.testclient import TestClient

import adapter_slack.app as app_module
from adapter_slack.app import app
from adapter_slack.backend import FakeSlackClient

import pytest

from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"

_CH = "C_BOTTHREAD"


@pytest.fixture
def fake_slack(monkeypatch):
    fake = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake)
    return fake


def _post_event(event: dict) -> dict:
    body = json.dumps({"type": "event_callback", "event": event}).encode()
    ts, sig = sign(body)
    resp = client.post(
        "/slack/events", content=body,
        headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig},
    )
    assert resp.status_code == 200
    return resp.json()


def _post_interaction(payload: dict) -> dict:
    body = f"payload={json.dumps(payload)}".encode()
    ts, sig = sign(body)
    resp = client.post(
        "/slack/interactions", content=body,
        headers={
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert resp.status_code == 200
    return resp.json()


def _reply_to(bot_post: dict, *, text: str, user: str, ts: str) -> dict:
    """A resposta humana à mensagem do bot, com a semântica real do Slack:
    o thread_ts do reply é a RAIZ da thread da mensagem respondida — o próprio
    ts dela quando o bot postou solto no canal (o caso medido)."""
    thread_ts = bot_post.get("thread_ts") or bot_post["ts"]
    return {"type": "message", "channel": bot_post["channel"], "user": user,
            "text": text, "ts": ts, "thread_ts": thread_ts}


def _work_items_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_items")
        return cur.fetchone()[0]


def _insert_sibling(conn, *, source_ref: dict, tenant_id: str) -> str:
    """O irmão do fan-out: MESMO source_ref, criado depois (mais novo)."""
    sib = f"wi_sib_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, "
            "idempotency_key, status) VALUES (%s,%s,'slack',%s,'usr_test',%s,'new')",
            (sib, tenant_id, json.dumps(source_ref), f"idem_{sib}"),
        )
    conn.commit()
    return sib


def test_a_reply_in_an_orphan_thread_is_refused_with_guidance_not_admitted(fake_slack):
    """F2, o fantasma 1611 reproduzido: resposta humana numa thread que não
    correlaciona a item nenhum → ZERO itens criados e orientação postada na
    hora — nunca um work item indespachável de kind não-task."""
    conn = psycopg2.connect(DSN)
    try:
        before = _work_items_count(conn)
        result = _post_event({
            "type": "message", "channel": _CH, "user": "U_ORPHAN",
            "text": "main", "ts": "7777.000200", "thread_ts": "7777.000100",
        })
        assert result["path"] == "refused_non_task", (
            "kind não-task sem correlação não vira tarefa — era o caminho do "
            "fantasma processed=false eterno"
        )
        assert _work_items_count(conn) == before, "zero fantasmas"
        assert fake_slack.ephemeral_calls, "a orientação chega na hora, no canal"
        assert "conversa da tarefa" in fake_slack.ephemeral_calls[-1]["text"]
    finally:
        conn.close()


def test_a_reply_to_the_bots_question_reaches_the_item_not_a_new_task(fake_slack):
    """A sequência exata do "main" (ingest 1611): bot pergunta, humano responde
    na mensagem do bot → o item recebe o clarification_answer; ZERO itens
    novos. Hoje: a pergunta rooteia thread nova e a resposta vira tarefa."""
    created = _post_event({
        "type": "app_mention", "channel": _CH, "ts": "8837.000100",
        "user": "U_BOTQ_REQ", "text": "retire payout levels please",
    })
    work_item_id = created["work_item_id"]

    resp = client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH,
        "body": "Qual a base branch?", "actor": "system:orchestrator",
    })
    assert resp.status_code == 200
    bot_post = fake_slack.post_calls[-1]
    # (a) a pergunta vive DENTRO da conversa original do item, nunca como raiz
    assert bot_post.get("thread_ts") == "8837.000100", (
        "a pergunta do bot tem que ser reply na thread do item — mensagem-raiz "
        "é exatamente o que fantasmou o 'main'"
    )

    conn = psycopg2.connect(DSN)
    try:
        before = _work_items_count(conn)
        result = _post_event(_reply_to(bot_post, text="main", user="U_BOTQ_REQ",
                                       ts="8837.000200"))
        assert result["path"] == "signal", "a resposta correlaciona ao item"
        assert result["work_item_id"] == work_item_id
        assert _work_items_count(conn) == before, "zero fantasmas"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ingest_events WHERE work_item_id=%s AND kind='clarification_answer'",
                (work_item_id,),
            )
            assert cur.fetchone()[0] == 1, "o answer entrou na fila do item certo"
    finally:
        conn.close()


def test_the_approve_click_reaches_the_items_plan_gate_not_a_new_task(fake_slack):
    """A sequência exata do Approve (ingest 1612), com o agravante dos irmãos:
    dois itens compartilham a thread original (fan-out), o prompt de Approve é
    do MAIS VELHO — o clique tem que chegar NELE (correlação por bot_ts
    registrado no source_ref; thread compartilhada não desambigua)."""
    created = _post_event({
        "type": "app_mention", "channel": _CH, "ts": "8840.000100",
        "user": "U_BOTA_REQ", "text": "cross repo change please",
    })
    primary = created["work_item_id"]

    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id, source_ref FROM work_items WHERE id=%s", (primary,))
            tenant_id, source_ref = cur.fetchone()
        sibling = _insert_sibling(conn, source_ref=source_ref, tenant_id=tenant_id)

        resp = client.post("/internal/status-comment", json={
            "work_item_id": primary, "channel": _CH,
            "body": "Plan ready — approve?", "actor": "system:orchestrator",
            "status": "awaiting_plan_approval",
        })
        assert resp.status_code == 200
        bot_post = fake_slack.post_calls[-1]
        assert bot_post.get("blocks"), "o prompt levou os botões"
        assert bot_post.get("thread_ts") == "8840.000100", "o prompt vive na conversa do item"

        before = _work_items_count(conn)
        click_message = {"ts": bot_post["ts"]}
        if bot_post.get("thread_ts"):
            click_message["thread_ts"] = bot_post["thread_ts"]
        result = _post_interaction({
            "type": "block_actions",
            "channel": {"id": _CH},
            "message": click_message,
            "user": {"id": "U_BOTA_REQ"},
            "action_ts": "8840.000900",
            "actions": [{"action_id": "dse_plan_approve", "value": "approve"}],
        })
        assert result["path"] == "signal", "o clique correlaciona"
        assert result["work_item_id"] == primary, (
            "o dono do prompt é o item mais VELHO — thread compartilhada com o "
            "irmão mais novo não pode desviar o Approve"
        )
        assert _work_items_count(conn) == before, "zero fantasmas"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ingest_events WHERE work_item_id=%s AND kind='approval'",
                (primary,),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT count(*) FROM ingest_events WHERE work_item_id=%s", (sibling,),
            )
            assert cur.fetchone()[0] == 0, "nada vaza para o irmão"
    finally:
        conn.close()
