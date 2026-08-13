"""`estimate_plan_cost`: o gate diz quanto deve custar — do custo REAL.

rc.89, metade B do "o plano fala a verdade": o aprovador decidia com uma
palavra de risco e nada mais. A estimativa vem da MEDIANA (+ faixa p25-p75) do
custo total de itens CONCLUÍDOS da mesma `task_class` — em `cost_usd`, nunca
tokens (os tokens_in do ledger não leem cache e são régua quebrada;
BACKLOG-REVIEW §ledger). Fonte: `console_rm.runs_view ⋈ work_items` — a única
superfície que une ledger e audit e portanto NÃO subconta (F0, migração 0022).

Fronteiras fixadas aqui:
  - mediana, não média: um item outlier de $100 não arrasta a previsão;
  - <3 itens na classe → fallback global; <3 no global → `available: False`
    honesto — nunca um número inventado de 1 amostra;
  - só `status='done'` conta: item escalado/em voo não é preço de referência;
  - escopo por TENANT: o histórico de um cliente não precifica o plano de
    outro — e é o tenant único por teste que isola os testes entre si (o
    schema do with_test_database é descartável; não há cleanup, nem grants
    de DELETE em work_items para fazê-lo).

Postgres REAL (DSN do conftest), como os vizinhos.
"""
from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest

try:
    from dse_orchestrator.local_activities import estimate_plan_cost
except ImportError:  # vermelho: a activity ainda não existe — os testes FALHAM
    estimate_plan_cost = None  # type: ignore[assignment]

from conftest import DSN, insert_work_item, new_work_item_id


def _seed_done_item(work_item_id: str, tenant_id: str, task_class: str,
                    total_usd: float) -> None:
    """Um item concluído com custo total conhecido: work_items.status='done' +
    uma linha em console_rm.runs_view somando `total_usd`."""
    insert_work_item(work_item_id, tenant_id=tenant_id)
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE work_items SET status='done', task_class=%s WHERE id=%s",
                (task_class, work_item_id),
            )
            cur.execute(
                """
                INSERT INTO console_rm.runs_view
                    (run_key, work_item_id, tenant_id, engine, model, status,
                     tokens_in, tokens_out, cost_usd, started_at, ended_at)
                VALUES (%s, %s, %s, 'coder', 'anthropic/claude', 'done',
                        0, 0, %s, now(), now())
                ON CONFLICT (run_key) DO UPDATE SET cost_usd = EXCLUDED.cost_usd
                """,
                (f"test:{work_item_id}", work_item_id, tenant_id, total_usd),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_target(work_item_id: str, tenant_id: str, task_class: str | None) -> None:
    insert_work_item(work_item_id, tenant_id=tenant_id)
    if task_class:
        conn = psycopg2.connect(DSN)
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE work_items SET task_class=%s WHERE id=%s",
                        (task_class, work_item_id))
        conn.close()


@pytest.fixture
def classe() -> str:
    # task_class tem vocabulário fechado (migração 0024) — usa um real; o
    # isolamento entre testes vem do TENANT único (o estimador é escopado).
    return "feature_small"


@pytest.fixture
def tenant() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


def _estimar(work_item_id: str) -> dict:
    assert estimate_plan_cost is not None, (
        "local activity estimate_plan_cost não existe em local_activities — o gate "
        "continua sem previsão de custo"
    )
    return asyncio.run(estimate_plan_cost({"work_item_id": work_item_id}))


def test_median_and_band_for_same_task_class(classe, tenant):
    for c in (1.0, 2.0, 3.0, 4.0, 100.0):  # o outlier de $100 não arrasta a mediana
        _seed_done_item(new_work_item_id("cost"), tenant, classe, c)
    alvo = new_work_item_id("cost-alvo")
    _seed_target(alvo, tenant, classe)

    est = _estimar(alvo)

    assert est["available"] is True
    assert est["scope"] == "task_class"
    assert est["n"] == 5
    assert est["p50_usd"] == pytest.approx(3.0, abs=0.5), (
        f"a mediana de [1,2,3,4,100] é 3 — veio {est['p50_usd']} (média seria ~22, "
        "e é exatamente por isso que não se usa média)"
    )
    assert est["p25_usd"] <= est["p50_usd"] <= est["p75_usd"]


def test_falls_back_to_global_below_three_in_class(classe, tenant):
    for _ in range(2):  # só 2 da classe do alvo
        _seed_done_item(new_work_item_id("cost-cls"), tenant, classe, 2.0)
    for _ in range(4):  # 4 de outra classe, MESMO tenant
        _seed_done_item(new_work_item_id("cost-out"), tenant, "bug_fix", 8.0)
    alvo = new_work_item_id("cost-alvo")
    _seed_target(alvo, tenant, classe)

    est = _estimar(alvo)

    assert est["available"] is True
    assert est["scope"] == "global", (
        f"com 2 itens na classe, o degrau é o global (do tenant) — veio {est['scope']!r}"
    )


def test_only_done_items_count(classe, tenant):
    """Itens não-concluídos (escalated, implementing) não são preço de
    referência — um item que morreu no meio custa menos do que custaria inteiro."""
    for status in ("escalated", "implementing"):
        wid = new_work_item_id("cost-nd")
        _seed_done_item(wid, tenant, classe, 50.0)
        conn = psycopg2.connect(DSN)
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE work_items SET status=%s WHERE id=%s", (status, wid))
        conn.close()
    alvo = new_work_item_id("cost-alvo")
    _seed_target(alvo, tenant, None)

    est = _estimar(alvo)

    assert est["available"] is False, (
        "dois itens NÃO concluídos entraram na amostra — só done conta"
    )


def test_another_tenants_history_is_not_my_price(classe, tenant):
    """O escopo por tenant é fronteira, não conveniência: 5 itens concluídos de
    OUTRO tenant não podem precificar o plano deste."""
    outro = f"t-{uuid.uuid4().hex[:10]}"
    for c in (1.0, 2.0, 3.0, 4.0, 5.0):
        _seed_done_item(new_work_item_id("cost-outro"), outro, classe, c)
    alvo = new_work_item_id("cost-alvo")
    _seed_target(alvo, tenant, classe)

    est = _estimar(alvo)

    assert est["available"] is False, (
        "o histórico de outro tenant virou preço deste — a fronteira vazou"
    )


def test_returns_unavailable_below_three_anywhere(tenant):
    alvo = new_work_item_id("cost-vazio")
    _seed_target(alvo, tenant, None)

    est = _estimar(alvo)

    assert est["available"] is False
    assert "p50_usd" not in est or est.get("p50_usd") is None, (
        "indisponível não carrega número — número inventado é o defeito do 400"
    )


def test_unknown_work_item_is_unavailable():
    est = _estimar(f"wi-nao-existe-{uuid.uuid4().hex[:8]}")
    assert est["available"] is False
