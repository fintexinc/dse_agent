"""A6 (rc do canal mínimo) — vereditos de parque como botões no Slack.

Medido na cena do caso 3: o FE parqueou em `spec_conflict` com dossiê de três
specs e a única saída era um signal manual por terminal. A mensagem de parque
ganha botões que emitem o veredito pelo MESMO encanamento do Approve:

  [Retry]   → modal com direcionamento OPCIONAL (o texto viaja como
              fix_context — o canal da rc.54, comment→instrução)
  [Escalar] → veredito direto
  [Reauthor] SÓ no parque de spec própria do Tester (tester_spec_exhaustion) —
              veredito inaplicável não é renderizado (no parque de spec de
              CLIENTE, reauthor não autoriza ninguém e cairia em escalate).

`discard` NÃO existe como veredito no workflow (verificado em 2026-08-09) —
pela mesma regra, não há botão Discard.
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

_CH = "C_PARKBTN"


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


def _parked_item(fake_slack, *, ts: str, park_reason: str | None = None) -> tuple[str, dict]:
    """Um item real parqueado: admissão pelo caminho normal, status
    `spec_conflict` no banco (como o workflow deixa), e a mensagem de parque
    postada pelo endpoint REAL — devolve (work_item_id, bot_post)."""
    from dse_identity import resolve_principal

    created = _post_event({
        "type": "app_mention", "channel": _CH, "ts": ts,
        "user": "U_PARK_REQ", "text": f"park scenario {ts}",
    })
    work_item_id = created["work_item_id"]
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET status='spec_conflict' WHERE id=%s",
                        (work_item_id,))
            # O clique de parque passa pelo gate de steering (deny-by-default,
            # paridade com o repo_confirm) — o decisor entra na allowlist.
            cur.execute("SELECT tenant_id FROM work_items WHERE id=%s", (work_item_id,))
            tenant_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) "
                "VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (tenant_id, resolve_principal("slack", "U_PARK_REQ")),
            )
        conn.commit()
    finally:
        conn.close()
    body = {
        "work_item_id": work_item_id, "channel": _CH,
        "body": "Pre-existing spec(s) broken. Specs: a.spec.ts. Expected 3, Received 2.",
        "actor": "system:orchestrator", "status": "spec_conflict",
    }
    if park_reason:
        body["park_reason"] = park_reason
    resp = client.post("/internal/status-comment", json=body)
    assert resp.status_code == 200
    return work_item_id, fake_slack.post_calls[-1]


def _action_ids(post: dict) -> list[str]:
    out = []
    for block in post.get("blocks") or []:
        for el in block.get("elements", []) if block.get("type") == "actions" else []:
            out.append(el.get("action_id"))
    return out


def test_the_park_message_renders_retry_and_escalate_buttons(fake_slack):
    """Hoje a mensagem de spec_conflict sai SEM botões — a decisão humana só
    entra por terminal. Depois: Retry/Escalar no Block Kit."""
    _, post = _parked_item(fake_slack, ts="9001.000100")
    ids = _action_ids(post)
    assert "dse_park_retry" in ids, "o parque precisa do botão Retry"
    assert "dse_park_escalate" in ids, "o parque precisa do botão Escalar"
    assert "dse_park_reauthor" not in ids, (
        "reauthor não existe no parque de spec de CLIENTE — veredito "
        "inaplicável não é renderizado"
    )


def test_the_exhaustion_park_also_renders_reauthor(fake_slack):
    """`tester_spec_exhaustion` é o parque de spec PRÓPRIA do Tester — o único
    contexto onde reauthor existe como veredito."""
    _, post = _parked_item(fake_slack, ts="9002.000100",
                           park_reason="tester_spec_exhaustion")
    ids = _action_ids(post)
    assert "dse_park_retry" in ids and "dse_park_escalate" in ids
    assert "dse_park_reauthor" in ids


def test_the_escalate_click_records_the_verdict_through_the_approve_plumbing(fake_slack):
    """Escalar é direto (sem modal): o clique vira ingest_event com o marker
    `park_verdict` — o mesmo caminho outbox→dispatcher→signal do Approve."""
    work_item_id, post = _parked_item(fake_slack, ts="9003.000100")
    result = _post_interaction({
        "type": "block_actions",
        "channel": {"id": _CH},
        "message": {"ts": post["ts"], "thread_ts": post.get("thread_ts")},
        "user": {"id": "U_PARK_REQ"},
        "action_ts": "9003.000900",
        "actions": [{"action_id": "dse_park_escalate", "value": "escalate"}],
    })
    assert result["path"] == "signal"
    assert result["work_item_id"] == work_item_id
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload->>'park_verdict' FROM ingest_events "
                "WHERE work_item_id=%s AND kind='approval' ORDER BY id DESC LIMIT 1",
                (work_item_id,),
            )
            row = cur.fetchone()
        assert row and row[0] == "escalate", (
            "o veredito do botão tem que virar marker determinístico no evento"
        )
    finally:
        conn.close()


def test_the_retry_click_opens_the_direction_modal_and_records_nothing_yet(fake_slack):
    """Retry abre o modal de direcionamento (campo opcional) — o veredito só
    nasce na SUBMISSÃO. O clique em si não grava evento nenhum."""
    work_item_id, post = _parked_item(fake_slack, ts="9004.000100")
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ingest_events WHERE work_item_id=%s AND kind='approval'",
                        (work_item_id,))
            before = cur.fetchone()[0]
    finally:
        conn.close()
    _post_interaction({
        "type": "block_actions",
        "trigger_id": "trig_9004",
        "channel": {"id": _CH},
        "message": {"ts": post["ts"], "thread_ts": post.get("thread_ts")},
        "user": {"id": "U_PARK_REQ"},
        "action_ts": "9004.000900",
        "actions": [{"action_id": "dse_park_retry", "value": "retry"}],
    })
    assert fake_slack.views_open_calls, "o Retry abre o modal (views.open)"
    view = fake_slack.views_open_calls[-1]["view"]
    meta = json.loads(view["private_metadata"])
    assert meta["channel"] == _CH and meta["message_ts"] == post["ts"]
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ingest_events WHERE work_item_id=%s AND kind='approval'",
                        (work_item_id,))
            assert cur.fetchone()[0] == before, "o clique do Retry ainda não é veredito"
    finally:
        conn.close()


def test_the_retry_modal_opens_through_the_real_ratelimit_wrapper(monkeypatch):
    """MEDIDO em produção (2026-08-10 10:01:52, primeiro clique real do
    canal): o Retry caiu em `AttributeError: 'RateLimitedSlackClient' object
    has no attribute 'views_open'`. O fake dos testes tinha o método; o
    wrapper REAL não — o fixture que troca `build_real_slack_client` pelo fake
    puro escondeu exatamente a camada que quebrou. Este teste monta o wrapper
    de produção em volta do fake, como `build_real_slack_client` monta em
    volta do WebClient."""
    import time as _time

    from adapter_slack.ratelimit import RateLimitedSlackClient

    fake = FakeSlackClient()
    monkeypatch.setattr(
        app_module, "build_real_slack_client",
        lambda token, *, deadline: RateLimitedSlackClient(fake, deadline=_time.monotonic()),
    )
    _, post = _parked_item_with(fake, ts="9006.000100")
    _post_interaction({
        "type": "block_actions",
        "trigger_id": "trig_9006",
        "channel": {"id": _CH},
        "message": {"ts": post["ts"], "thread_ts": post.get("thread_ts")},
        "user": {"id": "U_PARK_REQ"},
        "action_ts": "9006.000900",
        "actions": [{"action_id": "dse_park_retry", "value": "retry"}],
    })
    assert fake.views_open_calls, (
        "o wrapper de produção tem que expor views_open — sem isso o Retry "
        "morre em AttributeError e o humano fica sem modal"
    )


def _parked_item_with(fake, *, ts: str) -> tuple[str, dict]:
    """Como _parked_item, mas com um fake já embrulhado fora do fixture."""
    from dse_identity import resolve_principal

    created = _post_event({
        "type": "app_mention", "channel": _CH, "ts": ts,
        "user": "U_PARK_REQ", "text": f"park scenario {ts}",
    })
    work_item_id = created["work_item_id"]
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET status='spec_conflict' WHERE id=%s",
                        (work_item_id,))
            cur.execute("SELECT tenant_id FROM work_items WHERE id=%s", (work_item_id,))
            tenant_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) "
                "VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (tenant_id, resolve_principal("slack", "U_PARK_REQ")),
            )
        conn.commit()
    finally:
        conn.close()
    resp = client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH,
        "body": "Parqueado.", "actor": "system:orchestrator",
        "status": "spec_conflict",
    })
    assert resp.status_code == 200
    return work_item_id, fake.post_calls[-1]


def test_the_modal_submission_records_retry_with_the_direction_as_fix_context(fake_slack):
    """A submissão do modal é o veredito: retry + o texto livre viajando como
    `fix_context` (rc.54: comment→instrução do Coder)."""
    work_item_id, post = _parked_item(fake_slack, ts="9005.000100")
    _post_interaction({
        "type": "view_submission",
        "user": {"id": "U_PARK_REQ"},
        "view": {
            "callback_id": "dse_park_retry",
            "private_metadata": json.dumps({
                "channel": _CH, "message_ts": post["ts"],
                "thread_ts": post.get("thread_ts") or post["ts"],
            }),
            "state": {"values": {"dse_park_ctx": {"dse_park_ctx_input": {
                "type": "plain_text_input",
                "value": "as três specs pinam o shape antigo — atualize-as",
            }}}},
        },
    })
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload->>'park_verdict', payload->>'fix_context' "
                "FROM ingest_events WHERE work_item_id=%s AND kind='approval' "
                "ORDER BY id DESC LIMIT 1",
                (work_item_id,),
            )
            row = cur.fetchone()
        assert row and row[0] == "retry", "a submissão grava o veredito retry"
        assert row[1] == "as três specs pinam o shape antigo — atualize-as", (
            "o direcionamento viaja como fix_context"
        )
    finally:
        conn.close()
