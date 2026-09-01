"""WSF-E6-T1/T2 — queue board API (§9.3 projection, budgets, audit trail) +
operator controls (signals via FakeSignalSender + audit carrying the operator
identity). Real Postgres."""
from __future__ import annotations

import uuid

import psycopg2.extras
import pytest
from dse_audit import emit
from dse_audit.client import get_connection
from dse_platform import upsert_tenant_config
from dse_platform.queue_board import (
    FakeSignalSender,
    get_audit_trail,
    get_tenant_budget,
    list_work_items,
    work_items_by_state,
)
from dse_platform.queue_board.operator import OperatorConsole


def _insert_work_item(tenant, status="implementing", cost=None):
    wid = f"wi-{uuid.uuid4().hex[:10]}"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_items (id, tenant_id, source, source_ref, requester, status, idempotency_key, repo)
                VALUES (%s,%s,'github',%s,%s,%s,%s,'org/repo')
                """,
                (wid, tenant, psycopg2.extras.Json({"repo": "org/repo", "issue_number": 1}),
                 "usr_req", status, wid),
            )
        conn.commit()
    finally:
        conn.close()
    if cost is not None:
        emit(actor="system:orchestrator", action="coder_turn_completed",
             tenant_id=tenant, work_item_id=wid, details={"cost_usd": cost})
    return wid


@pytest.fixture()
def tenant():
    return f"acme-qb-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def op():
    return OperatorConsole(FakeSignalSender(), operator="usr_operator42")


# ---- API ----
def test_list_and_group_by_state(tenant):
    w1 = _insert_work_item(tenant, "implementing")
    w2 = _insert_work_item(tenant, "pr_ready")
    items = list_work_items(tenant_id=tenant)
    assert {w1, w2} <= {i.work_item_id for i in items}

    grouped = work_items_by_state(tenant_id=tenant)
    # every §9.3 state present as a key (the board shows the whole machine)
    assert "new" in grouped and "escalated" in grouped
    assert w1 in [i.work_item_id for i in grouped["implementing"]]
    assert w2 in [i.work_item_id for i in grouped["pr_ready"]]


def test_public_status_projection(tenant):
    w = _insert_work_item(tenant, "needs_clarification")
    item = next(i for i in list_work_items(tenant_id=tenant) if i.work_item_id == w)
    assert item.status == "needs_clarification"
    assert item.public_status == "blocked"


def test_budget_and_cost_aggregation(tenant):
    upsert_tenant_config(tenant, monthly_budget_usd=100)
    _insert_work_item(tenant, "implementing", cost=3.5)
    _insert_work_item(tenant, "validating", cost=1.25)
    b = get_tenant_budget(tenant)
    assert b.monthly_budget_usd == 100
    assert float(b.spent_usd) == pytest.approx(4.75)
    assert float(b.remaining_usd) == pytest.approx(95.25)
    assert b.active_work_items == 2


def test_audit_trail_for_work_item(tenant):
    w = _insert_work_item(tenant)
    emit(actor="system:orchestrator", action="intake_started", tenant_id=tenant, work_item_id=w, details={})
    trail = get_audit_trail(w)
    assert any(e["action"] == "intake_started" for e in trail)


# ---- Operator controls ----
def _op_audit(work_item_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT actor, action, details FROM audit_log WHERE work_item_id = %s ORDER BY ts, id",
                (work_item_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def test_operator_requires_resolved_principal():
    with pytest.raises(ValueError):
        OperatorConsole(FakeSignalSender(), operator="U12345")  # raw platform_user_id


def test_pause_sends_signal_and_audits_operator(tenant, op):
    w = _insert_work_item(tenant)
    op.pause(w, tenant, reason="inspecting")
    assert op._sender.sent == [(w, "pause", "inspecting")]
    rows = _op_audit(w)
    assert any(r["action"] == "operator_pause" and r["actor"] == "usr_operator42" for r in rows)
    assert any(r["details"].get("operator") == "usr_operator42" for r in rows)


def test_all_task_controls_send_correct_signals(tenant, op):
    w = _insert_work_item(tenant)
    op.resume(w, tenant)
    op.cancel(w, tenant, reason="bad")
    op.retry_from_checkpoint(w, tenant)
    op.force_clarification(w, tenant, reason="unclear")
    op.escalate(w, tenant, reason="stuck")
    op.reassign_model(w, tenant, model="claude-x")
    op.reassign_runtime(w, tenant, runtime="rt-2")
    names = [(n, a) for (_, n, a) in op._sender.sent]
    assert ("resume", None) in names
    assert ("cancel", "bad") in names
    assert ("retry_from_checkpoint", None) in names
    assert ("force_clarification", "unclear") in names
    assert ("escalate", "stuck") in names
    assert ("reassign_model", "claude-x") in names
    assert ("reassign_runtime", "rt-2") in names


def test_quarantine_marks_durable_and_pauses(tenant, op):
    from dse_platform import is_quarantined

    w = _insert_work_item(tenant)
    op.quarantine(w, tenant, reason="leak risk")
    assert is_quarantined(w) is True
    # signaled pause
    assert any(n == "pause" for (_, n, _) in op._sender.sent)
    # the board projection shows the quarantine
    item = next(i for i in list_work_items(tenant_id=tenant) if i.work_item_id == w)
    assert item.quarantined is True

    op.release_quarantine(w, tenant)
    assert is_quarantined(w) is False


def test_kill_switch_task_is_cancel(tenant, op):
    w = _insert_work_item(tenant)
    op.kill_switch_task(w, tenant, reason="kill it")
    assert any(n == "cancel" for (_, n, _) in op._sender.sent)


def test_operator_action_audited_even_if_signal_fails(tenant):
    w = _insert_work_item(tenant)
    failing = FakeSignalSender(raises_for={w})
    op = OperatorConsole(failing, operator="usr_operator99")
    with pytest.raises(RuntimeError):
        op.pause(w, tenant, reason="x")
    # the operator's INTENT was audited even though the signal failed (evidence
    # is never lost — the audit comes before the effect)
    rows = _op_audit(w)
    assert any(r["action"] == "operator_pause" for r in rows)


def test_cancel_of_a_dead_workflow_marks_the_row_cancelled_with_an_audit_row(tenant):
    """rc.130: o botão de cancel do painel só sinalizava — com o workflow morto
    (terminado, purgado, encalhado) o sinal falhava e o operador ia ao SQL
    escrever `cancelled` por conta própria: 33 linhas assim em produção, num
    valor que nem existia no enum, e o sweep de encalhados re-escalando cada
    uma 6 h depois. Falha PERMANENTE do sinal (workflow not found / already
    completed) vira o próprio cancelamento, com audit, pelo guard de status
    terminal — a forma de `stranded.escalate_stranded`."""
    w = _insert_work_item(tenant, status="review_ready")
    failing = FakeSignalSender(raises_for={w})
    op = OperatorConsole(failing, operator="usr_operator99")

    op.cancel(w, tenant, reason="cleanup after the cluster reset")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM work_items WHERE id = %s", (w,))
            assert cur.fetchone()[0] == "cancelled"
    finally:
        conn.close()
    rows = _op_audit(w)
    assert any(r["action"] == "operator_cancel" for r in rows), "a intenção, antes do efeito"
    assert any(r["action"] == "cancelled_by_operator" for r in rows), "o efeito, quando o sinal não tem quem ouvir"


def test_active_count_excludes_cancelled(tenant):
    _insert_work_item(tenant, status="cancelled")
    _insert_work_item(tenant, status="implementing")
    assert get_tenant_budget(tenant).active_work_items == 1
