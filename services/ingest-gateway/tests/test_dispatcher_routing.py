"""WSA-E6-T3 — signal routing by WorkItem STATUS + merge override
(WSA-E4-T3).

Two layers:
  1. `_route_signal(status, kind, payload)` — pure decision logic
     (deterministic, P1): tested exhaustively branch by branch.
  2. Real integration (Postgres + Temporal): the `Dispatcher` drains an
     approval ingest_event and emits the audit row with the correct signal,
     consuming the event (never mocked — CONVENTIONS.md).
"""
from __future__ import annotations

import asyncio
import json
import uuid

import psycopg2
import pytest
from temporalio.client import Client

from dse_contracts import TASK_QUEUE, WORKFLOW_TYPE
from dse_contracts.constants import (
    SIGNAL_CLARIFICATION_ANSWER,
    SIGNAL_MERGED_BY_HUMAN,
    SIGNAL_PLAN_APPROVAL,
    SIGNAL_REVIEW_COMMENT,
)
from ingest_gateway.dispatcher import DispatchOutcome, _dispatch_row, _route_signal

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
TEMPORAL_ADDRESS = "localhost:7233"


# --------------------------------------------------------------------------
# 1. Pure routing logic
# --------------------------------------------------------------------------
def _payload(content="ok", **extra):
    p = {"content_snapshot": content, "actor": {"resolved_principal": "usr_alice"}}
    p.update(extra)
    return p


def test_approval_with_awaiting_plan_approval_routes_to_plan_approval():
    route = _route_signal("awaiting_plan_approval", "approval", _payload())
    assert route.signal_name == SIGNAL_PLAN_APPROVAL
    assert route.payload["verdict"] == "approved"
    assert route.payload["actor"] == "usr_alice"


def test_approval_rejected_carries_route_when_rejected():
    route = _route_signal(
        "awaiting_plan_approval", "approval", _payload(approval_verdict="rejected", approval_route="re_clarify")
    )
    assert route.signal_name == SIGNAL_PLAN_APPROVAL
    assert route.payload["verdict"] == "rejected"
    assert route.payload["route"] == "re_clarify"


def test_approval_rejected_defaults_route_when_missing():
    route = _route_signal("awaiting_plan_approval", "approval", _payload(approval_verdict="rejected"))
    assert route.payload["verdict"] == "rejected"
    assert route.payload["route"] == "re_plan"  # required when rejected (WSB-E3-T3)


def test_approval_with_pr_ready_routes_to_review_comment():
    route = _route_signal("pr_ready", "approval", _payload())
    assert route.signal_name == SIGNAL_REVIEW_COMMENT
    assert route.payload["verdict"] == "approved"


def test_approval_with_unexpected_status_declines_never_guesses():
    route = _route_signal("implementing", "approval", _payload())
    assert route.signal_name is None
    assert route.reason == "unexpected_status"


def test_merge_marker_routes_to_merged_by_human_regardless_of_status():
    route = _route_signal(
        "pr_ready", "approval", _payload(merged_by_human=True, merged_by="usr_bob", pr_number=42)
    )
    assert route.signal_name == SIGNAL_MERGED_BY_HUMAN
    assert route.payload["merged_by"] == "usr_bob"
    assert route.payload["pr_number"] == 42


def test_clarification_answer_preserved_from_phase1():
    route = _route_signal("needs_clarification", "clarification_answer", _payload("answer"))
    assert route.signal_name == SIGNAL_CLARIFICATION_ANSWER
    assert route.payload["acceptance_criteria"] == "answer"


def test_review_comment_with_verdict_preserved():
    p = _payload("change this", source_ref={"review_state": "changes_requested"})
    route = _route_signal("pr_ready", "review_comment", p)
    assert route.signal_name == SIGNAL_REVIEW_COMMENT
    assert route.payload["verdict"] == "changes_requested"


def test_review_comment_without_verdict_is_not_a_decision():
    route = _route_signal("pr_ready", "review_comment", _payload("just a comment"))
    assert route.signal_name is None
    assert route.reason == "review_comment_no_verdict"


# --------------------------------------------------------------------------
# 2. Real integration (Postgres + Temporal)
#
# Isolation note: this phase's shared environment has the
# `dse_ingest_dispatcher` container (Phase 1 code, old `kind`-based routing)
# running `run_forever` against the SAME outbox. A test that inserted into
# `ingest_events` and called `drain_once` would compete with that container
# (SKIP LOCKED) — and, worse, the container routes with the old map. That is
# why the routing INTEGRATION here exercises `_dispatch_row` DIRECTLY (the new
# code, WSA-E6-T3), delivering a REAL signal to a REAL Temporal workflow —
# genuine signal durability/delivery (P8), without going through the contended
# outbox.
# --------------------------------------------------------------------------
def _create_work_item_and_workflow(client, tenant_id: str, status: str, *, start: bool = True) -> str:
    work_item_id = f"wi_route_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key, status) "
            "VALUES (%s,%s,'github',%s::jsonb,'usr_alice',%s,%s)",
            (work_item_id, tenant_id, json.dumps({"repo": "acme/x", "number": 1}), f"idem_{work_item_id}", status),
        )
    conn.commit()
    conn.close()
    if start:
        asyncio.get_event_loop().run_until_complete(
            client.start_workflow(WORKFLOW_TYPE, work_item_id, id=work_item_id, task_queue=TASK_QUEUE)
        )
    return work_item_id


@pytest.fixture
def loop():
    return asyncio.new_event_loop()


@pytest.fixture
def temporal_client(loop):
    return loop.run_until_complete(Client.connect(TEMPORAL_ADDRESS))


def test_approval_dispatch_signals_plan_approval_when_awaiting(tenant_id, temporal_client, loop):
    wi = _create_work_item_and_workflow(temporal_client, tenant_id, "awaiting_plan_approval")
    outcome, details = loop.run_until_complete(
        _dispatch_row(
            temporal_client,
            work_item_id=wi,
            event_id="evt_x",
            kind="approval",
            status="awaiting_plan_approval",
            payload={"content_snapshot": "approved", "actor": {"resolved_principal": "usr_alice"}},
        )
    )
    assert outcome == DispatchOutcome.SIGNALED
    assert details["signal"] == SIGNAL_PLAN_APPROVAL


def test_merge_event_dispatch_signals_merged_by_human(tenant_id, temporal_client, loop):
    wi = _create_work_item_and_workflow(temporal_client, tenant_id, "pr_ready")
    outcome, details = loop.run_until_complete(
        _dispatch_row(
            temporal_client,
            work_item_id=wi,
            event_id="evt_y",
            kind="approval",
            status="pr_ready",
            payload={"content_snapshot": "merged", "merged_by_human": True, "merged_by": "usr_bob", "pr_number": 2},
        )
    )
    assert outcome == DispatchOutcome.SIGNALED
    assert details["signal"] == SIGNAL_MERGED_BY_HUMAN


def test_approval_unexpected_status_is_declined_never_guesses(tenant_id, temporal_client, loop):
    wi = _create_work_item_and_workflow(temporal_client, tenant_id, "implementing", start=False)
    outcome, details = loop.run_until_complete(
        _dispatch_row(
            temporal_client,
            work_item_id=wi,
            event_id="evt_z",
            kind="approval",
            status="implementing",
            payload={"content_snapshot": "approved?", "actor": {"resolved_principal": "usr_alice"}},
        )
    )
    assert outcome == DispatchOutcome.DECLINED_UNEXPECTED_STATUS
    assert details["status"] == "implementing"


def test_a_plain_approval_on_spec_conflict_still_declines_never_guesses():
    """Um clique de Approve/Reject de PLANO (sem marker de parque) num item em
    spec_conflict continua recusado — P6: rota só com o marker determinístico
    do botão de parque, nunca por adivinhação."""
    route = _route_signal("spec_conflict", "approval", _payload("button:dse_plan_approve=approve"))
    assert route.signal_name is None
    assert route.reason == "unexpected_status"


# --------------------------------------------------------------------------
# Item 4 (rc do canal mínimo) — falha de entrega NUNCA silenciosa. As duas
# formas medidas: (a) o decline P6 era um beco mudo — o clique parecia ter
# funcionado e o humano nunca soube (o caso A1 produziu exatamente isso, 2x);
# (b) signal para workflow morto era retry INFINITO (`will be retried`, sem
# dead-letter) — o "workflow not found for ID" de hoje (wi_8a7036e7) ficaria
# eterno na fila.
# --------------------------------------------------------------------------
def test_a_declined_verdict_notifies_the_channel_instead_of_dying_mute(
    tenant_id, temporal_client, loop, monkeypatch
):
    from ingest_gateway import dispatcher as dispatcher_module

    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dispatcher_module, "_notify_undeliverable",
        lambda work_item_id, reason: notified.append((work_item_id, reason)),
    )
    wi = _create_work_item_and_workflow(temporal_client, tenant_id, "implementing", start=False)
    outcome, _ = loop.run_until_complete(
        _dispatch_row(
            temporal_client, work_item_id=wi, event_id="evt_mute_1",
            kind="approval", status="implementing",
            payload={"content_snapshot": "approved?", "actor": {"resolved_principal": "usr_alice"}},
        )
    )
    assert outcome == DispatchOutcome.DECLINED_UNEXPECTED_STATUS
    assert notified and notified[0][0] == wi, (
        "o decline tem que virar mensagem no canal — o clique que parece ter "
        "funcionado e não funcionou é a falha silenciosa em pessoa"
    )
    assert "implementing" in notified[0][1], "o motivo nomeia o estado real"


def test_a_signal_to_a_dead_workflow_is_permanent_not_retried_forever(
    tenant_id, temporal_client, loop, monkeypatch
):
    from ingest_gateway import dispatcher as dispatcher_module

    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dispatcher_module, "_notify_undeliverable",
        lambda work_item_id, reason: notified.append((work_item_id, reason)),
    )
    # status diz "esperando aprovação", mas NENHUM workflow existe (a classe
    # B8: linha viva, workflow morto — wi_8a7036e7 hoje).
    wi = _create_work_item_and_workflow(
        temporal_client, tenant_id, "awaiting_plan_approval", start=False
    )
    outcome, _ = loop.run_until_complete(
        _dispatch_row(
            temporal_client, work_item_id=wi, event_id="evt_dead_1",
            kind="approval", status="awaiting_plan_approval",
            payload={"content_snapshot": "approved", "actor": {"resolved_principal": "usr_alice"}},
        )
    )
    assert outcome == "signal_permanently_undeliverable", (
        "signal para workflow morto não pode ser retry infinito — é falha "
        "PERMANENTE, consumida com auditoria e aviso no canal"
    )
    assert notified and notified[0][0] == wi


def test_notify_undeliverable_posts_to_the_slack_adapter(tenant_id, monkeypatch):
    """A função de aviso: item slack → POST /internal/status-comment no
    adapter (o writer edita a MESMA mensagem mutável que o humano clicou);
    item de outra fonte → não posta (GitHub/Jira herdam depois)."""
    from ingest_gateway import dispatcher as dispatcher_module

    posts: list[tuple[str, dict]] = []

    class _FakeResp:
        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            posts.append((url, json))
            return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeClient)

    wi = f"wi_notif_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key, status) "
            "VALUES (%s,%s,'slack',%s::jsonb,'usr_alice',%s,'implementing')",
            (wi, tenant_id, json.dumps({"channel": "C_NOTIF", "thread_ts": "1.2"}),
             f"idem_{wi}"),
        )
    conn.commit()
    conn.close()

    dispatcher_module._notify_undeliverable(wi, "o item está em 'implementing'")
    assert posts, "item slack recebe o aviso no canal"
    url, body = posts[0]
    assert url.endswith("/internal/status-comment")
    assert body["work_item_id"] == wi and body["channel"] == "C_NOTIF"
    assert "Não consegui aplicar" in body["body"]
    assert "implementing" in body["body"]

# Os dois testes de rota de VEREDITO DE PARQUE saíram em 2026-08-10 com o
# parque: não há mais status `spec_conflict` para rotear nem botão que emita
# `park_verdict`. O terceiro do bloco — o Approve de plano chegando a um item
# parqueado continuar `unexpected_status` — perdeu o cenário mas não o
# invariante, e ele segue pinado pelos testes de aprovação de plano acima.
