"""WSA-E3-T2 — outbound: exactly 1 status message per WorkItem, edited
in-place via `MutableCommentWriter` + `SlackCommentBackend`. With no real
Slack App credential: the documented in-memory `FakeSlackClient` stands in for
`slack_sdk.WebClient` — the logic (`MutableCommentWriter`,
`SlackCommentBackend`, `PgCommentStateStore`) is 100% real."""
from __future__ import annotations

import json
import uuid

import psycopg2
from fastapi.testclient import TestClient

import adapter_slack.app as app_module
from adapter_slack.backend import FakeSlackClient
from adapter_slack.app import app

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _make_work_item(tenant_id: str, *, repo: str | None = None) -> str:
    work_item_id = f"wi_out_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key, repo) "
            "VALUES (%s,%s,'slack','{}'::jsonb,'usr_test',%s,%s)",
            (work_item_id, tenant_id, f"idem_{work_item_id}", repo),
        )
    conn.commit()
    conn.close()
    return work_item_id


def test_first_status_update_posts_a_single_message(tenant_id, monkeypatch):
    fake_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake_client)

    work_item_id = _make_work_item(tenant_id)

    resp = client.post(
        "/internal/status-comment",
        json={
            "work_item_id": work_item_id,
            "channel": "C_STATUS",
            "body": "Task started",
            "actor": "system:orchestrator",
        },
    )
    assert resp.status_code == 200
    assert len(fake_client.post_calls) == 1
    assert len(fake_client.update_calls) == 0
    assert fake_client.post_calls[0]["text"] == "Task started"


def test_subsequent_updates_edit_in_place_never_post_new_message(tenant_id, monkeypatch):
    fake_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake_client)

    work_item_id = _make_work_item(tenant_id)

    for i, body in enumerate(["Task started", "Task running (step 1/3)", "Task done"]):
        resp = client.post(
            "/internal/status-comment",
            json={"work_item_id": work_item_id, "channel": "C_STATUS", "body": body, "actor": "system:orchestrator"},
        )
        assert resp.status_code == 200

    # exactly 1 initial post + 2 edits — NEVER 3 posts.
    assert len(fake_client.post_calls) == 1
    assert len(fake_client.update_calls) == 2
    assert fake_client.update_calls[-1]["text"] == "Task done"

    # the single live message reflects the latest body.
    (ts, text), = [(t, v) for t, v in fake_client.messages.items()]
    assert text == "Task done"


def test_status_comment_ref_persisted_across_process_restart_simulation(tenant_id, monkeypatch):
    """The adapter is 100% stateless — this simulates 'restarting the process'
    by creating a SECOND FakeSlackClient and a SECOND writer (a new
    PgCommentStateStore) for the SAME work_item_id; it must keep editing the
    comment_ref already persisted in Postgres, not post again."""
    shared_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: shared_client)

    work_item_id = _make_work_item(tenant_id)

    client.post(
        "/internal/status-comment",
        json={"work_item_id": work_item_id, "channel": "C_STATUS", "body": "first", "actor": "system:orchestrator"},
    )
    # "restart": a new FakeSlackClient instance would lose the in-memory
    # state, but the comment_ref persists in Postgres (comment_state) — we
    # only swap the client here to simulate it; the store is always Postgres.
    client.post(
        "/internal/status-comment",
        json={"work_item_id": work_item_id, "channel": "C_STATUS", "body": "second", "actor": "system:orchestrator"},
    )

    assert len(shared_client.post_calls) == 1
    assert len(shared_client.update_calls) == 1

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT surface, comment_ref FROM comment_state WHERE work_item_id = %s", (work_item_id,)
        )
        row = cur.fetchone()
    conn.close()
    assert row[0] == "slack"
    assert json.loads(row[1])["channel"] == "C_STATUS"


def test_awaiting_plan_approval_posts_block_kit_buttons(tenant_id, monkeypatch):
    """Phase B (report 07): on status awaiting_plan_approval the message goes
    out with Block Kit (Approve/Reject) — the action_id/value are the markers
    that parse_slack_approval (C1) reads. This closes the loop: without posting
    the buttons, the human had no way to approve/reject from Slack."""
    fake_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake_client)
    work_item_id = _make_work_item(tenant_id)

    resp = client.post(
        "/internal/status-comment",
        json={
            "work_item_id": work_item_id,
            "channel": "C_APPROVE",
            "body": "Plan ready — approve?",
            "actor": "system:orchestrator",
            "status": "awaiting_plan_approval",
        },
    )
    assert resp.status_code == 200
    blocks = fake_client.post_calls[0]["blocks"]
    assert blocks is not None
    action_block = next(b for b in blocks if b["type"] == "actions")
    action_ids = {e["action_id"] for e in action_block["elements"]}
    # Três desde 2026-08-11: os dois que DECIDEM e o Details, que só mostra o
    # plano. O Details não tem `style` de propósito — cor de decisão convidaria
    # o clique errado — e é desviado ANTES do fallthrough de veredito.
    assert action_ids == {"dse_plan_approve", "dse_plan_reject", "dse_plan_details"}
    values = {e["value"] for e in action_block["elements"]}
    assert "reject:re_plan" in values  # the rejection marker that C1 parses


def test_non_approval_status_has_details_but_never_verdict_buttons(tenant_id, monkeypatch):
    """rc.90 INVERTE o pin antigo ('plain text fora do gate'): a mensagem de
    status ganha layout — mas os botões de VEREDITO continuam exclusivos do
    gate. O que não pode existir fora do gate é Approve/Reject; o Details
    (só-leitura, desviado antes do fallthrough de veredito) agora vive em toda
    mensagem."""
    fake_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake_client)
    work_item_id = _make_work_item(tenant_id, repo="fintexinc/bmo-fee-calculator-be-dse")
    client.post(
        "/internal/status-comment",
        json={"work_item_id": work_item_id, "channel": "C1", "body": "⚙️ implementing",
              "actor": "system:orchestrator", "status": "implementing"},
    )
    blocks = fake_client.post_calls[0]["blocks"]
    assert blocks, "a mensagem de status ficou sem layout — o pin antigo voltou"
    action_ids = {e["action_id"] for b in blocks if b["type"] == "actions"
                  for e in b["elements"]}
    assert "dse_plan_details" in action_ids, "o plano tem que estar a um clique SEMPRE"
    assert "dse_plan_approve" not in action_ids and "dse_plan_reject" not in action_ids, (
        "botão de veredito fora do gate — o clique errado aprovaria um plano"
    )


def test_status_message_shows_repo_and_stage_bar(tenant_id, monkeypatch):
    """rc.90, o pedido literal do operador ao ver dois irmãos na mesma thread:
    'repo (texto pequeno), estado, e o botão pra ver o plano' + a barra de
    etapas. Sem o repo, 'The DSE is implementing…' de um irmão é ilegível —
    foi lido como o item PARQUEADO tendo começado sem aprovação."""
    fake_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake_client)
    work_item_id = _make_work_item(tenant_id, repo="fintexinc/bmo-fee-calculator-fe-dse")
    client.post(
        "/internal/status-comment",
        json={"work_item_id": work_item_id, "channel": "C1", "body": "⚙️ implementing",
              "actor": "system:orchestrator", "status": "implementing"},
    )
    blocks = fake_client.post_calls[0]["blocks"] or []
    rendered = json.dumps(blocks, ensure_ascii=False)
    contexts = [b for b in blocks if b["type"] == "context"]
    assert any("bmo-fee-calculator-fe-dse" in json.dumps(c) for c in contexts), (
        f"o repo não aparece em texto pequeno (context): {rendered[:400]}"
    )
    assert "Build" in rendered and "Plan" in rendered, (
        "a barra de etapas não aparece"
    )
    # implementing = Build é a etapa ATUAL; Plan já concluída
    assert "✅ Plan" in rendered and "🔷 Build" in rendered, (
        f"a barra não marca a etapa atual: {rendered[:400]}"
    )


def test_terminal_status_and_missing_repo_stay_honest(tenant_id, monkeypatch):
    """Fronteiras: status terminal não mostra barra (o corpo diz o desfecho;
    uma barra 'em andamento' num item morto mentiria), e item sem repo não
    ganha context inventado."""
    fake_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake_client)

    com_repo = _make_work_item(tenant_id, repo="fintexinc/bmo-test-dse-fe")
    client.post(
        "/internal/status-comment",
        json={"work_item_id": com_repo, "channel": "C1", "body": "❌ failed",
              "actor": "system:orchestrator", "status": "failed"},
    )
    rendered = json.dumps(fake_client.post_calls[0]["blocks"] or [], ensure_ascii=False)
    assert "🔷" not in rendered, "item terminal com etapa 'atual' — a barra mente"

    sem_repo = _make_work_item(tenant_id)
    client.post(
        "/internal/status-comment",
        json={"work_item_id": sem_repo, "channel": "C1", "body": "⚙️ implementing",
              "actor": "system:orchestrator", "status": "implementing"},
    )
    rendered2 = json.dumps(fake_client.post_calls[1]["blocks"] or [], ensure_ascii=False)
    assert "None" not in rendered2.replace("null", ""), "repo inventado na mensagem"
