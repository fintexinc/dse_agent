"""WS-B-owned Activities — they are NOT part of the cross-workstream contract
in `dse_contracts.activities` (those are implemented by WS-C/WS-E). These three
exist to keep the workflow 100% deterministic (P1 discipline: all I/O —
Postgres, audit — lives in an Activity, never directly in the `@workflow.run`
body):

- `update_work_item_status`: WS-B's only write path into the shared
  `work_items` table (`status`/`repo`/`pr_number` and the other operational
  columns — nothing else in this service UPDATEs it). The workflow is the
  legitimate owner of the state machine (P1), so it is the one that writes the
  transition — other services (adapters, admin UI) read the same row.
- `check_clarification_completeness`: a pure checklist (repo? acceptance
  criteria? base branch?) over the WorkItem snapshot — computed in an Activity
  rather than in the workflow body out of discipline (there is no reason for it
  not to be an Activity, and keeping it that way leaves room for the checklist
  to grow without risking non-determinism).
- `emit_audit_event` (stable name = `dse_contracts.activities.ACTIVITY_EMIT_AUDIT`):
  Temporal has no audit log of its own; this is the Activity that ALL
  workstreams (including the orchestrator itself) call to write a row into
  `audit_log` via `dse_audit.emit` (P8).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time as _time
from typing import Any

from temporalio import activity

from dse_contracts.repos import TENANT_REPO_CATALOGUE_SQL
from dse_contracts.activities import (
    ACTIVITY_EMIT_AUDIT,
    ACTIVITY_POST_TRACKING_COMMENT,
    PersistWorkItemStateInput,
)
import dse_audit

from dse_orchestrator import policy

logger = logging.getLogger("dse_orchestrator.local_activities")

#: How many times the router asks the gateway before giving up and falling
#: through to the human repo picker. Three attempts over ~3s cover a pod restart
#: or a momentary 502; longer than that is an outage a human should see.
_ROUTER_ATTEMPTS = 3
_ROUTER_BACKOFF_SECONDS = 1.0

LOCAL_ACTIVITY_UPDATE_STATUS = "update_work_item_status"
LOCAL_ACTIVITY_CHECK_CLARIFICATION = "check_clarification_completeness"
LOCAL_ACTIVITY_LOAD_WORK_ITEM = "load_work_item"
# Phase B (report 07) — reflects the WorkItem status on the Jira board
# (column transition), via the adapter-jira serialized queue.
LOCAL_ACTIVITY_POST_STATUS_TRANSITION = "post_status_transition"
# Phase 2 (WSB-E3-T2) — approver resolution (I/O: DB + CODEOWNERS) and durable
# projection of the gate (WSB migration 0009).
LOCAL_ACTIVITY_RESOLVE_APPROVER = "resolve_plan_approver"
LOCAL_ACTIVITY_RECORD_GATE = "record_plan_approval"
# Phase 3 — durable projection of the evidence pipeline state (migration 0014)
# and emission of the OTel history-size metric (ALERTING-RULES.md §3).
LOCAL_ACTIVITY_RECORD_EVIDENCE = "record_evidence_state"
LOCAL_ACTIVITY_EMIT_HISTORY_METRIC = "emit_history_metric"
# Phase 4 — skill-learning input (source=clarification episode, migration 0019,
# owned by WS-C; WS-B only INSERTS the input) and PR quality metric
# (pilot gate "PR quality thresholds").
LOCAL_ACTIVITY_RECORD_SKILL_EPISODE = "record_skill_episode"
LOCAL_ACTIVITY_RECORD_RUN_EPISODE = "record_run_episode"

# One diário entry, bounded at the source. The reader injects the newest few into
# a Planner context whose entire budget is 16.000 chars, so an unbounded digest
# here would become an unbounded prompt there — and the truncation would happen
# at the far end, where nothing knows what it cut.
_RUN_DIGEST_MAX_CHARS = 800
LOCAL_ACTIVITY_EMIT_PR_QUALITY_METRIC = "emit_pr_quality_metric"
# Multi-repo. A request that genuinely touches two repositories becomes two work
# items, one per repo, sharing a group — because everything downstream is keyed
# by work_item_id, and three tables enforce that as a PRIMARY KEY.
LOCAL_ACTIVITY_ROUTE_REPOS = "route_repos"
LOCAL_ACTIVITY_FAN_OUT_SIBLINGS = "fan_out_sibling_work_items"
# Plan 08 §D — resolves the repo's deploys_preview gate (repo_bindings) to
# decide whether the preview environment applies (P1, deterministic, fail-safe).
LOCAL_ACTIVITY_PREVIEW_ENABLED = "preview_enabled_for_repo"
# The deployment default for the per-WorkItem dollar ceiling. It is an Activity,
# not a module read inside the workflow body, so the env read happens OUTSIDE the
# sandbox and its RESULT lands in history — replay stays deterministic even if
# the ConfigMap changes between worker versions (P1).
LOCAL_ACTIVITY_RESOLVE_BUDGET_CAP = "resolve_budget_cap"

_DSN = os.environ.get(
    "DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
)


def _get_connection():
    import psycopg2

    return psycopg2.connect(_DSN)


def _none_if_blank(value: Any) -> str | None:
    """A blank string means "the caller has nothing", never "erase the column".

    `COALESCE(%s, col)` only defends the column against NULL. A caller that
    renders an unset field as `""` (or as whitespace) would therefore blank a
    value that is already good. Normalizing here, at the single write path,
    keeps that guarantee no matter what any caller sends.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@activity.defn(name=LOCAL_ACTIVITY_UPDATE_STATUS)
async def update_work_item_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Projects the workflow state into ``work_items`` idempotently.

    The model accepts the minimal historical payload (work_item_id/status/
    pr_number) plus optional new fields. Plan/hash/expected_files are derived
    here, outside the workflow's deterministic sandbox. Redelivering the same
    Activity does not bump ``state_version`` when the state did not change.

    THE STATUS WRITE IS UNCONDITIONAL, INCLUDING OUT OF A TERMINAL STATUS, and
    that is not an oversight. A guard that refused to leave
    ``done|failed|blocked|escalated`` was tried here and had to be removed: an
    ordinary retry REUSES the work item row (the requester comments
    ``@dse-bot`` on the same issue of an ``escalated`` item, adapter-github
    promotes it to ``kind=task_request``, and `ingest_gateway.correlate` matches
    the existing row because its own terminal set is only done/failed), so the
    guard froze the projection of the whole retry at ``escalated``. That is not
    cosmetic: `ingest_gateway.dispatcher._route_signal` picks the plan-approval
    signal by reading this very column, so a frozen row made the dispatcher
    DECLINE the human's approval of the retry while the agent kept running.
    Re-escalation of an item this Activity moves back out of ``escalated`` is
    suppressed on the append-only ledger instead — `ingest_gateway.stranded`
    keys both its detection and its escalation on
    ``max(ts) FILTER (WHERE action = 'work_item_escalated_stranded')`` versus the
    newest audit row, which is why it does not depend on the status column
    staying terminal.
    """
    inp = PersistWorkItemStateInput(**payload)
    work_item_id = inp.work_item_id
    plan_json = json.dumps(inp.plan) if inp.plan is not None else None
    expected_files = None
    plan_hash = None
    if inp.plan is not None:
        expected_files = json.dumps(list(inp.plan.get("expected_files") or []))
        canonical_plan = json.dumps(inp.plan, sort_keys=True, separators=(",", ":"))
        plan_hash = hashlib.sha256(canonical_plan.encode("utf-8")).hexdigest()
    attempts_json = (
        json.dumps(inp.validation_attempts) if inp.validation_attempts is not None else None
    )
    # A work item born on Slack/Jira has no repo/base_branch; both are resolved
    # LATER, from the human's clarification answer, and this Activity is the only
    # thing that can put them on the row. They are read off the raw payload and
    # not off `inp` because PersistWorkItemStateInput does not declare them yet
    # and the model DROPS unknown keys silently — going through it would throw
    # the resolved repo away and leave the column NULL forever (which is what the
    # cost-per-repo rollup has been reading as "(unknown)").
    repo = _none_if_blank(payload.get("repo"))
    base_branch = _none_if_blank(payload.get("base_branch"))
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover - only happens without Postgres up
        logger.warning("update_work_item_status: no Postgres connection (%s); skipping persistence", exc)
        return {"persisted": False}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE work_items SET
                    status = COALESCE(%s, status),
                    repo = COALESCE(%s, repo),
                    base_branch = COALESCE(%s, base_branch),
                    pr_number = COALESCE(%s, pr_number),
                    pr_url = COALESCE(%s, pr_url),
                    plan = COALESCE(%s::jsonb, plan),
                    plan_hash = COALESCE(%s, plan_hash),
                    expected_files = COALESCE(%s::jsonb, expected_files),
                    risk_class = COALESCE(%s, risk_class),
                    base_sha = COALESCE(%s, base_sha),
                    head_sha = COALESCE(%s, head_sha),
                    ci_status = CASE
                        WHEN %s THEN NULL
                        ELSE COALESCE(%s, ci_status)
                    END,
                    last_error = COALESCE(%s, last_error),
                    validation_attempts = COALESCE(%s::jsonb, validation_attempts),
                    state_version = state_version + CASE
                        WHEN %s IS NOT NULL AND status IS DISTINCT FROM %s THEN 1
                        ELSE 0
                    END,
                    last_transition_at = CASE
                        WHEN %s IS NOT NULL AND status IS DISTINCT FROM %s THEN now()
                        ELSE last_transition_at
                    END
                WHERE id = %s
                RETURNING status, state_version, plan_hash, base_sha, head_sha, ci_status
                """,
                (
                    inp.status,
                    repo,
                    base_branch,
                    inp.pr_number,
                    inp.pr_url,
                    plan_json,
                    plan_hash,
                    expected_files,
                    inp.risk_class,
                    inp.base_sha,
                    inp.head_sha,
                    inp.clear_ci_status,
                    inp.ci_status,
                    inp.last_error,
                    attempts_json,
                    inp.status,
                    inp.status,
                    inp.status,
                    inp.status,
                    work_item_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        if row is None:
            logger.info(
                "update_work_item_status: work_item_id=%s does not exist in work_items yet (fine in an isolated test)",
                work_item_id,
            )
            return {"persisted": False}
        status, state_version, persisted_plan_hash, base_sha, head_sha, ci_status = row
        return {
            "persisted": True,
            "status": status,
            "state_version": state_version,
            "plan_hash": persisted_plan_hash,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "ci_status": ci_status,
        }
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_LOAD_WORK_ITEM)
async def load_work_item(payload: dict[str, Any]) -> dict[str, Any]:
    """Reads the `work_items` row by id — used ONLY when the workflow is started
    with just the `work_item_id` (string) instead of the full
    `WorkItemLifecycleInput` (see `workflows.py::_coerce_input` and the README,
    section "Assumed start_workflow contract"). WS-A is the one that writes
    the original row in `work_items` before calling `StartWorkflow`."""
    work_item_id = payload["work_item_id"]
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, repo, base_branch, requester, data_class, pr_number, "
                "       risk_class, budget, source_ref, plan, plan_hash, expected_files, "
                "       base_sha, head_sha, pr_url, ci_status, state_version, last_error "
                "FROM work_items WHERE id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"work_item_id={work_item_id!r} not found in work_items")
            # S1 (Phase 5): the task content (issue title+body) lives in the
            # admission event's `ingest_events.payload` (task_request), NOT in
            # work_items. We read it here so the Planner/Coder receive the real
            # task description — before this the agents only got
            # clarification_notes (empty), with no idea what to build.
            cur.execute(
                "SELECT payload FROM ingest_events "
                "WHERE work_item_id = %s AND kind = 'task_request' "
                "ORDER BY id ASC LIMIT 1",
                (work_item_id,),
            )
            ev = cur.fetchone()
        task_content = ""
        if ev and ev[0]:
            p = ev[0]  # JSONB -> dict (serialized ConversationEvent + sanitized_content)
            # the sanitized version is the one that goes to the model
            # (WSA-E2-T3); falls back to the original content_snapshot if absent.
            task_content = (p.get("sanitized_content") or p.get("content_snapshot") or "").strip()
        (
            tenant_id, repo, base_branch, requester, data_class, pr_number,
            risk_class, budget, source_ref, plan, plan_hash, expected_files,
            base_sha, head_sha, pr_url, ci_status, state_version, last_error,
        ) = row
        # S3 (Phase 5): the issue number lives in source_ref (JSONB {repo, number})
        # — required for the outbound to post the status comment on the right issue.
        issue_number = None
        if isinstance(source_ref, dict):
            issue_number = source_ref.get("number") or source_ref.get("issue_number")
        return {
            "work_item_id": work_item_id,
            "tenant_id": tenant_id,
            "repo": repo,
            "base_branch": base_branch,
            "requester": requester,
            "data_class": data_class or "internal",
            "pr_number": pr_number,
            "risk_class": risk_class,
            "plan": plan or {},
            "plan_hash": plan_hash,
            "expected_files": expected_files or [],
            "base_sha": base_sha,
            "head_sha": head_sha,
            "pr_url": pr_url,
            "ci_status": ci_status,
            "state_version": int(state_version or 0),
            "last_error": last_error,
            "task_content": task_content,
            "issue_number": issue_number,
            # WSB-E4-T1: budget read at admission. `budget` is the work_items
            # JSONB (default '{}'). The "max_usd" key is the aggregate ceiling.
            "budget": budget or {},
        }
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_RESOLVE_APPROVER)
async def resolve_plan_approver(payload: dict[str, Any]) -> dict[str, Any]:
    """WSB-E3-T2 — approver resolution cascade: CODEOWNERS -> designated
    approvers from the access bundle (WS-F, `dse_access_bundle`). Returns the
    FIRST non-empty source. An EMPTY cascade returns `[]` — the workflow treats
    that as Blocked + escalation, it NEVER auto-approves on absence (P1/P3).

    The I/O here (DB + CODEOWNERS) is why this is an Activity and not workflow
    code. Offboarded approvers (dse_console_identity.active=false) are filtered
    out once the console identity table exists (WS-F)."""
    tenant_id = payload["tenant_id"]
    repo = payload.get("repo")
    channel = payload.get("channel")

    # --- source 1: CODEOWNERS (swappable reader; production = GitHub adapter) ---
    codeowners: list[str] = []
    reader = policy._codeowners_reader
    if reader is not None:
        try:
            text = reader(tenant_id, repo)
            codeowners = policy.parse_codeowners_owners(text)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("codeowners_reader failed (%s); falling through to the access bundle", exc)
    if codeowners:
        return {"approvers": codeowners, "source": "codeowners"}

    # --- source 2: access bundle (WS-F) — designated_approvers ---
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover - no Postgres
        logger.warning("resolve_plan_approver: no Postgres (%s)", exc)
        return {"approvers": [], "source": "none"}
    try:
        approvers: list[str] = []
        try:
            with conn.cursor() as cur:
                # resolution: channel-specific first, otherwise the tenant default (channel NULL)
                cur.execute(
                    """
                    SELECT designated_approvers
                    FROM dse_access_bundle
                    WHERE tenant_id = %s AND enabled = true
                      AND (channel = %s OR channel IS NULL)
                    ORDER BY (channel IS NOT NULL) DESC
                    LIMIT 1
                    """,
                    (tenant_id, channel),
                )
                row = cur.fetchone()
            if row and row[0]:
                approvers = [str(a) for a in row[0]]
        except Exception as exc:
            # WS-F may not have created the table yet (parallel build) — treat
            # it as an empty source, never as a fatal error of the gate.
            conn.rollback()
            logger.warning("dse_access_bundle unavailable (%s); source treated as empty", exc)
            approvers = []

        # filter out offboarded principals via dse_console_identity.active, if the table exists
        if approvers:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT principal_id FROM dse_console_identity "
                        "WHERE principal_id = ANY(%s) AND active = false",
                        (approvers,),
                    )
                    inactive = {r[0] for r in cur.fetchall()}
                if inactive:
                    approvers = [a for a in approvers if a not in inactive]
            except Exception:
                conn.rollback()  # table missing -> no filter (does not block)
        return {"approvers": approvers, "source": "access_bundle" if approvers else "none"}
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_RECORD_GATE)
async def record_plan_approval(payload: dict[str, Any]) -> dict[str, Any]:
    """WSB-E3-T2/T3 — durable projection of the gate (migration 0009).
    Idempotent upsert by work_item_id. Does NOT replace the audit ledger (the
    workflow also calls emit_audit_event) — this is the mutable projection the
    queue board/operators can query."""
    work_item_id = payload["work_item_id"]
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover
        logger.warning("record_plan_approval: no Postgres (%s); skipping the projection", exc)
        return {"persisted": False}
    try:
        import json

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO plan_approval_gate
                    (work_item_id, tenant_id, risk_class, status, auto_approved,
                     resolved_approvers, decided_by, rejection_route, justification, plan_round)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    risk_class = EXCLUDED.risk_class,
                    status = EXCLUDED.status,
                    auto_approved = EXCLUDED.auto_approved,
                    resolved_approvers = EXCLUDED.resolved_approvers,
                    decided_by = EXCLUDED.decided_by,
                    rejection_route = EXCLUDED.rejection_route,
                    justification = EXCLUDED.justification,
                    plan_round = EXCLUDED.plan_round
                """,
                (
                    work_item_id,
                    payload["tenant_id"],
                    payload["risk_class"],
                    payload["status"],
                    bool(payload.get("auto_approved", False)),
                    json.dumps(payload.get("resolved_approvers", [])),
                    payload.get("decided_by"),
                    payload.get("rejection_route"),
                    payload.get("justification"),
                    int(payload.get("plan_round", 0)),
                ),
            )
        conn.commit()
        return {"persisted": True}
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_RECORD_EVIDENCE)
async def record_evidence_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Phase 3 — durable projection of the evidence pipeline state (migration
    0014, work_item_evidence table). Idempotent upsert by work_item_id. Does NOT
    replace the audit ledger (P8): the workflow emits the evidence events via
    emit_audit_event; this table is the mutable projection the queue board
    (WS-F)/operators can query ("what is the latest preview/video?")."""
    work_item_id = payload["work_item_id"]
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover
        logger.warning("record_evidence_state: no Postgres (%s); skipping the projection", exc)
        return {"persisted": False}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_item_evidence
                    (work_item_id, tenant_id, preview_status, preview_url, demo_passed,
                     video_artifact_key, trace_artifact_key, visual_baseline_key,
                     refresh_count, last_refresh_reason, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    preview_status = EXCLUDED.preview_status,
                    preview_url = EXCLUDED.preview_url,
                    demo_passed = EXCLUDED.demo_passed,
                    video_artifact_key = EXCLUDED.video_artifact_key,
                    trace_artifact_key = EXCLUDED.trace_artifact_key,
                    visual_baseline_key = EXCLUDED.visual_baseline_key,
                    refresh_count = EXCLUDED.refresh_count,
                    last_refresh_reason = EXCLUDED.last_refresh_reason,
                    detail = EXCLUDED.detail
                """,
                (
                    work_item_id,
                    payload["tenant_id"],
                    payload.get("preview_status"),
                    payload.get("preview_url"),
                    payload.get("demo_passed"),
                    payload.get("video_artifact_key"),
                    payload.get("trace_artifact_key"),
                    payload.get("visual_baseline_key"),
                    int(payload.get("refresh_count", 0)),
                    payload.get("last_refresh_reason"),
                    payload.get("detail"),
                ),
            )
        conn.commit()
        return {"persisted": True}
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_PREVIEW_ENABLED)
async def preview_enabled_for_repo(payload: dict[str, Any]) -> dict[str, Any]:
    """Plan 08 §D — does the target repo "produce a preview"? Deterministic gate
    (P1), operator-set via `repo_bindings.deploys_preview` (Repos & ROI panel, §C).

    Semantics (fail-safe, backward compatible):
      - some tenant binding with this `repo` marked `deploys_preview=true`
        → True (the operator declared this repo has previews);
      - the tenant has NO bindings at all (single-repo/unconfigured) → True
        (preserves the previous behavior — always preview, decided solely by
        the paths-filter);
      - the tenant has bindings, but none marks this repo → False (opt-in: the
        operator configured previews and did not include this repo).
    No Postgres/error → True (fail-open for the preview, which never blocks the
    PR: the paths-filter still skips docs/tests, and one extra preview is
    harmless)."""
    tenant_id = payload.get("tenant_id", "")
    repo = payload.get("repo") or ""
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover - fail-open
        logger.warning("preview_enabled_for_repo: no Postgres (%s); assuming enabled", exc)
        return {"enabled": True, "reason": "no_db_fail_open"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE repo = %s AND deploys_preview) "
                "FROM repo_bindings WHERE tenant_id = %s",
                (repo, tenant_id),
            )
            total, marked = cur.fetchone()
        if marked and marked > 0:
            return {"enabled": True, "reason": "binding_marked"}
        if not total:
            return {"enabled": True, "reason": "no_bindings_backward_compat"}
        return {"enabled": False, "reason": "bindings_exist_repo_not_marked"}
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_EMIT_HISTORY_METRIC)
async def emit_history_metric(payload: dict[str, Any]) -> None:
    """Phase 3 — feeds the history alert (ALERTING-RULES.md §3, with WS-F). The
    workflow READS the history size deterministically
    (workflow.info().get_current_history_length()/size()) and this Activity
    EMITS the OTel metric to the collector (I/O outside the sandbox — P1).
    Best-effort: the workflow treats a failure here as non-fatal."""
    from dse_orchestrator import metrics

    metrics.record_history_metric(
        work_item_id=payload["work_item_id"],
        tenant_id=payload["tenant_id"],
        phase=payload.get("phase", "unknown"),
        checkpoint=payload.get("checkpoint", "unknown"),
        history_length=int(payload.get("history_length", 0)),
        history_size_bytes=int(payload.get("history_size_bytes", 0)),
        continue_as_new_count=int(payload.get("continue_as_new_count", 0)),
    )


@activity.defn(name=LOCAL_ACTIVITY_RECORD_SKILL_EPISODE)
async def record_skill_episode(payload: dict[str, Any]) -> dict[str, Any]:
    """Phase 4 (WSC-E4-T2, source=clarification) — writes ONE skill-learning
    episode into skill_episode (migration 0019, table owned by WS-C; WS-B only
    writes the INPUT). NO skill is created/activated here (boundary tested in
    packages/contracts): the episode is merely the governable input that WS-C's
    promotion pipeline consumes. `occurrence_n` is the tenant-wide counter of
    occurrences of the same `pattern_key` (full provenance in JSONB).
    Idempotency: every detected recurrence produces a new row (append-only, like
    the audit ledger) — dedup/promotion triggering belongs to WS-C."""
    tenant_id = payload["tenant_id"]
    pattern_key = payload["pattern_key"]
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover - no Postgres
        logger.warning("record_skill_episode: no Postgres (%s); skipping the learning input", exc)
        return {"persisted": False}
    try:
        import json

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(occurrence_n), 0) FROM skill_episode "
                "WHERE tenant_id = %s AND pattern_key = %s",
                (tenant_id, pattern_key),
            )
            occurrence_n = int((cur.fetchone() or [0])[0] or 0) + 1
            cur.execute(
                """
                INSERT INTO skill_episode
                    (tenant_id, source, work_item_id, pattern_key, occurrence_n, provenance)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    tenant_id,
                    payload.get("source", "clarification"),
                    payload.get("work_item_id"),
                    pattern_key,
                    occurrence_n,
                    json.dumps(payload.get("provenance") or {}),
                ),
            )
            episode_id = cur.fetchone()[0]
        conn.commit()
        return {"persisted": True, "occurrence_n": occurrence_n, "episode_id": episode_id}
    finally:
        conn.close()


def _render_run_digest(p: dict[str, Any]) -> str:
    """The diário entry, rendered from fields the workflow already held.

    Deterministic on purpose. A cheap-model summariser was the obvious design and
    it buys nothing here: everything worth recording — what was planned, which
    files it touched, where it stopped, why — is already structured data at the
    terminal transition. Paying a model to turn structured data into prose, then
    paying again to read that prose back, is cost with no information added. It
    would also give the journal a way to be WRONG about a run that the run itself
    could not be wrong about.

    Kept short by construction: this text is destined for a prompt whose whole
    context budget is 16.000 chars."""
    outcome = p.get("outcome", "unknown")
    lines = [f"[{outcome}] {p.get('title') or p.get('work_item_id')}"]

    files = [f for f in (p.get("expected_files") or []) if f][:6]
    if files:
        lines.append(f"  planned: {', '.join(files)}")

    if p.get("risk_class"):
        lines.append(f"  risk: {p['risk_class']}")

    # The failure detail is the load-bearing part for every non-`done` outcome:
    # "what stopped this last time" is the thing a later run most wants and can
    # least cheaply rediscover.
    detail = (p.get("terminal_detail") or "").strip()
    if detail and outcome != "done":
        lines.append(f"  stopped at: {detail[:180]}")

    fixes = [f for f in (p.get("fix_context") or []) if f][:2]
    for fix in fixes:
        lines.append(f"  had to fix: {str(fix).strip()[:160]}")

    if p.get("plan_rounds"):
        lines.append(f"  re-planned {p['plan_rounds']}x")
    if outcome == "done" and p.get("pr_number"):
        lines.append(f"  merged as #{p['pr_number']}")

    return "\n".join(lines)[:_RUN_DIGEST_MAX_CHARS]


@activity.defn(name=LOCAL_ACTIVITY_RECORD_RUN_EPISODE)
async def record_run_episode(payload: dict[str, Any]) -> dict[str, Any]:
    """The diário de bordo — one row per run that reached a terminal state.

    Distinct from `record_skill_episode` in both table and meaning: that one is a
    candidate for a promotion pipeline and must never carry a failed run, this
    one is a record of what happened and is most useful precisely when the run
    failed. See migrations/0036_wsb_run_episode.sql.

    Best-effort by design — a work item must not fail because its journal entry
    could not be written — but NOT silent: a skipped write is audited, because
    "the diário is quietly empty" is exactly the failure mode this repository has
    already hit three separate times (the Planner's AGENTS.md read, the skills
    note under k8s, repo_map's '(not indexed)')."""
    tenant_id = payload["tenant_id"]
    work_item_id = payload["work_item_id"]
    digest = _render_run_digest(payload)
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover - no Postgres
        logger.warning("record_run_episode: no Postgres (%s); no journal entry for %s", exc, work_item_id)
        return {"persisted": False, "reason": "no_database"}
    try:
        import json

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_episode
                    (tenant_id, repo, work_item_id, outcome, base_sha, risk_class,
                     data_class, digest, provenance)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (work_item_id, outcome) DO UPDATE SET
                    digest = EXCLUDED.digest,
                    base_sha = EXCLUDED.base_sha,
                    risk_class = EXCLUDED.risk_class,
                    data_class = EXCLUDED.data_class,
                    provenance = EXCLUDED.provenance,
                    created_at = now()
                RETURNING id
                """,
                (
                    tenant_id,
                    payload.get("repo"),
                    work_item_id,
                    payload["outcome"],
                    payload.get("base_sha"),
                    payload.get("risk_class"),
                    payload.get("data_class"),
                    digest,
                    json.dumps(
                        {
                            k: payload.get(k)
                            for k in (
                                "expected_files", "terminal_detail", "fix_context",
                                "plan_rounds", "pr_number", "base_branch", "title",
                            )
                            if payload.get(k) is not None
                        }
                    ),
                ),
            )
            row = cur.fetchone()
        conn.commit()
        # Last-writer-wins (migration 0037). DO NOTHING kept the FIRST attempt
        # forever, and a work item can genuinely run twice: a comment on an
        # `escalated` item starts a new execution under the same workflow id, so
        # the second, more relevant account was the one being discarded. A replay
        # re-renders a byte-identical digest, so the UPDATE is a content no-op.
        return {"persisted": True, "episode_id": row[0] if row else None}
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_EMIT_PR_QUALITY_METRIC)
async def emit_pr_quality_metric(payload: dict[str, Any]) -> None:
    """Phase 4 — emits the PR quality OTel metrics (pilot gate). The READ
    (rounds/counts/time) is deterministic in the workflow; the EMISSION happens
    here (I/O outside the sandbox — P1). Best-effort."""
    from dse_orchestrator import metrics

    metrics.record_pr_quality_metric(
        work_item_id=payload["work_item_id"],
        tenant_id=payload["tenant_id"],
        outcome=payload.get("outcome", "unknown"),
        review_rounds=int(payload.get("review_rounds", 0)),
        changes_requested_count=int(payload.get("changes_requested_count", 0)),
        evidence_refreshes=int(payload.get("evidence_refreshes", 0)),
        time_to_merge_seconds=payload.get("time_to_merge_seconds"),
    )


@activity.defn(name=LOCAL_ACTIVITY_CHECK_CLARIFICATION)
async def check_clarification_completeness(payload: dict[str, Any]) -> dict[str, Any]:
    """Simple deterministic checklist per task-class (Phase 1: a single
    "default" task-class). An LLM never decides this (P1)."""
    missing: list[str] = []
    if not payload.get("repo"):
        missing.append("repo")
    if not payload.get("base_branch"):
        missing.append("base_branch")
    # S2 (Phase 5): "what to do" is satisfied by an explicit acceptance
    # criterion OR by a substantial task body (a well-described issue).
    # Deterministic heuristic (P1): >= 40 chars of real content counts as a
    # sufficient description; below that (e.g. "fix the bug") it asks for
    # clarification. An LLM never decides this.
    acceptance = (payload.get("acceptance_criteria") or "").strip()
    task_content = (payload.get("task_content") or "").strip()
    if not acceptance and len(task_content) < 40:
        missing.append("acceptance_criteria")
    return {"complete": not missing, "missing": missing}


@activity.defn(name=ACTIVITY_EMIT_AUDIT)
async def emit_audit_event(payload: dict[str, Any]) -> None:
    """The only bridge between the workflow's deterministic world and the audit
    ledger (P8). Uses `dse_audit.emit` underneath — never writes to audit_log
    directly.
    """
    dse_audit.emit(
        actor=payload["actor"],
        action=payload["action"],
        tenant_id=payload["tenant_id"],
        work_item_id=payload.get("work_item_id"),
        details=payload.get("details") or {},
    )


# Every consequential transition describes the CURRENT STATE on the originating
# surface (cheap-oversight principle — never leave the human in the dark on any
# platform). The generic fallback guarantees that a status without a template
# still produces a comment. Today GitHub; Slack/Jira reuse the same status
# vocabulary via their own outbound adapters.
_STATUS_BODIES = {
    "needs_clarification": "🔎 The DSE needs clarification before it can start:\n\n{detail}",
    "awaiting_plan_approval": "📋 Plan ready — awaiting human approval (risk: {detail}).",
    # DELIBERATELY NO `awaiting_plan_approval_reminder` ENTRY. A reminder for the
    # gate must NOT travel as its own status: adapter-slack attaches the
    # Approve/Reject Block Kit only for the exact string `awaiting_plan_approval`,
    # so any other status re-renders the same mutable message WITHOUT the buttons
    # and the approver is left with no way to answer. The reminder therefore keeps
    # the real status and overrides `body` instead — see
    # `workflows._post_plan_approval_reminder`. Any future "louder" variant of a
    # gate message has to do the same.
    "awaiting_repo_selection": "🔎 Which repository should I use?\n\n{detail}",
    "implementing": "⚙️ The DSE is implementing the change in an isolated sandbox.",
    "validating": "🧪 Implementation ready — running validation (L1/L2) in the sandbox.",
    # `pr_open` (CI still running) and `pr_ready` (ready to review) are distinct
    # states since the `fine-pr-ci-states-v1` patch. Without this entry the human
    # read the raw fallback — "DSE status: pr_open" — at the MOST visible moment
    # of the flow, right after the PR is born.
    "pr_open": "✅ PR opened — CI is running. Nothing to review yet; this message will update.",
    "pr_ready": "✅ PR opened with the change and evidence — ready for human review.",
    "pr_updated": "🔁 PR updated with the review fix — ready for another review.",
    "done": "🎉 Merged by a human. Task completed.",
    "failed": "❌ The task failed and stopped: {detail}",
    "escalated": (
        "⚠️ The DSE escalated this task for human review and stopped.\n\n"
        "**Reason:** {detail}\n\n"
        "Review the description / acceptance criteria and re-apply the `dse` "
        "label to try again."
    ),
    "blocked": (
        "🚧 Blocked awaiting human intervention.\n\n**Reason:** {detail}\n\n"
        "(e.g. no resolvable approver — adjust CODEOWNERS / access bundle.)"
    ),
}


@activity.defn(name=ACTIVITY_POST_TRACKING_COMMENT)
async def post_tracking_comment(payload: dict[str, Any]) -> dict[str, Any]:
    """Posts/edits THE single status comment on the ORIGINATING surface
    (github/slack/jira), via that source's adapter `/internal/status-comment`
    (all of them use the SAME MutableCommentWriter). Auto-resolves the target
    from `work_items.source/source_ref` — call sites only pass work_item_id +
    status (+ optional detail). Deterministic (P1); best-effort (never brings
    the workflow down — the audit ledger is the source of truth).

    C3 (report 07): generalized beyond github. Each source has its own
    adapter and its own correlation field:
      github -> {repo, issue_number}   @ DSE_ADAPTER_GITHUB_URL
      slack  -> {channel}              @ DSE_ADAPTER_SLACK_URL
      jira   -> {ticket_key}           @ DSE_ADAPTER_JIRA_URL
    Unknown source = audited no-op."""
    work_item_id = payload["work_item_id"]
    tenant_id = payload.get("tenant_id", "")
    status = payload.get("status", "")
    detail = str(payload.get("detail") or "")
    body = payload.get("body")

    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source, repo, source_ref FROM work_items WHERE id = %s", (work_item_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "reason": "work_item_not_found"}
    source, repo, source_ref = row
    source_ref = source_ref if isinstance(source_ref, dict) else {}

    target = _resolve_comment_target(source, repo, source_ref)
    if target is None:
        return {"ok": True, "skipped": f"source={source}_no_target"}

    if not body:
        template = _STATUS_BODIES.get(status, "DSE status: {status}")
        body = template.format(detail=detail or "—", status=status)

    adapter_url, extra_fields = target
    # Slack uses `status` to build Block Kit on awaiting_plan_approval (Phase B);
    # github/jira do not have the field (do not send it — their models are strict).
    if source == "slack":
        extra_fields = {**extra_fields, "status": status}
    import httpx
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=2.0)) as client:
            resp = client.post(
                f"{adapter_url}/internal/status-comment",
                json={"work_item_id": work_item_id, "body": body,
                      "actor": "system:orchestrator", **extra_fields},
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - outbound is best-effort; never brings the workflow down
        logging.getLogger("dse_orchestrator").warning(
            "post_tracking_comment failed for %s (%s): %s", work_item_id, source, exc
        )
        return {"ok": False, "reason": "adapter_unavailable", "error": str(exc)[:200]}
    dse_audit.emit(actor="system:orchestrator", action="tracking_comment_posted",
                   tenant_id=tenant_id, work_item_id=work_item_id,
                   details={"source": source, "status": status, **extra_fields})
    return {"ok": True}


def _resolve_comment_target(source, repo, source_ref: dict[str, Any]):
    """(adapter_url, correlation_fields) per source, or None when it cannot be
    addressed (e.g. github without issue_number). URLs are read per call (not at
    import time) so tests can override them via env."""
    if source == "github":
        issue_number = source_ref.get("number") or source_ref.get("issue_number")
        if not repo or not issue_number:
            return None
        url = os.environ.get("DSE_ADAPTER_GITHUB_URL", "http://adapter-github:8802")
        return url, {"repo": repo, "issue_number": int(issue_number)}
    if source == "slack":
        channel = source_ref.get("channel")
        if not channel:
            return None
        url = os.environ.get("DSE_ADAPTER_SLACK_URL", "http://adapter-slack:8801")
        return url, {"channel": channel}
    if source == "jira":
        ticket_key = source_ref.get("ticket_key")
        if not ticket_key:
            return None
        url = os.environ.get("DSE_ADAPTER_JIRA_URL", "http://adapter-jira:8804")
        return url, {"ticket_key": ticket_key}
    return None


# DSE status -> Jira board column map (Phase B). Column names vary per Jira
# project, so it is overridable via env (DSE_JIRA_STATUS_MAP as JSON). Only
# statuses with an entry produce a transition; several DSE statuses collapse
# into one column (e.g. pr_open/ci_pending/review_ready -> "In Review"), and the
# per-column dedup avoids moving the card for nothing.
_DEFAULT_JIRA_STATUS_MAP = {
    "implementing": "In Progress",
    "validating": "In Progress",
    "pr_open": "In Review",
    "ci_pending": "In Review",
    "review_ready": "In Review",
    "pr_ready": "In Review",
    "merge_pending": "In Review",
    "done": "Done",
    "blocked": "Blocked",
    "escalated": "Blocked",
    "failed": "Blocked",
}


def _jira_status_map() -> dict[str, str]:
    raw = os.environ.get("DSE_JIRA_STATUS_MAP")
    if raw:
        try:
            return {**_DEFAULT_JIRA_STATUS_MAP, **json.loads(raw)}
        except Exception:  # noqa: BLE001 - a malformed env var must not break the flow
            logging.getLogger("dse_orchestrator").warning("DSE_JIRA_STATUS_MAP invalid; using the default")
    return _DEFAULT_JIRA_STATUS_MAP


# Default per-WorkItem dollar ceiling. Measured burn on the live cluster
# (audit_log, 2026-07-28): worst single item $11.74 in 9m14s, next $8.16, the
# rest under $1.30 — while review_round_cap permits roughly $60 by design. 25.00
# is ~2x the worst observed run and well under the design ceiling, so it stops a
# runaway without cutting a legitimately expensive task in half.
#
# <= 0 DISABLES the ceiling (explicit operator opt-out, restoring the behaviour
# where nothing denominated in dollars can end a run).
#
# NOTE: spent_usd under-counts — the Planner, L1 and evidence stages report no
# cost_usd — so real spend when this trips is strictly higher than the cap.
_DEFAULT_WORK_ITEM_MAX_USD = 25.0


def _default_work_item_max_usd() -> float | None:
    raw = os.environ.get("DSE_DEFAULT_WORK_ITEM_MAX_USD")
    if raw is None or raw.strip() == "":
        return _DEFAULT_WORK_ITEM_MAX_USD
    try:
        value = float(raw)
    except ValueError:  # a malformed env var must not silently remove the ceiling
        logging.getLogger("dse_orchestrator").warning(
            "DSE_DEFAULT_WORK_ITEM_MAX_USD=%r is not a number; using %.2f", raw, _DEFAULT_WORK_ITEM_MAX_USD
        )
        return _DEFAULT_WORK_ITEM_MAX_USD
    return value if value > 0 else None


@activity.defn(name=LOCAL_ACTIVITY_RESOLVE_BUDGET_CAP)
async def resolve_budget_cap(payload: dict[str, Any]) -> dict[str, Any]:
    """Deployment default for the per-WorkItem ceiling, for items whose
    `work_items.budget` JSONB carries no `max_usd` — which is every item today.

    Pure: no DB, no network, so it cannot stall a boundary on a Postgres blip.
    It exists as an Activity purely so the env read stays outside the workflow
    sandbox and the resolved number is recorded in history."""
    value = _default_work_item_max_usd()
    return {
        "max_usd": value,
        "source": "deployment_default" if value is not None else "disabled",
    }


@activity.defn(name=LOCAL_ACTIVITY_POST_STATUS_TRANSITION)
async def post_status_transition(payload: dict[str, Any]) -> dict[str, Any]:
    """Phase B (report 07): reflects the WorkItem status on the Jira board by
    moving the card to the mapped column — via the adapter-jira
    serialized/idempotent queue (`/internal/transition`). ONLY for source
    `jira`; other sources are an audited no-op. Best-effort; deterministic
    (P1: fixed map).

    dedup_key = work_item_id:column -> the same column is never re-transitioned
    (Jira rejects a no-op transition; and it avoids board noise)."""
    work_item_id = payload["work_item_id"]
    tenant_id = payload.get("tenant_id", "")
    status = payload.get("status", "")

    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source, source_ref FROM work_items WHERE id = %s", (work_item_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "reason": "work_item_not_found"}
    source, source_ref = row
    source_ref = source_ref if isinstance(source_ref, dict) else {}
    if source != "jira":
        return {"ok": True, "skipped": f"source={source}_not_jira"}
    ticket_key = source_ref.get("ticket_key")
    if not ticket_key:
        return {"ok": True, "skipped": "no_ticket_key"}
    target_status = _jira_status_map().get(status)
    if not target_status:
        return {"ok": True, "skipped": f"status={status}_not_mapped"}

    adapter_url = os.environ.get("DSE_ADAPTER_JIRA_URL", "http://adapter-jira:8804")
    import httpx
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=2.0)) as client:
            resp = client.post(
                f"{adapter_url}/internal/transition",
                json={"work_item_id": work_item_id, "ticket_key": ticket_key,
                      "target_status": target_status,
                      "dedup_key": f"{work_item_id}:{target_status}",
                      "actor": "system:orchestrator", "tenant_id": tenant_id},
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - best-effort; never brings the workflow down
        logging.getLogger("dse_orchestrator").warning(
            "post_status_transition failed for %s: %s", work_item_id, exc
        )
        return {"ok": False, "reason": "adapter_unavailable", "error": str(exc)[:200]}
    return {"ok": True, "target_status": target_status}


_ROUTER_PROMPT = """You decide which repositories a change must be MADE IN.

Repositories:
{catalogue}

The request:
{instruction}

Reply with JSON only: {{"repos": ["owner/name", ...], "reason": "one sentence"}}

How to decide. For each repository ask ONE question: does satisfying this
request require EDITING A FILE in that repository?

- Displaying, styling or laying out something the API already returns is a
  frontend edit only. Do not add the backend because the data comes from it.
- Fixing an endpoint, its response, or its behaviour is a backend edit only. Do
  not add the frontend because a user will eventually see the result.
- Add BOTH only when each side needs its own edit: a new or changed field, a new
  endpoint the UI must call, a new parameter the server must honour. If you
  cannot name the edit needed on a side, that side is not included.

Between a needless repository and a missed one, prefer the needless one — but
only when you genuinely cannot tell. Do not use it as a default.
"""


def _route_repos_sync(tenant_id: str, instruction: str) -> dict[str, Any]:
    """Ask the model which repositories a request needs. Never raises.

    The intelligence lives HERE, in the orchestrator, and not in
    `ingest_gateway.repo_resolver` — which stays deterministic and LLM-free, as
    its own docstring requires. Two reasons beyond respecting that rule: the
    Slack events endpoint answers inline against a 3-second acknowledgement
    budget, so a model call there makes Slack retry the webhook; and the gateway
    has no virtual key and no budget path, while every other model call in the
    system already goes through the gateway from here, under the audit ledger.

    Everything the model returns is CLAMPED to the repositories this tenant
    actually has. A hallucinated name cannot become a work item. An empty answer
    is not an error — it falls through to the human repo picker that exists
    today, so the worst case is exactly the current behaviour."""
    import json as _json

    conn = _get_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                TENANT_REPO_CATALOGUE_SQL,
                {"t": tenant_id},
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    candidates = [r[0] for r in rows]
    if len(candidates) < 2:
        # Nothing to route between. Say so rather than spending a model call.
        return {"repos": candidates, "reason": "the tenant has a single repository"}

    catalogue = "\n".join(
        f"- {repo} — {role or 'unknown role'}, {lang or 'unknown stack'}: {desc or 'no description'}"
        for repo, role, lang, desc in rows
    )
    prompt = _ROUTER_PROMPT.format(catalogue=catalogue, instruction=(instruction or "")[:4000])

    try:
        import httpx

        # The names the rest of the system already uses. The first version of
        # this invented `DSE_MODEL_GATEWAY_MASTER_KEY`, which exists nowhere —
        # so the header went out as `Bearer ` and httpx refused it with
        # LocalProtocolError. The router then returned no repositories, every
        # work item fell through to asking a human, and they sat in
        # `clarification_requested` all night. The fallback behaved exactly as
        # designed; the primary path had simply never run once.
        #
        # `sandbox_runtime.model_gateway_client` reads the same variable
        # (`_MASTER_KEY`, model_gateway_client.py:34) and is the reason to check
        # there rather than guess a name.
        base = os.environ.get("DSE_MODEL_GATEWAY_BASE_URL") or os.environ.get(
            "DSE_MODEL_GATEWAY_URL", "http://dse-dse-model-gateway:4000"
        )
        key = os.environ.get("DSE_LITELLM_MASTER_KEY") or os.environ.get(
            "LITELLM_MASTER_KEY", ""
        )
        if not key:
            raise RuntimeError(
                "no gateway master key in the environment "
                "(DSE_LITELLM_MASTER_KEY / LITELLM_MASTER_KEY)"
            )
        # Retried, because giving up here does not fail the item — it parks it.
        # A router that returns nothing falls through to the human repo picker,
        # and in an unattended run there is no human: the work item sits in
        # `awaiting_repo_selection` forever. Measured: one 502 lasting seconds
        # (the gateway answered 200 on the same URL minutes later) stopped an
        # item dead for the rest of the night.
        #
        # Only TRANSPORT and 5xx are retried. A 4xx is a wrong key or a wrong
        # model name — configuration, not weather — and repeating it just burns
        # the clock before the same human picker.
        for attempt in range(_ROUTER_ATTEMPTS):
            try:
                resp = httpx.post(
                    f"{base.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": os.environ.get("DSE_ROUTER_MODEL", "anthropic/claude-haiku"),
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 400,
                    },
                    timeout=30.0,
                )
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"gateway {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status < 500:
                    raise
                if attempt == _ROUTER_ATTEMPTS - 1:
                    raise
                _time.sleep(_ROUTER_BACKOFF_SECONDS * (2 ** attempt))
                logger.warning(
                    "route_repos: gateway attempt %d/%d failed (%s); retrying",
                    attempt + 1, _ROUTER_ATTEMPTS, type(exc).__name__,
                )
        content = resp.json()["choices"][0]["message"]["content"]
        start, end = content.find("{"), content.rfind("}")
        parsed = _json.loads(content[start : end + 1])
        chosen = [r for r in (parsed.get("repos") or []) if r in candidates]
        return {"repos": chosen, "reason": str(parsed.get("reason", ""))[:400]}
    except Exception as exc:  # noqa: BLE001 — a router that raises blocks the item
        logger.warning("route_repos: falling back to the human picker: %s", exc)
        return {"repos": [], "reason": f"router unavailable: {type(exc).__name__}"}


@activity.defn(name=LOCAL_ACTIVITY_ROUTE_REPOS)
async def route_repos(payload: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    return await asyncio.to_thread(
        _route_repos_sync, payload["tenant_id"], payload.get("instruction") or ""
    )


def sibling_work_item_id(event_id: str, repo: str) -> str:
    """The id of the work item that will carry `repo` for this request.

    HASHED, not suffixed, and that is not a style choice. `pod_name_for` is
    `f"dse-sbx-{slug}"[:63]` (k8s_driver.py:138-140) while a work_item_id is
    already 67 characters — so the last twelve are ALREADY discarded today.
    `wi_<sha>__fe` and `wi_<sha>__be` would truncate to the same Pod name, and
    two sandboxes would fight over one Pod. Three other call sites truncate the
    same way: the preview namespace (`argocd.py:79`), its labels (`:175`), and
    the preview image tag (`pr_image.py:133`).

    Hashing `event_id:repo` keeps the exact shape and length of a normal id and
    diverges at character 4, so every one of those truncations stays distinct.
    It is also deterministic, which is what makes the fan-out safe to retry:
    the same request always derives the same sibling ids, so the UNIQUE
    constraints on `work_items.idempotency_key` and `ingest_events.event_id`
    turn a replay into a no-op instead of a duplicate."""
    return "wi_" + hashlib.sha256(f"{event_id}:{repo}".encode()).hexdigest()


@activity.defn(name=LOCAL_ACTIVITY_FAN_OUT_SIBLINGS)
async def fan_out_sibling_work_items(payload: dict[str, Any]) -> dict[str, Any]:
    """Create one sibling work item per extra repository, in the outbox.

    Deliberately NOT a Temporal child workflow. `dispatcher._dispatch_row`
    already turns (a `work_items` row + an `ingest_events` row of kind
    `task_request`) into a started workflow, idempotently — so writing those two
    rows buys a top-level workflow with the outbox's crash-safety for free. A
    child workflow would drag ParentClosePolicy into a parent that calls
    `continue_as_new` at every phase boundary, for no gain.

    The siblings share `source_ref` with the primary, so a human replying in the
    same Slack thread still reaches the conversation. They share `group_id` so
    the surface can render them as one thing."""
    primary = payload["work_item_id"]
    repos = [r for r in (payload.get("repos") or []) if r]
    # F3 (wi_6e5c25bf): o base_branch RESOLVIDO vem do estado do workflow — a
    # linha do primário ainda está NULL neste instante (o default `or "main"`
    # do roteamento só chega ao banco depois), e o irmão que nascia vazio
    # pulava o único branch onde o default mora e pedia a clarificação que o
    # binding do canal já respondia.
    resolved_base_branch = payload.get("base_branch") or None
    if not repos:
        return {"created": [], "group_id": primary}

    created: list[str] = []
    conn = _get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT idempotency_key FROM work_items WHERE id = %s", (primary,)
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(f"primary work item {primary!r} not found")
                event_id = row[0]

                # The primary joins its own group, so COALESCE(group_id, id)
                # names the group for every member without a special case.
                cur.execute(
                    "UPDATE work_items SET group_id = %s WHERE id = %s", (primary, primary)
                )

                for repo in repos:
                    sib = sibling_work_item_id(event_id, repo)
                    cur.execute(
                        """
                        INSERT INTO work_items (
                            id, tenant_id, source, source_ref, repo, base_branch,
                            requester, data_class, task_class, idempotency_key,
                            group_id, status
                        )
                        SELECT %s, tenant_id, source, source_ref, %s,
                               COALESCE(%s, base_branch),
                               requester, data_class, task_class, %s, %s, 'new'
                          FROM work_items WHERE id = %s
                        ON CONFLICT (idempotency_key) DO NOTHING
                        """,
                        (sib, repo, resolved_base_branch, sib, primary, primary),
                    )
                    # The payload is copied verbatim so the sibling's
                    # `load_work_item` reads the same task_content the primary
                    # did — the request was one sentence, not two.
                    cur.execute(
                        """
                        INSERT INTO ingest_events (work_item_id, event_id, kind, payload)
                        SELECT %s, %s, 'task_request', payload
                          FROM ingest_events
                         WHERE work_item_id = %s AND kind = 'task_request'
                         ORDER BY id ASC LIMIT 1
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        (sib, sib, primary),
                    )
                    created.append(sib)
    finally:
        conn.close()
    logger.info("fan-out: %d sibling work item(s) for group %s", len(created), primary)
    return {"created": created, "group_id": primary}


LOCAL_ACTIVITIES = [
    update_work_item_status,
    post_status_transition,
    check_clarification_completeness,
    emit_audit_event,
    load_work_item,
    resolve_plan_approver,
    record_plan_approval,
    record_evidence_state,
    emit_history_metric,
    record_skill_episode,
    record_run_episode,
    emit_pr_quality_metric,
    post_tracking_comment,
    preview_enabled_for_repo,
    resolve_budget_cap,
    fan_out_sibling_work_items,
    route_repos,
]
