"""Postgres access for the tables in `migrations/0006_wse.sql`. Uses the
`dse_app` role (same convention as `dse_audit`/`dse_identity`) — no ORM framework
(P7 boring-first)."""
from __future__ import annotations

import json
import os
from typing import Any

import psycopg2

_DSN = os.environ.get(
    "DSE_VALIDATION_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def get_connection():
    return psycopg2.connect(_DSN)


def _jsonb_safe(value: Any) -> Any:
    """Strip NUL from anything on its way to a `::jsonb` column.

    jsonb cannot represent U+0000. `json.dumps` happily encodes it as `\\u0000`
    and Postgres then rejects the cast — so one NUL byte anywhere in a payload
    fails the INSERT. L1 findings carry subprocess output captured with
    `text=True`, and a compiler or linter that emits a NUL (a binary file read
    as text, a crash dump) would wedge the whole L1 activity: the write raises,
    the activity fails, the retry replays the same bytes and raises again.

    The guard belongs here, at the boundary that cannot hold the value, rather
    than in each of the dozen places a detail string is produced."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: _jsonb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonb_safe(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# validation_runs (WSE-E1) — 1 evidence row per L1 run.
# ---------------------------------------------------------------------------
def record_validation_run(work_item_id: str, tenant_id: str, passed: bool, findings: list[dict[str, Any]]) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO validation_runs (work_item_id, tenant_id, passed, findings)
                VALUES (%s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (work_item_id, tenant_id, passed, json.dumps(_jsonb_safe(findings))),
            )
            run_id = cur.fetchone()[0]
        conn.commit()
        return run_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_pr_tracking (WSE-E3-T6) — enforces the "1 PR per WorkItem" idempotency.
# ---------------------------------------------------------------------------
def get_tracked_pr(work_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_item_id, tenant_id, repo, branch, pr_number, pr_url, compare_url "
                "FROM wse_pr_tracking WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ["work_item_id", "tenant_id", "repo", "branch", "pr_number", "pr_url", "compare_url"]
        return dict(zip(keys, row))
    finally:
        conn.close()


def save_tracked_pr(
    work_item_id: str,
    tenant_id: str,
    repo: str,
    branch: str,
    pr_number: int | None,
    pr_url: str,
    compare_url: str | None = None,
) -> None:
    # Phase 2 (WSE-E3-T8): pr_number can be NULL in strict mode (only the compare
    # link posted, PR not yet opened by a human). `pr_url` holds the best known URL
    # (the compare link while pr_number IS NULL, then the PR's).
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_pr_tracking
                    (work_item_id, tenant_id, repo, branch, pr_number, pr_url, compare_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    pr_number = EXCLUDED.pr_number, pr_url = EXCLUDED.pr_url,
                    compare_url = EXCLUDED.compare_url
                """,
                (work_item_id, tenant_id, repo, branch, pr_number, pr_url, compare_url),
            )
        conn.commit()
    finally:
        conn.close()


def adopt_tracked_pr(work_item_id: str, pr_number: int, pr_url: str) -> None:
    """WSE-E3-T8 — strict mode: a human opened the PR from the compare link; fills
    pr_number/pr_url on the existing row WITHOUT creating another. Idempotent: it
    only writes if there was no pr_number yet (the first human to open wins;
    re-runs do not overwrite)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wse_pr_tracking
                   SET pr_number = %s, pr_url = %s
                 WHERE work_item_id = %s AND pr_number IS NULL
                """,
                (pr_number, pr_url, work_item_id),
            )
        conn.commit()
    finally:
        conn.close()


def _delete_tracked_pr_for_test(work_item_id: str) -> None:
    """Test-only — simulates a "crash before persisting" by deleting the row
    without deleting the GitHub-side state (fake). Never called in production."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM wse_pr_tracking WHERE work_item_id = %s", (work_item_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_comment_refs — CommentStateStore (dse_contracts.mutable_comment).
# ---------------------------------------------------------------------------
class PostgresCommentStateStore:
    def get_ref(self, work_item_id: str, surface: str) -> str | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT comment_ref FROM wse_comment_refs WHERE work_item_id = %s AND surface = %s",
                    (work_item_id, surface),
                )
                row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def save_ref(self, work_item_id: str, surface: str, comment_ref: str) -> None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wse_comment_refs (work_item_id, surface, comment_ref)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (work_item_id, surface) DO UPDATE SET
                        comment_ref = EXCLUDED.comment_ref, updated_at = now()
                    """,
                    (work_item_id, surface, comment_ref),
                )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# wse_ci_status (WSE-E4-T9a).
# ---------------------------------------------------------------------------
def save_ci_status(work_item_id: str, pr_number: int, status: str, detail: dict[str, Any]) -> str | None:
    """Upserts the row and returns the status it held BEFORE this write — None the
    first time the work item is observed.

    The previous value comes from the same statement instead of a SELECT before
    it, because callers use it to decide whether the observation deserves an audit
    row: the question is "what did THIS write replace", and only the writing
    statement answers it. A separate SELECT answers "what was there a moment ago",
    which a concurrent attempt of the same retryable Activity can invalidate
    between the two round trips. The CTE is evaluated on the statement's snapshot,
    so it still reads the row as it was before the upsert.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH prev AS (SELECT status FROM wse_ci_status WHERE work_item_id = %s)
                INSERT INTO wse_ci_status (work_item_id, pr_number, status, detail)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    pr_number = EXCLUDED.pr_number, status = EXCLUDED.status,
                    detail = EXCLUDED.detail, updated_at = now()
                RETURNING (SELECT status FROM prev)
                """,
                (work_item_id, work_item_id, pr_number, status, json.dumps(detail)),
            )
            previous_status = cur.fetchone()[0]
        conn.commit()
        return previous_status
    finally:
        conn.close()


def get_ci_status(work_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_item_id, pr_number, status, detail FROM wse_ci_status WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"work_item_id": row[0], "pr_number": row[1], "status": row[2], "detail": row[3]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_l2_reviews (WSE-E2-T4) — evidence for each run of the L2 Reviewer session.
# ---------------------------------------------------------------------------
def record_l2_review(
    work_item_id: str,
    tenant_id: str,
    iteration: int,
    passed: bool,
    objections: list[str],
    cost_usd: float,
) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_l2_reviews
                    (work_item_id, tenant_id, iteration, passed, objections, cost_usd)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                RETURNING id
                """,
                (work_item_id, tenant_id, iteration, passed, json.dumps(objections), cost_usd),
            )
            review_id = cur.fetchone()[0]
        conn.commit()
        return review_id
    finally:
        conn.close()


def get_l2_reviews(work_item_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT iteration, passed, objections, cost_usd, run_at "
                "FROM wse_l2_reviews WHERE work_item_id = %s ORDER BY run_at ASC, id ASC",
                (work_item_id,),
            )
            rows = cur.fetchall()
        return [
            {"iteration": r[0], "passed": r[1], "objections": r[2], "cost_usd": float(r[3]), "run_at": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_fix_loops (WSE-E2-T5) — durable counter for the bounded L2->Coder loop.
# ---------------------------------------------------------------------------
def get_fix_loop(work_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_item_id, tenant_id, iterations, spent_usd, exhausted "
                "FROM wse_fix_loops WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "work_item_id": row[0],
            "tenant_id": row[1],
            "iterations": row[2],
            "spent_usd": float(row[3]),
            "exhausted": row[4],
        }
    finally:
        conn.close()


def upsert_fix_loop(
    work_item_id: str,
    tenant_id: str,
    iterations: int,
    spent_usd: float,
    exhausted: bool,
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_fix_loops (work_item_id, tenant_id, iterations, spent_usd, exhausted)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    iterations = EXCLUDED.iterations, spent_usd = EXCLUDED.spent_usd,
                    exhausted = EXCLUDED.exhausted, updated_at = now()
                """,
                (work_item_id, tenant_id, iterations, spent_usd, exhausted),
            )
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# Phase 3 (0017_wse3.sql)
# ===========================================================================

# ---------------------------------------------------------------------------
# wse_artifacts + wse_artifact_access_log (WSE-E5-T12)
# ---------------------------------------------------------------------------
def record_artifact(
    *,
    work_item_id: str,
    tenant_id: str,
    kind: str,
    bucket: str,
    store_key: str,
    content_type: str,
    size_bytes: int,
    multipart: bool,
    ttl_seconds: int,
    expires_at,
) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_artifacts
                    (work_item_id, tenant_id, kind, bucket, store_key, content_type,
                     size_bytes, multipart, ttl_seconds, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (bucket, store_key) DO UPDATE SET
                    content_type = EXCLUDED.content_type, size_bytes = EXCLUDED.size_bytes,
                    multipart = EXCLUDED.multipart, ttl_seconds = EXCLUDED.ttl_seconds,
                    expires_at = EXCLUDED.expires_at,
                    quarantined_at = NULL, quarantine_key = NULL
                RETURNING id
                """,
                (work_item_id, tenant_id, kind, bucket, store_key, content_type,
                 size_bytes, multipart, ttl_seconds, expires_at),
            )
            artifact_id = cur.fetchone()[0]
        conn.commit()
        return artifact_id
    finally:
        conn.close()


_ARTIFACT_COLS = (
    "id, work_item_id, tenant_id, kind, bucket, store_key, content_type, "
    "size_bytes, multipart, ttl_seconds, expires_at, quarantined_at, quarantine_key"
)


def _artifact_row_to_dict(row) -> dict[str, Any]:
    keys = ["id", "work_item_id", "tenant_id", "kind", "bucket", "store_key", "content_type",
            "size_bytes", "multipart", "ttl_seconds", "expires_at", "quarantined_at", "quarantine_key"]
    return dict(zip(keys, row))


def get_artifact(work_item_id: str, store_key: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_ARTIFACT_COLS} FROM wse_artifacts WHERE work_item_id = %s AND store_key = %s",
                (work_item_id, store_key),
            )
            row = cur.fetchone()
        return _artifact_row_to_dict(row) if row else None
    finally:
        conn.close()


def list_artifacts(work_item_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_ARTIFACT_COLS} FROM wse_artifacts WHERE work_item_id = %s ORDER BY id",
                (work_item_id,),
            )
            rows = cur.fetchall()
        return [_artifact_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def mark_artifact_quarantined(artifact_id: int, *, quarantine_key: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wse_artifacts
                   SET quarantined_at = now(), quarantine_key = %s
                 WHERE id = %s AND quarantined_at IS NULL
                """,
                (quarantine_key, artifact_id),
            )
        conn.commit()
    finally:
        conn.close()


def record_artifact_access(
    *,
    artifact_id: int,
    work_item_id: str,
    tenant_id: str,
    pr_number: int | None,
    accessor: str,
    via: str,
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_artifact_access_log
                    (artifact_id, work_item_id, tenant_id, pr_number, accessor, via)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (artifact_id, work_item_id, tenant_id, pr_number, accessor, via),
            )
        conn.commit()
    finally:
        conn.close()


def list_artifact_accesses(work_item_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT artifact_id, pr_number, accessor, via, accessed_at "
                "FROM wse_artifact_access_log WHERE work_item_id = %s ORDER BY id",
                (work_item_id,),
            )
            rows = cur.fetchall()
        return [
            {"artifact_id": r[0], "pr_number": r[1], "accessor": r[2], "via": r[3], "accessed_at": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def list_quarantined_work_items_with_artifacts() -> list[str]:
    """ACTIVE quarantined work items (WS-F's table, data-plane — we do not edit
    their migration) that still have a non-quarantined artifact in the store."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT a.work_item_id
                  FROM wse_artifacts a
                  JOIN dse_work_item_quarantine q
                    ON q.work_item_id = a.work_item_id AND q.released_at IS NULL
                 WHERE a.quarantined_at IS NULL
                """
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_previews + wse_preview_caps (WSE-E4-T10 / ADR-26)
# ---------------------------------------------------------------------------
def upsert_preview(
    *,
    work_item_id: str,
    tenant_id: str,
    pr_number: int | None,
    repo: str,
    status: str,
    namespace: str | None = None,
    url: str | None = None,
    detail: str = "",
    ttl_seconds: int = 3600,
    expires_at=None,
    deep_path: str = "",
    deep_note: str = "",
    test_guide: dict | None = None,
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_previews
                    (work_item_id, tenant_id, pr_number, repo, status, namespace, url,
                     detail, ttl_seconds, expires_at, deep_path, deep_note, test_guide)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    pr_number = EXCLUDED.pr_number, status = EXCLUDED.status,
                    namespace = EXCLUDED.namespace, url = EXCLUDED.url,
                    detail = EXCLUDED.detail, ttl_seconds = EXCLUDED.ttl_seconds,
                    expires_at = EXCLUDED.expires_at,
                    deep_path = EXCLUDED.deep_path, deep_note = EXCLUDED.deep_note,
                    test_guide = EXCLUDED.test_guide,
                    reaped_at = CASE WHEN EXCLUDED.status = 'created' THEN NULL
                                     ELSE wse_previews.reaped_at END,
                    updated_at = now()
                """,
                (work_item_id, tenant_id, pr_number, repo, status, namespace, url,
                 detail, ttl_seconds, expires_at, deep_path, deep_note,
                 json.dumps(test_guide or {})),
            )
        conn.commit()
    finally:
        conn.close()


def get_preview(work_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_item_id, tenant_id, pr_number, repo, status, namespace, url, "
                "detail, ttl_seconds, expires_at, reaped_at, deep_path, deep_note, "
                "test_guide "
                "FROM wse_previews WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ["work_item_id", "tenant_id", "pr_number", "repo", "status", "namespace",
                "url", "detail", "ttl_seconds", "expires_at", "reaped_at",
                "deep_path", "deep_note", "test_guide"]
        return dict(zip(keys, row))
    finally:
        conn.close()


def count_active_previews(tenant_id: str) -> int:
    """How many previews are actually holding a slot.

    A row past its TTL does NOT hold one. The reaper runs entirely from cluster
    state — it lists the namespaces carrying the preview label, reads their
    `dse.fintex/expires-at` annotation and deletes the expired ones — and never
    writes to this table. So an expired row is stale accounting, not capacity:
    the namespace it refers to is already gone.

    Counting those was a permanent outage, not a rounding error. Three previews
    expired on a Saturday, the reaper deleted their namespaces, the rows stayed
    `created`, and every later preview was refused with `concurrency_cap` — the
    feature switched itself off for good after three uses.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM wse_previews "
                "WHERE tenant_id = %s AND status = 'created' AND reaped_at IS NULL "
                "  AND (expires_at IS NULL OR expires_at > now())",
                (tenant_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def live_sibling_preview_namespace(work_item_id: str) -> tuple[str | None, bool]:
    """G-3 degrau 2 (2026-08-09): o namespace de preview VIVO de um irmão de
    group_id deste item — o backend para onde o proxy /api do FE aponta. Junta
    `work_items` (grupo) com `wse_previews` (preview created e não-reaped, TTL
    válido). Retorna `(namespace, True)` do irmão vivo mais recente, ou
    `(None, False)` — degrau 1. Nunca casa o próprio item (só IRMÃOS)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.namespace
                  FROM work_items me
                  JOIN work_items sib
                    ON COALESCE(sib.group_id, sib.id) = COALESCE(me.group_id, me.id)
                   AND sib.id <> me.id
                  JOIN wse_previews p ON p.work_item_id = sib.id
                 WHERE me.id = %s
                   AND p.status = 'created' AND p.reaped_at IS NULL
                   AND (p.expires_at IS NULL OR p.expires_at > now())
                   AND p.namespace IS NOT NULL
                 ORDER BY p.created_at DESC
                 LIMIT 1
                """,
                (work_item_id,),
            )
            row = cur.fetchone()
            return (row[0], True) if row else (None, False)
    finally:
        conn.close()


def list_oldest_active_previews(tenant_id: str, limit: int = 1) -> list[dict[str, Any]]:
    """The eviction queue for the cap (operator decision 2026-07-23): cap full =>
    the oldest yields its slot to the new PR.

    Filters TTL exactly like `count_active_previews`, and that is the whole
    point: while it did not, the two disagreed. This one used to return expired
    rows FIRST — on the reasoning that they cost nothing to evict — but the cap
    is counted with `expires_at > now()`, so evicting an expired row frees
    nothing the counter was measuring. With the eviction limit at
    `active - cap + 1` (one row when the cap is exactly full), that single
    eviction was spent on a row that never counted, the recount did not drop,
    and the flow degraded anyway.

    Measured on PR #4 (2026-08-11 19:30:32): `preview_evicted_lru` fired and
    `preview_degraded` followed one millisecond later with `active` unchanged
    at 3.

    Expired rows are still cheap to collect — they are just not EVICTION. The
    caller harvests them first, as garbage, and only then evicts among rows that
    the counter can actually see.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT work_item_id, tenant_id, namespace FROM wse_previews
                 WHERE tenant_id = %s AND status = 'created' AND reaped_at IS NULL
                   AND (expires_at IS NULL OR expires_at > now())
                 ORDER BY created_at ASC
                 LIMIT %s
                """,
                (tenant_id, limit),
            )
            rows = cur.fetchall()
        return [{"work_item_id": r[0], "tenant_id": r[1], "namespace": r[2]} for r in rows]
    finally:
        conn.close()


def get_preview_cap(tenant_id: str) -> int | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT max_concurrent FROM wse_preview_caps WHERE tenant_id = %s", (tenant_id,))
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_preview_cap(tenant_id: str, max_concurrent: int) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_preview_caps (tenant_id, max_concurrent) VALUES (%s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET max_concurrent = EXCLUDED.max_concurrent
                """,
                (tenant_id, max_concurrent),
            )
        conn.commit()
    finally:
        conn.close()


def list_expired_previews(now=None) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT work_item_id, tenant_id, namespace FROM wse_previews
                 WHERE status = 'created' AND reaped_at IS NULL
                   AND expires_at IS NOT NULL AND expires_at <= COALESCE(%s, now())
                """,
                (now,),
            )
            rows = cur.fetchall()
        return [{"work_item_id": r[0], "tenant_id": r[1], "namespace": r[2]} for r in rows]
    finally:
        conn.close()


def mark_preview_reaped(work_item_id: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE wse_previews SET status = 'reaped', reaped_at = now(), updated_at = now() "
                "WHERE work_item_id = %s AND reaped_at IS NULL",
                (work_item_id,),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_ci_reruns + wse_ci_repair_episodes (WSE-E4-T9b)
# ---------------------------------------------------------------------------
def record_ci_rerun(
    *,
    work_item_id: str,
    tenant_id: str,
    pr_number: int,
    fix_commit_sha: str,
    check_run_ids: list,
    check_names: list,
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_ci_reruns
                    (work_item_id, tenant_id, pr_number, fix_commit_sha, check_run_ids, check_names)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
                """,
                (work_item_id, tenant_id, pr_number, fix_commit_sha,
                 json.dumps(check_run_ids), json.dumps(check_names)),
            )
        conn.commit()
    finally:
        conn.close()


def list_ci_reruns(work_item_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pr_number, fix_commit_sha, check_run_ids, check_names, requested_at "
                "FROM wse_ci_reruns WHERE work_item_id = %s ORDER BY id",
                (work_item_id,),
            )
            rows = cur.fetchall()
        return [
            {"pr_number": r[0], "fix_commit_sha": r[1], "check_run_ids": r[2],
             "check_names": r[3], "requested_at": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def record_ci_repair_episode(
    *,
    tenant_id: str,
    work_item_id: str,
    check_name: str,
    failure_signature: str,
    fix_commit_sha: str,
    provenance: dict,
) -> dict[str, Any]:
    """Records the CI-repair episode (tenant-scoped) with `occurrence_n` = the
    number of occurrences of the SAME pattern (tenant, signature) so far, inclusive.
    NO skill is created/activated — only the episode (promotion is Phase 4)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM wse_ci_repair_episodes "
                "WHERE tenant_id = %s AND failure_signature = %s",
                (tenant_id, failure_signature),
            )
            occurrence_n = cur.fetchone()[0] + 1
            cur.execute(
                """
                INSERT INTO wse_ci_repair_episodes
                    (tenant_id, work_item_id, check_name, failure_signature,
                     fix_commit_sha, occurrence_n, provenance)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (tenant_id, work_item_id, check_name, failure_signature,
                 fix_commit_sha, occurrence_n, json.dumps(provenance)),
            )
            episode_id = cur.fetchone()[0]
        conn.commit()
        return {"id": episode_id, "occurrence_n": occurrence_n}
    finally:
        conn.close()


def list_ci_repair_episodes(tenant_id: str, failure_signature: str | None = None) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if failure_signature is None:
                cur.execute(
                    "SELECT work_item_id, check_name, failure_signature, fix_commit_sha, occurrence_n, provenance "
                    "FROM wse_ci_repair_episodes WHERE tenant_id = %s ORDER BY id",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "SELECT work_item_id, check_name, failure_signature, fix_commit_sha, occurrence_n, provenance "
                    "FROM wse_ci_repair_episodes WHERE tenant_id = %s AND failure_signature = %s ORDER BY id",
                    (tenant_id, failure_signature),
                )
            rows = cur.fetchall()
        return [
            {"work_item_id": r[0], "check_name": r[1], "failure_signature": r[2],
             "fix_commit_sha": r[3], "occurrence_n": r[4], "provenance": r[5]}
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_evidence_publications (WSE-E5-T14 / ADR-26 debounce)
# ---------------------------------------------------------------------------
def get_evidence_publication(work_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_item_id, tenant_id, last_commit_sha, fingerprint, published_at "
                "FROM wse_evidence_publications WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ["work_item_id", "tenant_id", "last_commit_sha", "fingerprint", "published_at"]
        return dict(zip(keys, row))
    finally:
        conn.close()


def upsert_evidence_publication(
    work_item_id: str, tenant_id: str, last_commit_sha: str, fingerprint: str
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_evidence_publications
                    (work_item_id, tenant_id, last_commit_sha, fingerprint, published_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (work_item_id) DO UPDATE SET
                    last_commit_sha = EXCLUDED.last_commit_sha,
                    fingerprint = EXCLUDED.fingerprint, published_at = now()
                """,
                (work_item_id, tenant_id, last_commit_sha, fingerprint),
            )
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# Phase 4 (0020_wse4.sql)
# ===========================================================================

# ---------------------------------------------------------------------------
# wse_base_updates (WSE-E6-T16) — evidence for each merge-base/rebase.
# ---------------------------------------------------------------------------
def record_base_update(
    *,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    branch: str,
    base_branch: str,
    strategy: str,
    conflict: bool,
    orphaned_threads: int,
    anchored_threads: int,
    first_human_review_done: bool,
    old_tip_sha: str,
    new_tip_sha: str,
    detail: str,
) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_base_updates
                    (work_item_id, tenant_id, repo, branch, base_branch, strategy,
                     conflict, orphaned_threads, anchored_threads, first_human_review_done,
                     old_tip_sha, new_tip_sha, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (work_item_id, tenant_id, repo, branch, base_branch, strategy,
                 conflict, orphaned_threads, anchored_threads, first_human_review_done,
                 old_tip_sha, new_tip_sha, detail),
            )
            row_id = cur.fetchone()[0]
        conn.commit()
        return row_id
    finally:
        conn.close()


def list_base_updates(work_item_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT strategy, conflict, orphaned_threads, anchored_threads, "
                "first_human_review_done, old_tip_sha, new_tip_sha, detail, created_at "
                "FROM wse_base_updates WHERE work_item_id = %s ORDER BY id",
                (work_item_id,),
            )
            rows = cur.fetchall()
        keys = ["strategy", "conflict", "orphaned_threads", "anchored_threads",
                "first_human_review_done", "old_tip_sha", "new_tip_sha", "detail", "created_at"]
        return [dict(zip(keys, r)) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# skill_episode (source='review_feedback') — WSE-E6-T18. WS-C's table
# (migration 0019); WS-E only does INSERT/SELECT (granted by 0019). NO skill is
# created here — it is only the episode (the boundary is tested).
# ---------------------------------------------------------------------------
def record_review_feedback_episode(
    *,
    tenant_id: str,
    work_item_id: str,
    pattern_key: str,
    provenance: dict,
) -> dict[str, Any]:
    """Records the episode (source='review_feedback') with `occurrence_n` = the
    number of occurrences of the SAME (tenant, source, pattern_key) so far,
    inclusive. Same discipline as `record_ci_repair_episode` (Phase 3)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM skill_episode "
                "WHERE tenant_id = %s AND source = 'review_feedback' AND pattern_key = %s",
                (tenant_id, pattern_key),
            )
            occurrence_n = cur.fetchone()[0] + 1
            cur.execute(
                """
                INSERT INTO skill_episode
                    (tenant_id, source, work_item_id, pattern_key, occurrence_n, provenance)
                VALUES (%s, 'review_feedback', %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (tenant_id, work_item_id, pattern_key, occurrence_n, json.dumps(provenance)),
            )
            episode_id = cur.fetchone()[0]
        conn.commit()
        return {"id": episode_id, "occurrence_n": occurrence_n}
    finally:
        conn.close()


def list_skill_episodes(
    tenant_id: str, source: str | None = None, pattern_key: str | None = None
) -> list[dict[str, Any]]:
    clauses = ["tenant_id = %s"]
    params: list[Any] = [tenant_id]
    if source is not None:
        clauses.append("source = %s")
        params.append(source)
    if pattern_key is not None:
        clauses.append("pattern_key = %s")
        params.append(pattern_key)
    where = " AND ".join(clauses)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT source, work_item_id, pattern_key, occurrence_n, provenance, created_at "
                f"FROM skill_episode WHERE {where} ORDER BY id",
                tuple(params),
            )
            rows = cur.fetchall()
        keys = ["source", "work_item_id", "pattern_key", "occurrence_n", "provenance", "created_at"]
        return [dict(zip(keys, r)) for r in rows]
    finally:
        conn.close()


def count_skill_registry(tenant_id: str) -> int:
    """Count of skills in the registry for the tenant — used by the BOUNDARY tests
    (WSE-E6-T18): recording an episode must NOT create/activate a skill."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM skill_registry WHERE tenant_id = %s", (tenant_id,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def egress_denials_since(started_at, *, tenant_id: str | None = None,
                         limit: int = 20) -> list[tuple[str, int]]:
    """Os hosts que o proxy de egress recusou desde `started_at`.

    Best-effort por construção: isto enriquece uma mensagem de erro e nunca
    decide veredito, então banco indisponível devolve lista vazia em vez de
    trocar um diagnóstico ruim por um crash.

    Sem `work_item_id` na consulta porque o proxy não o tem — ele vê uma
    conexão TCP, não um item de trabalho. A janela de tempo é a correlação
    possível, e quem escreve a mensagem é responsável por dizer isso ao leitor
    (ver `egress_denial_note`)."""
    if started_at is None:
        return []
    sql = ("SELECT details->>'host', details->>'port' FROM audit_log "
           "WHERE action = 'egress_denied' AND ts >= %s")
    args: list = [started_at]
    if tenant_id:
        sql += " AND tenant_id = %s"
        args.append(tenant_id)
    sql += " ORDER BY ts DESC LIMIT %s"
    args.append(limit)
    try:
        conn = get_connection()
    except Exception:  # noqa: BLE001 — ver docstring
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, args)
            saida: list[tuple[str, int]] = []
            for host, porta in cur.fetchall():
                if not host:
                    continue
                try:
                    saida.append((str(host), int(porta or 443)))
                except (TypeError, ValueError):
                    saida.append((str(host), 443))
            return saida
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
