"""O botão "How to test" na mensagem final — e o modal que ensina a testar.

O item termina, a mensagem entrega o link do preview e o humano fica sozinho:
sem o usuário de seed, sem saber a tela, sem o caminho. O guia já existe na
linha do preview (`wse_previews.test_guide`, gerado no turno do deep link);
este arquivo pina a última perna: o botão em TODA mensagem `pr_ready`, o
modal com passos+login+link, e as MESMAS três armadilhas do Details — clique
de leitura nunca vira veredito (molde `test_plan_details_modal.py`).
"""
from __future__ import annotations

import json

import psycopg2
import pytest
from fastapi.testclient import TestClient

import adapter_slack.app as app_module
from adapter_slack.app import app
from adapter_slack.backend import FakeSlackClient, status_blocks

from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
_CH = "C_HOWTOTEST"

_GUIA = {"steps": ["Abra /planos", "Clique em Nova Simulação", "Confira a projeção"],
         "login": "demo@acme.com / demo123 (supabase/seed.sql)"}


@pytest.fixture
def fake_slack(monkeypatch):
    fake = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client",
                        lambda token, *, deadline: fake)
    return fake


def _post_event(event: dict) -> dict:
    body = json.dumps({"type": "event_callback", "event": event}).encode()
    ts, sig = sign(body)
    resp = client.post("/slack/events", content=body,
                       headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})
    assert resp.status_code == 200
    return resp.json()


def _post_interaction(payload: dict) -> dict:
    body = f"payload={json.dumps(payload)}".encode()
    ts, sig = sign(body)
    resp = client.post("/slack/interactions", content=body,
                       headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
                                "Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 200
    return resp.json()


def _item_with_a_preview(fake_slack, *, ts: str, guia: dict | None = _GUIA):
    """Um item em pr_ready com preview vivo (e, por padrão, um guia)."""
    work_item_id = _post_event({
        "type": "app_mention", "channel": _CH, "ts": ts,
        "user": "U_HTT_REQ", "text": f"how to test scenario {ts}",
    })["work_item_id"]
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET status='pr_ready' WHERE id=%s",
                        (work_item_id,))
            cur.execute("SELECT tenant_id FROM work_items WHERE id=%s", (work_item_id,))
            tenant_id = cur.fetchone()[0]
            if guia is not None:
                cur.execute(
                    "INSERT INTO wse_previews (work_item_id, tenant_id, pr_number, "
                    "repo, status, url, test_guide) "
                    "VALUES (%s,%s,%s,%s,'created',%s,%s::jsonb) "
                    "ON CONFLICT (work_item_id) DO UPDATE SET "
                    "test_guide=EXCLUDED.test_guide, url=EXCLUDED.url",
                    (work_item_id, tenant_id, 7, "acme/app",
                     "https://preview.example", json.dumps(guia)),
                )
        conn.commit()
    finally:
        conn.close()
    resp = client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH,
        "body": "🔗 Preview ready — open it and decide.",
        "actor": "system:orchestrator", "status": "pr_ready",
    })
    assert resp.status_code == 200
    return work_item_id, fake_slack.post_calls[-1]


def _click(post: dict, *, user: str = "U_HTT_REQ") -> dict:
    return _post_interaction({
        "type": "block_actions",
        "channel": {"id": _CH},
        "message": {"ts": post["ts"], "thread_ts": post.get("thread_ts")},
        "user": {"id": user},
        "trigger_id": "trigger.htt",
        "actions": [{"action_id": "dse_how_to_test", "value": "how_to_test"}],
    })


# --- o botão ----------------------------------------------------------------

def test_the_pr_ready_message_carries_the_button_and_the_gate_does_not():
    ids = [e["action_id"] for b in status_blocks("x", status="pr_ready")
           if b["type"] == "actions" for e in b["elements"]]
    assert "dse_how_to_test" in ids, "a mensagem final não oferece o How to test"

    no_gate = [e["action_id"] for b in status_blocks("x", status="awaiting_plan_approval")
               if b["type"] == "actions" for e in b["elements"]]
    assert "dse_how_to_test" not in no_gate, (
        "no gate ainda não há preview — o botão mentiria"
    )


# --- o modal ----------------------------------------------------------------

def test_clicking_opens_a_modal_with_steps_login_and_the_link(fake_slack):
    _, post = _item_with_a_preview(fake_slack, ts="8001.000100")
    result = _click(post)

    assert result.get("path") == "how_to_test_opened", result
    assert fake_slack.views_open_calls, "nenhum modal foi aberto"
    view = fake_slack.views_open_calls[-1]["view"]
    assert view["type"] == "modal"
    rendered = json.dumps(view, ensure_ascii=False)
    assert "Nova Simulação" in rendered, "os passos têm que aparecer"
    assert "demo@acme.com" in rendered, "o login de seed é o motivo do botão"
    assert "https://preview.example" in rendered, "sem o link o guia é teoria"
    assert view.get("submit") is None, "modal de leitura não tem submit"


def test_without_a_guide_the_click_answers_honestly(fake_slack):
    _, post = _item_with_a_preview(fake_slack, ts="8002.000100", guia=None)
    result = _click(post)
    assert result.get("path") == "how_to_test_no_guide", result
    assert not fake_slack.views_open_calls


def test_how_to_test_never_becomes_a_verdict(fake_slack):
    """As três armadilhas do Details, de novo: nenhum signal, nenhum veredito
    consumido, a mensagem intacta com os botões."""
    work_item_id, post = _item_with_a_preview(fake_slack, ts="8003.000100")
    _click(post)

    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ingest_events WHERE work_item_id=%s",
                        (work_item_id,))
            assert cur.fetchone()[0] == 0, "o clique virou evento"
            cur.execute("SELECT count(*) FROM verdict_consumptions WHERE work_item_id=%s",
                        (work_item_id,))
            assert cur.fetchone()[0] == 0, "o clique consumiu o veredito one-shot"
    finally:
        conn.close()
    assert not [u for u in fake_slack.update_calls if u["ts"] == post["ts"]], (
        "o clique reescreveu a mensagem — os botões somem"
    )
