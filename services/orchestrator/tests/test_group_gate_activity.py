"""`check_group_plan_gate`: a semântica SQL da barreira de grupo.

Regras (decisão do operador, 2026-08-14):
  - holding quando OUTRO membro do grupo está `awaiting_plan_approval`
    (qualquer membro gateado segura o conjunto);
  - holding quando o PRIMÁRIO (id == group_id) ainda não passou da fase de
    plano (new/needs_clarification/ready/queued/awaiting_plan_approval) — o
    irmão não corre na frente de um primário lento; `blocked` também segura
    (é recuperável por humano);
  - abort quando um membro morreu na fase de plano (failed — inclui
    plan_rejected_cancel — ou escalated): o grupo colapsou junto;
  - item sem grupo → in_group False, nada segura (o caminho single-repo não
    ganha latência);
  - irmão↔irmão ambos `queued` NÃO se seguram (sem deadlock: a regra do
    pré-implementing vale só para o primário).

Postgres REAL (DSN do conftest), como os vizinhos.
"""
from __future__ import annotations

import asyncio
import uuid

import psycopg2

try:
    from dse_orchestrator.local_activities import check_group_plan_gate
except ImportError:  # vermelho: a activity ainda não existe — os testes FALHAM
    check_group_plan_gate = None  # type: ignore[assignment]

from conftest import DSN, insert_work_item, new_work_item_id


def _set(work_item_id: str, *, status: str | None = None,
         group_id: str | None = None) -> None:
    conn = psycopg2.connect(DSN)
    try:
        with conn:
            with conn.cursor() as cur:
                if status is not None:
                    cur.execute("UPDATE work_items SET status = %s WHERE id = %s",
                                (status, work_item_id))
                if group_id is not None:
                    cur.execute("UPDATE work_items SET group_id = %s WHERE id = %s",
                                (group_id, work_item_id))
    finally:
        conn.close()


def _gate(work_item_id: str) -> dict:
    assert check_group_plan_gate is not None, "check_group_plan_gate não existe ainda"
    return asyncio.run(check_group_plan_gate({"work_item_id": work_item_id}))


def _group(tenant: str, *, primary_status: str, sibling_status: str = "queued"):
    """Primário + 2 irmãos no mesmo grupo; devolve (primary, sib_a, sib_b)."""
    primary = new_work_item_id("grp-pri")
    sib_a = new_work_item_id("grp-sa")
    sib_b = new_work_item_id("grp-sb")
    for wid in (primary, sib_a, sib_b):
        insert_work_item(wid, tenant_id=tenant)
    _set(primary, status=primary_status, group_id=primary)
    _set(sib_a, status=sibling_status, group_id=primary)
    _set(sib_b, status=sibling_status, group_id=primary)
    return primary, sib_a, sib_b


def test_a_gated_member_holds_the_whole_group():
    tenant = f"t-{uuid.uuid4().hex[:10]}"
    _, sib_a, _ = _group(tenant, primary_status="awaiting_plan_approval")
    out = _gate(sib_a)
    assert out["in_group"] is True
    assert out["holding"] is True, out
    assert out["abort"] is False


def test_a_slow_primary_holds_the_siblings_even_without_a_gate():
    tenant = f"t-{uuid.uuid4().hex[:10]}"
    _, sib_a, _ = _group(tenant, primary_status="needs_clarification")
    out = _gate(sib_a)
    assert out["holding"] is True, (
        f"primário pré-plano não segura o irmão — a corrida do wi_e15f4991 volta: {out}"
    )


def test_two_queued_siblings_do_not_deadlock_each_other():
    tenant = f"t-{uuid.uuid4().hex[:10]}"
    _, sib_a, _ = _group(tenant, primary_status="implementing",
                         sibling_status="queued")
    out = _gate(sib_a)
    assert out["holding"] is False, (
        f"irmão queued segurou irmão queued — deadlock: {out}"
    )
    assert out["abort"] is False


def test_a_dead_member_aborts_the_group():
    tenant = f"t-{uuid.uuid4().hex[:10]}"
    _, sib_a, _ = _group(tenant, primary_status="failed")
    out = _gate(sib_a)
    assert out["abort"] is True, out
    assert "failed" in out.get("reason", ""), out


def test_the_primary_holds_on_a_gated_sibling_but_not_on_queued_ones():
    tenant = f"t-{uuid.uuid4().hex[:10]}"
    primary, sib_a, _ = _group(tenant, primary_status="queued",
                               sibling_status="queued")
    assert _gate(primary)["holding"] is False, "irmãos queued não seguram o primário"
    _set(sib_a, status="awaiting_plan_approval")
    out = _gate(primary)
    assert out["holding"] is True, f"irmão gateado tem que segurar o primário: {out}"


def test_an_ungrouped_item_is_not_held():
    tenant = f"t-{uuid.uuid4().hex[:10]}"
    solo = new_work_item_id("grp-solo")
    insert_work_item(solo, tenant_id=tenant)
    _set(solo, status="queued")
    out = _gate(solo)
    assert out["in_group"] is False
    assert out["holding"] is False
    assert out["abort"] is False
