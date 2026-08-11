"""Item 4 (rc do canal mínimo), a metade SÍNCRONA — clique em item já
encerrado. Hoje o clique recebe um ephemeral genérico ("I could not find the
tarefa...") que aponta para o lado errado: a tarefa EXISTE, ela terminou. A
mensagem clicada tem que dizer isso — "⚠️ Could not apply: ..." — e
nenhum signal fantasma pode nascer.
"""
from __future__ import annotations

import json

import psycopg2
from fastapi.testclient import TestClient

import adapter_slack.app as app_module
from adapter_slack.app import app
from adapter_slack.backend import FakeSlackClient

import pytest

from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"

_CH = "C_DELIVFAIL"


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


def test_a_click_on_a_finished_item_updates_the_message_and_signals_nothing(fake_slack):
    from dse_identity import resolve_principal

    created = _post_event({
        "type": "app_mention", "channel": _CH, "ts": "9201.000100",
        "user": "U_DF_REQ", "text": "task that will finish",
    })
    work_item_id = created["work_item_id"]
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET status='awaiting_plan_approval' WHERE id=%s",
                        (work_item_id,))
            cur.execute("SELECT tenant_id FROM work_items WHERE id=%s", (work_item_id,))
            tenant_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) "
                "VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (tenant_id, resolve_principal("slack", "U_DF_REQ")),
            )
        conn.commit()
    finally:
        conn.close()
    resp = client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH,
        "body": "Parqueado.", "actor": "system:orchestrator",
        "status": "awaiting_plan_approval",
    })
    assert resp.status_code == 200
    post = fake_slack.post_calls[-1]

    # ...e o item TERMINA antes do humano decidir (cancel de operador, TTL,
    # qualquer corrida real). O botão continua na tela.
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET status='failed' WHERE id=%s", (work_item_id,))
            cur.execute("SELECT count(*) FROM ingest_events WHERE work_item_id=%s", (work_item_id,))
            before = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    _post_interaction({
        "type": "block_actions",
        "channel": {"id": _CH},
        "message": {"ts": post["ts"], "thread_ts": post.get("thread_ts")},
        "user": {"id": "U_DF_REQ"},
        "action_ts": "9201.000900",
        "actions": [{"action_id": "dse_plan_reject", "value": "reject"}],
    })

    updates = [u for u in fake_slack.update_calls if u["ts"] == post["ts"]]
    assert updates, (
        "a falha tem que aparecer NA MENSAGEM clicada — o ephemeral genérico "
        "'não encontrei a tarefa' aponta para o lado errado (ela existe, terminou)"
    )
    assert "Could not apply" in updates[-1]["text"]
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ingest_events WHERE work_item_id=%s", (work_item_id,))
            assert cur.fetchone()[0] == before, "zero signal fantasma"
    finally:
        conn.close()
