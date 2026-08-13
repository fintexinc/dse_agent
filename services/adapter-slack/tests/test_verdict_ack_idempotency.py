"""Item 3 da rc do canal mínimo — ack visível e idempotência one-shot.

(a) Todo clique atualiza a mensagem original IN-PLACE: os botões somem e ela
    vira "✅ Approved by @X at HH:MM" / "🚫 Rejected by..." — quem olha a
    thread vê a decisão e o decisor, não um prompt eternamente pendente.
(b) Clique duplicado (ou em veredito já consumido) responde ephemeral
    "já resolvido por X às HH:MM" e NÃO emite segundo signal. O workflow já
    consome one-shot do lado dele, mas um segundo signal re-arma a flag DEPOIS
    do consumo — e o próximo parque do mesmo item se auto-resolveria com o
    veredito velho (a classe do wi_8edaef39). O anticorpo é na borda.
(c) O re-parque re-arma: renderizar os botões de novo é uma NOVA decisão.
(d) Transição sem botões LIMPA os botões velhos: chat.update sem `blocks`
    PRESERVA os antigos no Slack real — todo edit do writer manda blocks
    (vazio quando o status não tem).
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

_CH = "C_ACKIDEM"


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


def _gated_item(fake_slack, *, ts: str) -> tuple[str, dict]:
    from dse_identity import resolve_principal

    created = _post_event({
        "type": "app_mention", "channel": _CH, "ts": ts,
        "user": "U_ACK_REQ", "text": f"ack scenario {ts}",
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
                (tenant_id, resolve_principal("slack", "U_ACK_REQ")),
            )
        conn.commit()
    finally:
        conn.close()
    resp = client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH,
        "body": "Parqueado: spec quebrada.", "actor": "system:orchestrator",
        "status": "awaiting_plan_approval",
    })
    assert resp.status_code == 200
    return work_item_id, fake_slack.post_calls[-1]


def _click(post: dict, *, action_id: str, value: str, action_ts: str, user: str = "U_ACK_REQ") -> dict:
    return _post_interaction({
        "type": "block_actions",
        "channel": {"id": _CH},
        "message": {"ts": post["ts"], "thread_ts": post.get("thread_ts")},
        "user": {"id": user},
        "action_ts": action_ts,
        "actions": [{"action_id": action_id, "value": value}],
    })


def _event_count(work_item_id: str) -> int:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ingest_events WHERE work_item_id=%s AND kind='approval'",
                (work_item_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_a_duplicate_click_emits_exactly_one_signal_and_tells_the_late_clicker(fake_slack):
    """Dois cliques no mesmo botão → UM evento. O segundo recebe ephemeral
    'já resolvido por X' — nunca um segundo signal fantasma re-armando a flag
    consumida do workflow."""
    work_item_id, post = _gated_item(fake_slack, ts="9101.000100")
    first = _click(post, action_id="dse_plan_reject", value="reject",
                   action_ts="9101.000900")
    assert first["path"] == "signal"
    second = _click(post, action_id="dse_plan_reject", value="reject",
                    action_ts="9101.000901")
    assert second["path"] == "already_resolved", (
        "o segundo clique não pode virar segundo signal — é ele que re-arma a "
        "flag consumida e auto-resolve o PRÓXIMO parque com o veredito velho"
    )
    assert _event_count(work_item_id) == 1, "um clique humano = um signal"
    assert fake_slack.ephemeral_calls, "quem clicou tarde precisa saber"
    assert "already resolved" in fake_slack.ephemeral_calls[-1]["text"].lower()


def test_the_click_acks_in_place_and_the_buttons_disappear(fake_slack):
    """O clique atualiza a mensagem original: botões fora, decisão e decisor
    dentro — '🚫 Rejected by @X at HH:MM'.

    O cenário era o parque de spec até 2026-08-10; o parque saiu, o invariante
    (consumo one-shot + ack no lugar) é da infra de veredito e vale igual na
    aprovação de plano, que é onde ele passa a ser exercitado."""
    _, post = _gated_item(fake_slack, ts="9102.000100")
    _click(post, action_id="dse_plan_reject", value="reject",
           action_ts="9102.000900")
    updates = [u for u in fake_slack.update_calls if u["ts"] == post["ts"]]
    assert updates, "o ack é o chat.update na PRÓPRIA mensagem do prompt"
    ack = updates[-1]
    assert "<@U_ACK_REQ>" in ack["text"], "o decisor aparece no ack"
    assert "Rejected" in ack["text"]
    for block in ack.get("blocks") or []:
        assert block.get("type") != "actions", "os botões somem no ack"


def test_the_approve_click_acks_with_the_approved_stamp(fake_slack):
    """O mesmo contrato no prompt de plano: '✅ Approved by @X at HH:MM'."""
    created = _post_event({
        "type": "app_mention", "channel": _CH, "ts": "9103.000100",
        "user": "U_ACK_REQ", "text": "plan approval ack scenario",
    })
    work_item_id = created["work_item_id"]
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE work_items SET status='awaiting_plan_approval' WHERE id=%s",
                (work_item_id,),
            )
        conn.commit()
    finally:
        conn.close()
    resp = client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH,
        "body": "Plan ready — approve?", "actor": "system:orchestrator",
        "status": "awaiting_plan_approval",
    })
    assert resp.status_code == 200
    post = fake_slack.post_calls[-1]
    result = _click(post, action_id="dse_plan_approve", value="approve",
                    action_ts="9103.000900")
    assert result["path"] == "signal"
    updates = [u for u in fake_slack.update_calls if u["ts"] == post["ts"]]
    assert updates and "Approved" in updates[-1]["text"] and "✅" in updates[-1]["text"]


def test_a_repark_rearms_the_decision(fake_slack):
    """PIN do re-arm: o mesmo item pode parquear DE NOVO (retry → falha → novo
    parque). Renderizar os botões outra vez re-arma a decisão — o clique novo
    grava um SEGUNDO evento, não um 'já resolvido'."""
    work_item_id, post = _gated_item(fake_slack, ts="9104.000100")
    _click(post, action_id="dse_plan_reject", value="reject",
           action_ts="9104.000900")
    assert _event_count(work_item_id) == 1
    # o re-parque: o workflow posta o status de novo (upsert edita a mesma msg)
    resp = client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH,
        "body": "Parqueado de novo: outra spec.", "actor": "system:orchestrator",
        "status": "awaiting_plan_approval",
    })
    assert resp.status_code == 200
    result = _click(post, action_id="dse_plan_reject", value="reject",
                    action_ts="9104.000901")
    assert result["path"] == "signal", "re-parque = nova decisão, o botão volta a valer"
    assert _event_count(work_item_id) == 2


def test_a_transition_without_buttons_clears_the_stale_blocks(fake_slack):
    """chat.update sem `blocks` PRESERVA os blocos antigos no Slack real — a
    transição pós-decisão (implementing etc.) precisa tirar os botões de
    decisão, senão o prompt continua clicável para sempre. Desde a rc.90 o
    edit re-renderiza os blocks inteiros (layout novo); a proteção passa a
    ser: blocks presentes E sem Approve/Reject."""
    work_item_id, post = _gated_item(fake_slack, ts="9105.000100")
    resp = client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH,
        "body": "⚙️ implementando de novo", "actor": "system:orchestrator",
    })
    assert resp.status_code == 200
    updates = [u for u in fake_slack.update_calls if u["ts"] == post["ts"]]
    assert updates, "o upsert edita a mesma mensagem"
    latest = updates[-1]["blocks"]
    assert latest, (
        "o edit tem que mandar blocks (re-render) — omitir preserva os "
        "botões velhos no Slack real"
    )
    action_ids = {e.get("action_id") for b in latest if b.get("type") == "actions"
                  for e in b.get("elements", [])}
    assert "dse_plan_approve" not in action_ids, "Approve não sobrevive à transição"
    assert "dse_plan_reject" not in action_ids, "Reject não sobrevive à transição"
