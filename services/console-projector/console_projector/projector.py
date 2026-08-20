"""Projector fase1 -> console_rm (Plan 06 §5).

One pass (`run_once`) processes each source from its cursor and writes both
output + cursor IN THE SAME TRANSACTION (effectively exactly-once; upserts are
idempotent — replaying a batch is harmless; a FULL replay with cursor 0 rebuilds
the read model from scratch, which doubles as the DR plan).

F0 sources:
  - work_items        (cursor on updated_at — set_updated_at trigger)
  - audit_log         (cursor on id BIGSERIAL) -> timeline_events + last_event
  - model_call_ledger (cursor on id BIGSERIAL) -> runs_view
"""
from __future__ import annotations

import json
import logging
import os
import time

import psycopg2
import psycopg2.extras

from .mappers import map_audit_event, map_status, split_title

logger = logging.getLogger("console_projector")

_BATCH = int(os.environ.get("DSE_PROJECTOR_BATCH", "2000"))


def get_connection():
    dsn = os.environ.get(
        "DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
    )
    return psycopg2.connect(dsn)


# ---------------------------------------------------------------------------
# cursors
# ---------------------------------------------------------------------------

def _cursor(cur, source: str):
    cur.execute(
        "INSERT INTO console_rm.projection_cursor (source) VALUES (%s) "
        "ON CONFLICT (source) DO NOTHING",
        (source,),
    )
    cur.execute(
        "SELECT last_id, last_seen, last_key FROM console_rm.projection_cursor WHERE source = %s",
        (source,),
    )
    row = cur.fetchone()
    # cur is a RealDictCursor (run_once) — access by name, not by position.
    return int(row["last_id"]), row["last_seen"], row["last_key"]


def _advance(cur, source: str, *, last_id: int | None = None, last_seen=None,
             last_key: str | None = None) -> None:
    cur.execute(
        "UPDATE console_rm.projection_cursor SET last_id = COALESCE(%s, last_id), "
        "last_seen = COALESCE(%s, last_seen), last_key = COALESCE(%s, last_key), "
        "updated_at = now() WHERE source = %s",
        (last_id, last_seen, last_key, source),
    )


def _effective_last_id(cur, source: str, last_id: int) -> int:
    """An id cursor is only meaningful against the incarnation of the source it
    was advanced on. audit_log and model_call_ledger are append-only BIGSERIALs,
    so max(id) BELOW the cursor means the table was reset under a surviving read
    model (a disposable test schema restarting serials at 1; a restored
    database) — trusting the cursor then leaves the projector permanently blind:
    `id > cursor` matches nothing and every pass converges on 0. The only
    correct cursor for a reset source is 0 — full replay, the documented DR
    path, idempotent by design. `source` doubles as the table name for both id
    sources (literal call sites, never user input)."""
    if last_id <= 0:
        return last_id
    cur.execute(f"SELECT COALESCE(max(id), 0) AS max_id FROM {source}")
    max_id = int(cur.fetchone()["max_id"])
    if max_id >= last_id:
        return last_id
    logger.warning(
        "%s regressed below its cursor (max %s < cursor %s) — source was reset; "
        "rebuilding the read model from 0 (DR replay)",
        source, max_id, last_id,
    )
    return 0


# ---------------------------------------------------------------------------
# work_items -> work_items_view
# ---------------------------------------------------------------------------

def _project_work_items(cur) -> int:
    # Keyset (updated_at, id): STRICT progress — timestamp ties are broken by
    # the PK; drains to 0 (required by drain()/tests).
    _, last_seen, last_key = _cursor(cur, "work_items")
    if last_seen is None:
        cur.execute(
            "SELECT wi.*, COALESCE(wi.pr_url, t.pr_url) AS pr_url_eff, "
            "COALESCE(wi.pr_number, t.pr_number) AS pr_number_eff "
            "FROM work_items wi LEFT JOIN wse_pr_tracking t ON t.work_item_id = wi.id "
            "ORDER BY wi.updated_at, wi.id LIMIT %s", (_BATCH,))
    else:
        cur.execute(
            "SELECT wi.*, COALESCE(wi.pr_url, t.pr_url) AS pr_url_eff, "
            "COALESCE(wi.pr_number, t.pr_number) AS pr_number_eff "
            "FROM work_items wi LEFT JOIN wse_pr_tracking t ON t.work_item_id = wi.id "
            "WHERE (wi.updated_at, wi.id) > (%s, %s) "
            "ORDER BY wi.updated_at, wi.id LIMIT %s",
            (last_seen, last_key or "", _BATCH),
        )
    rows = cur.fetchall()
    if not rows:
        return 0
    for row in rows:
        _upsert_work_item(cur, row)
    tail = rows[-1]
    _advance(cur, "work_items", last_seen=tail["updated_at"], last_key=tail["id"])
    return len(rows)


def _upsert_work_item(cur, wi) -> None:
    try:
        status, phase = map_status(wi["status"])
    except KeyError:
        # new status with no map entry: fail-visible (log it; never make one up,
        # never crash).
        logger.error("fase1 status has no console mapping: %r (wi=%s)", wi["status"], wi["id"])
        return

    source_ref = wi["source_ref"] or {}
    number = source_ref.get("number") or source_ref.get("issue_number")
    source_id = f"{wi['repo']}#{number}" if wi["repo"] and number else (wi["repo"] or wi["id"][:18])

    # title/description: admission-time snapshot (TOCTOU) from the outbox — sanitized.
    cur.execute(
        "SELECT payload FROM ingest_events WHERE work_item_id = %s AND kind = 'task_request' "
        "ORDER BY id LIMIT 1",
        (wi["id"],),
    )
    ev = cur.fetchone()
    payload = (ev["payload"] if ev else None) or {}
    content = payload.get("sanitized_content") or payload.get("content_snapshot") or ""
    title, description = split_title(content, fallback=source_id)

    budget = wi["budget"] or {}
    budget_usd = budget.get("max_usd") if isinstance(budget, dict) else None

    cur.execute(
        """
        INSERT INTO console_rm.work_items_view (
            work_item_id, tenant_id, source, source_id, repo, base_branch, title,
            description, requester, data_class, status, current_phase,
            pr_number, pr_url, budget_usd, risk_class, task_class, created_at, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (work_item_id) DO UPDATE SET
            status = EXCLUDED.status, current_phase = EXCLUDED.current_phase,
            title = EXCLUDED.title, description = EXCLUDED.description,
            pr_number = EXCLUDED.pr_number, pr_url = EXCLUDED.pr_url,
            risk_class = EXCLUDED.risk_class, budget_usd = EXCLUDED.budget_usd,
            task_class = EXCLUDED.task_class,
            -- repo/base_branch/source_id são RESOLVIDOS DEPOIS DA ADMISSÃO: um
            -- item primário do Slack nasce sem repo e o roteamento o grava
            -- segundos depois. Fora desta lista, a primeira projeção (repo
            -- NULL) vencia todas as seguintes e o painel mostrava "—" para
            -- sempre — o operador leu isso como "o DSE não identificou o
            -- repositório" e gastou três verificações provando o contrário.
            -- source_id deriva do repo, então acompanha ou mostra o id cru.
            repo = EXCLUDED.repo, base_branch = EXCLUDED.base_branch,
            source_id = EXCLUDED.source_id,
            updated_at = EXCLUDED.updated_at
        """,
        (
            wi["id"], wi["tenant_id"], wi["source"], source_id, wi["repo"],
            wi["base_branch"], title, description, wi["requester"], wi["data_class"],
            status, phase, wi.get("pr_number_eff") or wi["pr_number"],
            wi.get("pr_url_eff"), budget_usd,
            wi["risk_class"], wi.get("task_class"), wi["created_at"], wi["updated_at"],
        ),
    )


# ---------------------------------------------------------------------------
# work_item_evidence -> work_items_view (preview_status/url) — plan 08 §D (D5)
# ---------------------------------------------------------------------------

def _project_evidence(cur) -> int:
    """Reflects the evidence pipeline state (preview/demo) into the view. Keyset
    (updated_at, work_item_id) — same pattern as _project_work_items; drains to
    0. Only UPDATEs the view row (the work item always exists before its
    evidence); if the row hasn't been projected yet the UPDATE is a no-op and the
    cursor still advances (the evidence lands once the work item is projected —
    idempotent)."""
    _, last_seen, last_key = _cursor(cur, "work_item_evidence")
    if last_seen is None:
        cur.execute(
            "SELECT work_item_id, preview_status, preview_url, demo_passed, "
            "video_artifact_key, trace_artifact_key, updated_at "
            "FROM work_item_evidence ORDER BY updated_at, work_item_id LIMIT %s", (_BATCH,))
    else:
        cur.execute(
            "SELECT work_item_id, preview_status, preview_url, demo_passed, "
            "video_artifact_key, trace_artifact_key, updated_at "
            "FROM work_item_evidence WHERE (updated_at, work_item_id) > (%s, %s) "
            "ORDER BY updated_at, work_item_id LIMIT %s",
            (last_seen, last_key or "", _BATCH),
        )
    rows = cur.fetchall()
    if not rows:
        return 0
    for row in rows:
        cur.execute(
            "UPDATE console_rm.work_items_view SET "
            "preview_status = %s, preview_url = %s, demo_passed = %s, "
            "video_artifact_key = %s, trace_artifact_key = %s "
            "WHERE work_item_id = %s",
            (row["preview_status"], row["preview_url"], row["demo_passed"],
             row["video_artifact_key"], row["trace_artifact_key"], row["work_item_id"]),
        )
    tail = rows[-1]
    _advance(cur, "work_item_evidence", last_seen=tail["updated_at"], last_key=tail["work_item_id"])
    return len(rows)


# ---------------------------------------------------------------------------
# audit_log -> timeline_events (+ last_event na view)
# ---------------------------------------------------------------------------

def _project_audit(cur) -> int:
    last_id, _, _ = _cursor(cur, "audit_log")
    last_id = _effective_last_id(cur, "audit_log", last_id)
    cur.execute(
        "SELECT id, ts, work_item_id, tenant_id, actor, action, details FROM audit_log "
        "WHERE id > %s AND work_item_id IS NOT NULL ORDER BY id LIMIT %s",
        (last_id, _BATCH),
    )
    rows = cur.fetchall()
    if not rows:
        return 0
    last_event_by_wi: dict[str, tuple[str, object]] = {}
    for row in rows:
        ev_type, message = map_audit_event(row["action"], row["details"])
        _maybe_run_from_audit(cur, row)
        cur.execute(
            """
            INSERT INTO console_rm.timeline_events
                (audit_id, work_item_id, tenant_id, type, channel, message, data, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (work_item_id, audit_id) DO NOTHING
            """,
            (
                row["id"], row["work_item_id"], row["tenant_id"], ev_type, None,
                message, json.dumps(row["details"] or {}), row["ts"],
            ),
        )
        last_event_by_wi[row["work_item_id"]] = (message, row["ts"])
    for wi_id, (message, ts) in last_event_by_wi.items():
        cur.execute(
            "UPDATE console_rm.work_items_view SET last_event = %s, updated_at = GREATEST(updated_at, %s) "
            "WHERE work_item_id = %s",
            (message, ts, wi_id),
        )
    _advance(cur, "audit_log", last_id=rows[-1]["id"])
    return len(rows)


_AUDIT_COST_ACTIONS = {
    "planner_turn_completed": "planner",
    "coder_turn_completed": "coder",
    "tester_turn_completed": "tester",
    "l2_review_completed": "l2",
}


def _maybe_run_from_audit(cur, row) -> None:
    """Real cost of in-process turns (F0): the substrate talks to the gateway
    directly (it does not go through the Python client that writes the ledger),
    so the durable cost lives in details->>'cost_usd' of the *_turn_completed
    events. run_key 'audit:<id>' cannot collide with 'ledger:<id>'.

    Dedup (found via the dashboard showing ~2x the real spend): the SAME turn is
    audited TWICE — once by the activity (system:sandbox-runtime) and once by the
    workflow (system:orchestrator). Only the ORCHESTRATOR row becomes a run; the
    activity row stays on the timeline but does not add cost again.

    THIS IS NOW THE LEGACY PATH. Coder turns are metered into
    model_call_ledger at the source, and a metered turn must NOT also produce a
    run here — otherwise the one surface that was CORRECT (runs_view, $27.91)
    would double to $55.82, which is worse than the bug being fixed. Two guards
    below cover the two ways a turn can already be accounted for."""
    engine = _AUDIT_COST_ACTIONS.get(row["action"])
    if not engine:
        return
    if row.get("actor") != "system:orchestrator":
        return
    details = row["details"] or {}
    # (1) Metered at turn time: _project_ledger owns this run.
    if details.get("ledger_id") is not None:
        return
    # (2) Metered retroactively by a backfill. audit_log is append-only, so a
    #     historical row can never gain a ledger_id — the ledger row points back
    #     at it instead. Without this, a full replay from cursor 0 (the
    #     documented DR path) recreates every audit:<id> run on top of the
    #     backfilled ledger:<id> rows.
    cur.execute(
        "SELECT 1 FROM model_call_ledger WHERE source_audit_id = %s LIMIT 1", (row["id"],)
    )
    if cur.fetchone() is not None:
        return
    cost = details.get("cost_usd")
    if cost in (None, ""):
        return
    cur.execute(
        """
        INSERT INTO console_rm.runs_view
            (run_key, work_item_id, tenant_id, engine, model, status,
             tokens_in, tokens_out, cost_usd, started_at, ended_at)
        VALUES (%s,%s,%s,%s,%s,'completed',%s,%s,%s,%s,%s)
        ON CONFLICT (run_key) DO NOTHING
        """,
        (
            f"audit:{row['id']}", row["work_item_id"], row["tenant_id"], engine,
            details.get("model"), int(details.get("tokens_in") or 0),
            int(details.get("tokens_out") or 0), float(cost), row["ts"], row["ts"],
        ),
    )


# ---------------------------------------------------------------------------
# model_call_ledger -> runs_view
# ---------------------------------------------------------------------------

def _project_ledger(cur) -> int:
    last_id, _, _ = _cursor(cur, "model_call_ledger")
    last_id = _effective_last_id(cur, "model_call_ledger", last_id)
    cur.execute(
        "SELECT id, tenant_id, work_item_id, stage, model, cost_usd, tokens_in, tokens_out, created_at "
        "FROM model_call_ledger WHERE id > %s ORDER BY id LIMIT %s",
        (last_id, _BATCH),
    )
    rows = cur.fetchall()
    if not rows:
        return 0
    for row in rows:
        cur.execute(
            """
            INSERT INTO console_rm.runs_view
                (run_key, work_item_id, tenant_id, engine, model, status,
                 tokens_in, tokens_out, cost_usd, started_at, ended_at)
            VALUES (%s,%s,%s,%s,%s,'completed',%s,%s,%s,%s,%s)
            ON CONFLICT (run_key) DO NOTHING
            """,
            (
                f"ledger:{row['id']}", row["work_item_id"], row["tenant_id"], row["stage"],
                row["model"], row["tokens_in"], row["tokens_out"], row["cost_usd"],
                row["created_at"], row["created_at"],
            ),
        )
    _advance(cur, "model_call_ledger", last_id=rows[-1]["id"])
    # Os DIAS que este lote tocou: é o recorte do rollup abaixo. Sem eles a
    # agregação varria o ledger inteiro a cada passada.
    _project_ledger.dias_tocados = sorted({
        r["created_at"].date() for r in rows if r.get("created_at")
    })
    return len(rows)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _refresh_cost_rollup(cur, dias=None) -> int:
    """Plan 08 §E — recomputes the cost rollup from the SoR (model_call_ledger,
    the P8 cost truth), aggregating by day x repo x model x task_class. Full
    recompute (DELETE+INSERT) -> idempotent by construction (the reconciliation
    test guarantees rollup == ledger). Called only when the ledger advanced in
    this pass (cheap enough at dev scale; an incremental rollup is the upgrade
    once volume demands it). Returns the number of rollup cells."""
    # `dias` = None recomputa TUDO (o caminho do drain/DR, que precisa
    # reconstruir o rollup inteiro). Com uma lista, o recorte é por dia: o
    # DELETE+INSERT sobre o ledger INTEIRO a cada passada com atividade era
    # O(ledger) por lote — num drain de 2000 em 2000, M/2000 agregações
    # completas sobre M linhas. Idempotente do mesmo jeito, porque a chave do
    # rollup contém o dia: recomputar um dia é recomputar tudo o que aquele dia
    # tem.
    filtro = ""
    args: tuple = ()
    if dias:
        filtro = "WHERE (l.created_at AT TIME ZONE 'UTC')::date = ANY(%s)"
        args = (list(dias),)
        cur.execute(
            "DELETE FROM console_rm.cost_rollup WHERE day = ANY(%s)", (list(dias),)
        )
    else:
        cur.execute("DELETE FROM console_rm.cost_rollup")
    cur.execute(
        f"""
        INSERT INTO console_rm.cost_rollup
            (tenant_id, day, repo, model, task_class, run_count, cost_usd, tokens_in, tokens_out)
        SELECT l.tenant_id,
               (l.created_at AT TIME ZONE 'UTC')::date AS day,
               COALESCE(NULLIF(wi.repo, ''), '(unknown)') AS repo,
               COALESCE(NULLIF(l.model, ''), '(unknown)') AS model,
               COALESCE(NULLIF(l.task_class, ''), 'chore') AS task_class,
               count(*), COALESCE(sum(l.cost_usd), 0),
               COALESCE(sum(l.tokens_in), 0), COALESCE(sum(l.tokens_out), 0)
        FROM model_call_ledger l
        LEFT JOIN work_items wi ON wi.id = l.work_item_id
        {filtro}
        GROUP BY 1, 2, 3, 4, 5
        """,
        args,
    )
    return cur.rowcount


def run_once(conn) -> dict[str, int]:
    """One pass over every source. Single transaction: output + cursors commit
    together; any error rolls the whole batch back (retryable)."""
    with conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            counts = {
                "work_items": _project_work_items(cur),
                # after work_items: the evidence UPDATEs the view row (§D D5)
                "work_item_evidence": _project_evidence(cur),
                "audit_log": _project_audit(cur),
                "model_call_ledger": _project_ledger(cur),
            }
            # §E — cost rollup: recompute only when the ledger advanced (avoids
            # work on every idle tick). Does NOT count toward drain convergence
            # (it is derived, not a cursor-backed source).
            if counts["model_call_ledger"] > 0:
                _refresh_cost_rollup(cur, getattr(_project_ledger, "dias_tocados", None))
    return counts


def drain(conn, *, max_iterations: int = 1000) -> None:
    """Runs run_once until every source returns 0 (backfill/tests). Progress is
    guaranteed by the strict cursors; the cap is just a backstop.

    §E: at the end, forces ONE cost-rollup refresh — backfill/DR must rebuild the
    rollup even when the ledger cursor is already at the end (no new row would
    trigger run_once's incremental refresh). A full recompute is idempotent."""
    for _ in range(max_iterations):
        if not any(run_once(conn).values()):
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    _refresh_cost_rollup(cur)
            return
    raise RuntimeError("drain did not converge — cursor made no progress?")


def main() -> None:  # pragma: no cover - production loop
    logging.basicConfig(level=logging.INFO)
    interval = float(os.environ.get("DSE_PROJECTOR_INTERVAL_SECONDS", "2"))
    conn = get_connection()
    logger.info("console-projector is up (interval %.1fs)", interval)
    while True:
        try:
            counts = run_once(conn)
            if any(counts.values()):
                logger.info("projected: %s", counts)
        except psycopg2.Error:
            logger.exception("database error; reconnecting")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(interval)
            conn = get_connection()
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    main()
