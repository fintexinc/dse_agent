"""Projector (Plan 06 F0) — map contract + real projection against Postgres.

Runs against the infra Postgres (like the other suites): seeds a synthetic
WorkItem + outbox + audit + ledger, runs `run_once` and validates the read model.
Idempotency: a second pass duplicates nothing (replay is harmless).
"""
from __future__ import annotations

import json
import uuid

import psycopg2
import psycopg2.extras
import pytest

from dse_contracts.work_item import WorkItemStatus

from console_projector.mappers import (
    AUDIT_EVENT_MAP,
    STATUS_MAP,
    map_audit_event,
    map_status,
    split_title,
)
from console_projector.projector import drain

from conftest import DSN


_SUPER_DSN = DSN.replace("dse_app:dse_app_dev_only", "dse:dse_dev_only")


def _cleanup(wi_id: str) -> None:
    """Teardown using the migration role (dse_app cannot delete from the ledger —
    correct: the read model inherits the SoR's discipline). Best-effort."""
    try:
        conn = psycopg2.connect(_SUPER_DSN)
    except Exception:
        return
    try:
        with conn.cursor() as cur:
            for stmt in (
                "DELETE FROM console_rm.timeline_events WHERE work_item_id = %s",
                "DELETE FROM console_rm.runs_view WHERE work_item_id = %s",
                "DELETE FROM console_rm.work_items_view WHERE work_item_id = %s",
                "DELETE FROM work_item_evidence WHERE work_item_id = %s",
                "DELETE FROM model_call_ledger WHERE work_item_id = %s",
                "DELETE FROM ingest_events WHERE work_item_id = %s",
                "DELETE FROM work_items WHERE id = %s",
            ):
                cur.execute(stmt, (wi_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# map contract — change the enum on one side and it breaks HERE, not in production
# ---------------------------------------------------------------------------

_CONSOLE_STATUSES = {
    "new", "needs_clarification", "ready", "queued", "running", "blocked",
    "pr_ready", "review_feedback", "done", "failed", "cancelled",
}
_CONSOLE_EVENT_TYPES = {
    "created", "status_changed", "comment", "clarification_requested",
    "clarification_answered", "plan", "file_change", "tool_call",
    "test_result", "pr_opened", "feedback", "error", "note",
}


def test_every_fase1_status_has_console_mapping():
    for status in WorkItemStatus:
        mapped, _phase = map_status(status.value)
        assert mapped in _CONSOLE_STATUSES, f"{status.value} -> {mapped} outside the vocabulary"
    # and nothing beyond the real enum lives in the map (no stale entries)
    assert set(STATUS_MAP) == {s.value for s in WorkItemStatus}


def test_audit_event_map_targets_console_event_types():
    assert set(AUDIT_EVENT_MAP.values()) <= _CONSOLE_EVENT_TYPES


def test_a_stranded_escalation_projects_as_an_error_and_explains_itself():
    """ingest-gateway's stranded sweep hands an item to a human because its
    workflow no longer exists. Unmapped, it landed on the timeline as a `note` —
    the fallback for actions nobody classified — while the item went terminal, so
    the label a reader saw read as a remark. The message also has to carry the
    cause and the silence, or the timeline shows a hand-over with no reason
    attached."""
    ev_type, message = map_audit_event(
        "work_item_escalated_stranded",
        {"reason": "no_live_workflow", "status_before": "implementing", "idle_seconds": 144000},
    )

    assert ev_type == "error"
    assert "no_live_workflow" in message
    assert "implementing" in message
    assert "144000" in message


def test_split_title():
    t, d = split_title("Account\n\nMake a function that shows balance", fallback="x")
    assert t == "Account" and "balance" in d
    t, d = split_title("", fallback="repo#8")
    assert t == "repo#8" and d == ""


# ---------------------------------------------------------------------------
# real projection
# ---------------------------------------------------------------------------

def _seed(conn, wi_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_items (id, tenant_id, source, source_ref, repo, base_branch,
                                    requester, status, risk_class, budget, idempotency_key)
            VALUES (%s, 'crm-test', 'github', %s::jsonb, 'acme/repo', 'main',
                    'usr_test', 'implementing', 'low', %s::jsonb, %s)
            """,
            (wi_id, json.dumps({"repo": "acme/repo", "number": 42}),
             json.dumps({"max_usd": 5.0}), f"idem-{wi_id}"),
        )
        cur.execute(
            """
            INSERT INTO ingest_events (work_item_id, event_id, kind, payload, processed)
            VALUES (%s, %s, 'task_request', %s::jsonb, true)
            """,
            (wi_id, f"ev-{wi_id}",
             json.dumps({"content_snapshot": "Task title\n\nDetailed body."})),
        )
        cur.execute(
            "INSERT INTO audit_log (work_item_id, tenant_id, actor, action, details) "
            "VALUES (%s, 'crm-test', 'system:test', 'work_item_admitted', '{}'::jsonb), "
            "       (%s, 'crm-test', 'system:orchestrator', 'coder_turn_completed', %s::jsonb)",
            (wi_id, wi_id, json.dumps({"cost_usd": 1.23})),
        )
        cur.execute(
            "INSERT INTO model_call_ledger (tenant_id, work_item_id, stage, task_class, model, "
            "cost_usd, tokens_in, tokens_out) VALUES "
            "('crm-test', %s, 'coder', 'default', 'anthropic/claude', 1.23, 1000, 500)",
            (wi_id,),
        )
    conn.commit()


def test_projects_work_item_timeline_and_runs_idempotently():
    conn = psycopg2.connect(DSN)
    wi_id = f"wi-crm-{uuid.uuid4().hex[:10]}"
    try:
        _seed(conn, wi_id)
        drain(conn)
        drain(conn)  # idempotency: replay does not duplicate

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM console_rm.work_items_view WHERE work_item_id = %s", (wi_id,)
            )
            wi = cur.fetchone()
            assert wi is not None
            assert wi["status"] == "running" and wi["current_phase"] == "coding"
            assert wi["title"] == "Task title"
            assert wi["source_id"] == "acme/repo#42"
            assert float(wi["budget_usd"]) == 5.0
            # last_event came from the most recent audit row
            assert "coder turn completed" in (wi["last_event"] or "")

            cur.execute(
                "SELECT type, message FROM console_rm.timeline_events "
                "WHERE work_item_id = %s ORDER BY audit_id", (wi_id,)
            )
            events = cur.fetchall()
            assert [e["type"] for e in events] == ["created", "file_change"]
            assert "cost_usd=1.23" in events[1]["message"]

            cur.execute(
                "SELECT count(*) AS n, sum(cost_usd) AS cost FROM console_rm.runs_view "
                "WHERE work_item_id = %s", (wi_id,)
            )
            runs = cur.fetchone()
            # TWO cost sources (ledger + audit coder_turn_completed) and no
            # duplicates on the second pass (idempotency keyed by run_key).
            assert runs["n"] == 2
            assert float(runs["cost"]) == pytest.approx(2.46)
    finally:
        conn.rollback()
        conn.close()
        _cleanup(wi_id)


def test_status_transition_reprojects():
    conn = psycopg2.connect(DSN)
    wi_id = f"wi-crm-{uuid.uuid4().hex[:10]}"
    try:
        _seed(conn, wi_id)
        drain(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET status = 'escalated' WHERE id = %s", (wi_id,))
        conn.commit()
        drain(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, current_phase FROM console_rm.work_items_view WHERE work_item_id = %s",
                (wi_id,),
            )
            status, phase = cur.fetchone()
            assert (status, phase) == ("blocked", "escalated")
    finally:
        conn.rollback()
        conn.close()
        _cleanup(wi_id)


def test_cost_rollup_reconciles_with_ledger():
    """Plan 08 §E: the cost rollup matches the ledger EXACTLY (the cost truth,
    P8) for the tenant. Reconciliation — if they diverge, CI breaks HERE."""
    conn = psycopg2.connect(DSN)
    wi_id = f"wi-crm-{uuid.uuid4().hex[:10]}"
    try:
        _seed(conn, wi_id)  # 1 ledger row: cost 1.23, model anthropic/claude
        # one more model call on the SAME work item (aggregates into the same cell)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO model_call_ledger (tenant_id, work_item_id, stage, task_class, "
                "model, cost_usd, tokens_in, tokens_out) VALUES "
                "('crm-test', %s, 'planner', 'default', 'anthropic/claude', 0.77, 200, 100)",
                (wi_id,),
            )
        conn.commit()
        drain(conn)
        drain(conn)  # idempotency: a full recompute does not duplicate
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT COALESCE(sum(cost_usd),0) AS c FROM console_rm.cost_rollup WHERE tenant_id='crm-test'")
            rollup_cost = float(cur.fetchone()["c"])
            cur.execute(
                "SELECT COALESCE(sum(cost_usd),0) AS c FROM model_call_ledger WHERE tenant_id='crm-test'")
            ledger_cost = float(cur.fetchone()["c"])
            assert rollup_cost == pytest.approx(ledger_cost)  # exact reconciliation
            # our repo/model cell exists and sums both calls (1.23+0.77)
            cur.execute(
                "SELECT run_count, cost_usd FROM console_rm.cost_rollup "
                "WHERE tenant_id='crm-test' AND repo='acme/repo' AND model='anthropic/claude'")
            row = cur.fetchone()
            assert row is not None and row["run_count"] >= 2
    finally:
        conn.rollback()
        conn.close()
        _cleanup(wi_id)


def test_task_class_projected_into_view():
    """§E: the work_item's task_class reaches the view (the 'by category' charts)."""
    conn = psycopg2.connect(DSN)
    wi_id = f"wi-crm-{uuid.uuid4().hex[:10]}"
    try:
        _seed(conn, wi_id)
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET task_class = 'bug_fix' WHERE id = %s", (wi_id,))
        conn.commit()
        drain(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT task_class FROM console_rm.work_items_view WHERE work_item_id = %s", (wi_id,))
            assert cur.fetchone()[0] == "bug_fix"
    finally:
        conn.rollback()
        conn.close()
        _cleanup(wi_id)


def test_projects_evidence_preview_into_view():
    """Plan 08 §D (D5): preview_status/url from work_item_evidence reaches
    work_items_view (the dashboard shows the preview link next to the PR)."""
    conn = psycopg2.connect(DSN)
    wi_id = f"wi-crm-{uuid.uuid4().hex[:10]}"
    try:
        _seed(conn, wi_id)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work_item_evidence (work_item_id, tenant_id, preview_status, "
                "preview_url, demo_passed, video_artifact_key) "
                "VALUES (%s, 'crm-test', 'created', %s, true, 'evidence/demo.webm')",
                (wi_id, "https://preview-x.preview.dse.local"),
            )
        conn.commit()
        drain(conn)
        drain(conn)  # idempotency
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT preview_status, preview_url, demo_passed, video_artifact_key "
                "FROM console_rm.work_items_view WHERE work_item_id = %s", (wi_id,),
            )
            row = cur.fetchone()
            assert row["preview_status"] == "created"
            assert row["preview_url"] == "https://preview-x.preview.dse.local"
            assert row["demo_passed"] is True
            assert row["video_artifact_key"] == "evidence/demo.webm"
    finally:
        conn.rollback()
        conn.close()
        _cleanup(wi_id)


# ---------------------------------------------------------------------------
# The coder's spend is now metered into model_call_ledger at turn time. The
# audit-derived path stays as the legacy fallback, and these pin the boundary
# between them — because getting it wrong makes the fix WORSE than the bug: the
# one surface that was correct ($27.91 in runs_view) would double to $55.82.
# ---------------------------------------------------------------------------


def _seed_metered(conn, wi_id: str) -> int:
    """A coder turn that WAS metered: a ledger row plus an orchestrator audit
    row carrying its id — the shape every turn has from rc.7 onward."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_items (id, tenant_id, source, source_ref, repo, base_branch,
                                    requester, status, risk_class, budget, idempotency_key)
            VALUES (%s, 'crm-test', 'github', %s::jsonb, 'acme/repo', 'main',
                    'usr_test', 'implementing', 'low', %s::jsonb, %s)
            """,
            (wi_id, json.dumps({"repo": "acme/repo", "number": 7}),
             json.dumps({"max_usd": 5.0}), f"idem-{wi_id}"),
        )
        cur.execute(
            "INSERT INTO model_call_ledger (tenant_id, work_item_id, stage, task_class, model, "
            "cost_usd, tokens_in, tokens_out) VALUES "
            "('crm-test', %s, 'coder', 'default', 'anthropic/claude', 3.00, 10, 5) RETURNING id",
            (wi_id,),
        )
        ledger_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO audit_log (work_item_id, tenant_id, actor, action, details) "
            "VALUES (%s, 'crm-test', 'system:orchestrator', 'coder_turn_completed', %s::jsonb)",
            (wi_id, json.dumps({"cost_usd": 3.00, "ledger_id": ledger_id})),
        )
    conn.commit()
    return ledger_id


def test_a_metered_turn_produces_one_run_not_two():
    conn = psycopg2.connect(DSN)
    wi_id = f"wi-crm-{uuid.uuid4().hex[:10]}"
    try:
        _cleanup(wi_id)
        ledger_id = _seed_metered(conn, wi_id)
        drain(conn)
        drain(conn)  # idempotency: a second pass must not add anything
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_key, cost_usd FROM console_rm.runs_view WHERE work_item_id = %s",
                (wi_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1, f"expected exactly one run, got {rows}"
        assert rows[0][0] == f"ledger:{ledger_id}"
        assert float(rows[0][1]) == 3.00
    finally:
        _cleanup(wi_id)
        conn.close()


def test_a_backfilled_turn_stays_single_across_a_full_replay():
    """The DR case. audit_log is append-only, so a historical row can never gain
    a ledger_id — a backfill points the LEDGER row at the audit row instead. A
    rebuild from cursor 0 must not resurrect the legacy run on top of it."""
    conn = psycopg2.connect(DSN)
    wi_id = f"wi-crm-{uuid.uuid4().hex[:10]}"
    try:
        _cleanup(wi_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_items (id, tenant_id, source, source_ref, repo, base_branch,
                                        requester, status, risk_class, budget, idempotency_key)
                VALUES (%s, 'crm-test', 'github', %s::jsonb, 'acme/repo', 'main',
                        'usr_test', 'implementing', 'low', %s::jsonb, %s)
                """,
                (wi_id, json.dumps({"repo": "acme/repo", "number": 8}),
                 json.dumps({"max_usd": 5.0}), f"idem-{wi_id}"),
            )
            # the historical audit row: no ledger_id, and it can never get one
            cur.execute(
                "INSERT INTO audit_log (work_item_id, tenant_id, actor, action, details) "
                "VALUES (%s, 'crm-test', 'system:orchestrator', 'coder_turn_completed', %s::jsonb) "
                "RETURNING id",
                (wi_id, json.dumps({"cost_usd": 4.00})),
            )
            audit_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO model_call_ledger (tenant_id, work_item_id, stage, task_class, model, "
                "cost_usd, tokens_in, tokens_out, source_audit_id) VALUES "
                "('crm-test', %s, 'coder', 'default', 'anthropic/claude', 4.00, 10, 5, %s)",
                (wi_id, audit_id),
            )
        conn.commit()
        drain(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM console_rm.runs_view WHERE work_item_id = %s", (wi_id,))
            assert cur.fetchone()[0] == 1
            cur.execute("UPDATE console_rm.projection_cursor SET last_id = 0")
        conn.commit()
        drain(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_key FROM console_rm.runs_view WHERE work_item_id = %s", (wi_id,)
            )
            rows = cur.fetchall()
        assert len(rows) == 1, f"a cursor-0 replay duplicated the run: {rows}"
        assert rows[0][0].startswith("ledger:")
    finally:
        _cleanup(wi_id)
        conn.close()


def test_a_cursor_ahead_of_a_reset_source_rebuilds_the_read_model():
    """The disposable-schema harness (with_test_database, the CI boundary)
    restarts every BIGSERIAL at 1, while console_rm — schema-qualified, so
    database-global — survives OUTSIDE the boundary carrying the cursors of a
    previous incarnation of the sources. `id > cursor` then matches nothing and
    drain() "converges" having projected nothing: the work_items_view row is
    there (timestamp cursor; now() is globally monotonic) but last_event=None
    and runs_view stays empty. An append-only source can only be BEHIND its id
    cursor when it is not the same source anymore, and the only correct cursor
    for a reset source is 0 — full replay, the documented DR path, idempotent
    by design. This pins that deterministically in EVERY environment by moving
    the cursor beyond anything the visible sources will ever show this run."""
    conn = psycopg2.connect(DSN)
    wi_id = f"wi-crm-{uuid.uuid4().hex[:10]}"
    try:
        _seed(conn, wi_id)
        with conn.cursor() as cur:
            for source in ("audit_log", "model_call_ledger"):
                cur.execute(
                    "INSERT INTO console_rm.projection_cursor (source) VALUES (%s) "
                    "ON CONFLICT (source) DO NOTHING",
                    (source,),
                )
                cur.execute(
                    f"UPDATE console_rm.projection_cursor SET last_id = "
                    f"(SELECT COALESCE(max(id), 0) + 1000000 FROM {source}) "
                    f"WHERE source = %s",
                    (source,),
                )
        conn.commit()
        drain(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT last_event FROM console_rm.work_items_view WHERE work_item_id = %s",
                (wi_id,),
            )
            wi = cur.fetchone()
            assert wi is not None
            assert "coder turn completed" in (wi["last_event"] or "")
            cur.execute(
                "SELECT count(*) AS n FROM console_rm.timeline_events WHERE work_item_id = %s",
                (wi_id,),
            )
            assert cur.fetchone()["n"] == 2
            cur.execute(
                "SELECT count(*) AS n, sum(cost_usd) AS cost FROM console_rm.runs_view "
                "WHERE work_item_id = %s",
                (wi_id,),
            )
            runs = cur.fetchone()
            assert runs["n"] == 2
            assert float(runs["cost"]) == pytest.approx(2.46)
    finally:
        conn.rollback()
        conn.close()
        _cleanup(wi_id)
