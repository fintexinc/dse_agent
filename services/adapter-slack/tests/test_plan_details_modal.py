"""O plano tem que ser LEGÍVEL antes de ser aprovado.

Hoje a mensagem do gate é uma frase e dois botões:

    📋 Plan ready — awaiting human approval (risk: —).
    [ Approve ]  [ Reject ]

O plano em si — os passos, os arquivos que ele promete tocar, o orçamento de
diff, os caminhos proibidos — está no banco (`work_items.plan`, JSONB) e não
aparece em lugar nenhum. Nada no repositório renderiza `plan["steps"]`: o
humano aprova um plano que não pode ler. Este arquivo fecha isso com um terceiro
botão que abre um modal.

**A armadilha, e é por isso que este teste existe.** Um botão novo na mensagem
de aprovação não é inócuo — ele cai no fallthrough de `/slack/interactions`, e
lá acontecem três coisas, nesta ordem:

  1. `parse_slack_approval` devolve `("approved", None)` para QUALQUER
     `action_id` que não case os tokens de rejeição — inclusive um chamado
     "details". Ou seja: **o clique em Details aprovaria o plano.**
  2. `_stage_for_action` casa o prefixo `dse_plan_` e o veredito é CONSUMIDO
     em `verdict_consumptions`; o Approve de verdade, depois, vira
     "já resolvido".
  3. `_finish_verdict_click` → `_ack_update` reescreve a mensagem só com uma
     `section` — **os botões somem**.

Cada uma dessas três é suficiente para transformar "quero ler o plano" em "o
plano foi aprovado". O teste prova as três, não só a primeira.
"""
from __future__ import annotations

import json

import psycopg2
import pytest
from fastapi.testclient import TestClient

import adapter_slack.app as app_module
from adapter_slack.app import app
from adapter_slack.backend import FakeSlackClient

from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
_CH = "C_PLANDETAILS"

_PLAN = {
    "work_item_id": "wi_x",
    "steps": [
        "Add a `retired` boolean to the payout level entity",
        "Exclude retired levels from the advisor fee calculation",
    ],
    "expected_files": [
        "src/main/java/com/fintex/domain/PayoutLevel.java",
        "src/main/java/com/fintex/service/AdvisorFeeCalculationService.java",
    ],
    "test_plan": "Unit tests on the calculation service with a retired level",
    "risk_class": "high",
    "diff_budget_lines": 400,
    "forbidden_paths": [".github/workflows/", "migrations/"],
    "no_code_change": False,
}


@pytest.fixture
def fake_slack(monkeypatch):
    fake = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake)
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


def _item_at_the_plan_gate(fake_slack, *, ts: str) -> tuple[str, dict]:
    """Um item parado no gate de plano, COM plano no banco — que é o estado
    real quando a mensagem é postada (`persist-plan-before-coder-v1` grava o
    plano antes do gate)."""
    from dse_identity import resolve_principal

    work_item_id = _post_event({
        "type": "app_mention", "channel": _CH, "ts": ts,
        "user": "U_PLAN_REQ", "text": f"plan details scenario {ts}",
    })["work_item_id"]
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE work_items SET status='awaiting_plan_approval', plan=%s::jsonb "
                "WHERE id=%s",
                (json.dumps(_PLAN), work_item_id),
            )
            cur.execute("SELECT tenant_id FROM work_items WHERE id=%s", (work_item_id,))
            tenant_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) "
                "VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (tenant_id, resolve_principal("slack", "U_PLAN_REQ")),
            )
        conn.commit()
    finally:
        conn.close()
    resp = client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH,
        "body": "📋 Plan ready — awaiting human approval (risk: high).",
        "actor": "system:orchestrator", "status": "awaiting_plan_approval",
    })
    assert resp.status_code == 200
    return work_item_id, fake_slack.post_calls[-1]


def _click_details(post: dict, *, user: str = "U_PLAN_REQ") -> dict:
    return _post_interaction({
        "type": "block_actions",
        "channel": {"id": _CH},
        "message": {"ts": post["ts"], "thread_ts": post.get("thread_ts")},
        "user": {"id": user},
        "trigger_id": "trigger.123",
        "actions": [{"action_id": "dse_plan_details", "value": "details"}],
    })


def _verdicts_consumed(work_item_id: str) -> int:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM verdict_consumptions WHERE work_item_id=%s",
                (work_item_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def _signals(work_item_id: str) -> int:
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


def test_the_gate_message_offers_a_details_button(fake_slack):
    _, post = _item_at_the_plan_gate(fake_slack, ts="7001.000100")
    actions = [b for b in post["blocks"] if b["type"] == "actions"][0]
    ids = {e["action_id"] for e in actions["elements"]}
    assert ids == {"dse_plan_approve", "dse_plan_reject", "dse_plan_details"}


def test_clicking_details_opens_a_modal_with_the_plan(fake_slack):
    """O conteúdo tem que vir do BANCO. Um modal que mostra a mesma frase da
    mensagem não resolve nada — o que falta ao humano são os passos e os
    arquivos prometidos."""
    _, post = _item_at_the_plan_gate(fake_slack, ts="7002.000100")
    result = _click_details(post)

    assert result.get("path") == "plan_details_opened", result
    assert fake_slack.views_open_calls, "nenhum modal foi aberto"
    view = fake_slack.views_open_calls[-1]["view"]
    assert view["type"] == "modal"
    rendered = json.dumps(view, ensure_ascii=False)
    assert "Exclude retired levels" in rendered, "os passos do plano têm que aparecer"
    assert "AdvisorFeeCalculationService.java" in rendered, "e os arquivos prometidos"
    assert "high" in rendered, "e o risco, que é o que decide quem pode aprovar"


def test_details_never_becomes_a_verdict(fake_slack):
    """As três armadilhas de uma vez: nenhum signal nasce, nenhum veredito é
    consumido, e a mensagem original continua com os botões."""
    work_item_id, post = _item_at_the_plan_gate(fake_slack, ts="7003.000100")
    _click_details(post)

    assert _signals(work_item_id) == 0, (
        "o clique em Details virou um evento de aprovação — `parse_slack_approval` "
        "devolve 'approved' para qualquer action_id que não seja de rejeição"
    )
    assert _verdicts_consumed(work_item_id) == 0, (
        "o Details queimou a decisão one-shot; o Approve de verdade viraria "
        "'já resolvido'"
    )
    assert not [u for u in fake_slack.update_calls if u["ts"] == post["ts"]], (
        "o Details reescreveu a mensagem — os botões somem e o gate fica sem "
        "como ser aprovado"
    )


def test_approve_still_works_after_reading_the_details(fake_slack):
    """PIN do caminho feliz inteiro: ler o plano e DEPOIS aprovar tem que
    funcionar. É a sequência que o humano realmente faz."""
    work_item_id, post = _item_at_the_plan_gate(fake_slack, ts="7004.000100")
    _click_details(post)
    approve = _post_interaction({
        "type": "block_actions", "channel": {"id": _CH},
        "message": {"ts": post["ts"], "thread_ts": post.get("thread_ts")},
        "user": {"id": "U_PLAN_REQ"}, "trigger_id": "trigger.124",
        "actions": [{"action_id": "dse_plan_approve", "value": "approve"}],
    })

    assert approve.get("path") == "signal", approve
    assert _signals(work_item_id) == 1
    updates = [u for u in fake_slack.update_calls if u["ts"] == post["ts"]]
    assert updates and "Approved" in updates[-1]["text"]


def test_a_missing_plan_does_not_break_the_button(fake_slack):
    """Fronteira: o gate pode ser re-renderizado por um lembrete depois de um
    `continue_as_new`, e nada garante que o plano esteja lá. O modal abre
    dizendo o que sabe em vez de estourar — o botão nunca pode derrubar a
    única via de aprovação."""
    from dse_identity import resolve_principal

    work_item_id = _post_event({
        "type": "app_mention", "channel": _CH, "ts": "7005.000100",
        "user": "U_PLAN_REQ", "text": "no plan scenario",
    })["work_item_id"]
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
                (tenant_id, resolve_principal("slack", "U_PLAN_REQ")),
            )
        conn.commit()
    finally:
        conn.close()
    client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH, "body": "📋 Plan ready.",
        "actor": "system:orchestrator", "status": "awaiting_plan_approval",
    })
    post = fake_slack.post_calls[-1]

    result = _click_details(post)
    assert result.get("path") == "plan_details_opened"
    rendered = json.dumps(fake_slack.views_open_calls[-1]["view"], ensure_ascii=False)
    assert "not available" in rendered.lower() or "no plan" in rendered.lower()
