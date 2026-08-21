"""Ingestion core of the Jira adapter, SHARED between the webhook (`app.py`)
and the fallback poller (`poller.py`) — WSA-E5-T1/T2.

Both paths call exactly these functions, which build the same
`ConversationEvent` (with `message_id` derived from the issue state, see
`events.py`) and go through the same idempotent `ingest_gateway` path
(`admit_work_item`/`record_signal_event`, dedup by `event_id`). That is what
guarantees "webhook + poller never duplicate": the two produce the same
`event_id`, and whichever arrives second dedupes.

Transaction: each function opens its own connection and delegates the commit to
`admit_work_item`/`record_signal_event` (which commit when they are handed a
`conn`), the same convention as the adapter-github handlers.
"""
from __future__ import annotations

import json
from typing import Any

from dse_audit import emit as audit_emit
from ingest_gateway import (
    AdmissionBlocked,
    NonTaskAdmissionRefused,
    admit_work_item,
    recorded_work_item_id,
    classify_task_class,
    correlate,
    get_connection,
    is_channel_killed,
    record_signal_event,
    resolve_repo,
    sanitize_content,
)

from . import events
from .comment_store import SURFACE as _SURFACE


def ingest_task_trigger(
    issue: dict[str, Any],
    *,
    tenant_id: str,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
) -> dict:
    """Issue with the trigger label -> task_request (Path A) or an idempotent
    signal if there is already an active WorkItem for the ticket. Mirror of
    adapter-github's `_handle_task_creating_event`."""
    ev = events.build_task_event(
        issue, actor_account_id=actor_account_id, resolved_principal=resolved_principal, display_name=display_name
    )
    sanitized = sanitize_content(ev.content_snapshot)
    channel = events.project_key(issue)
    conn = get_connection()
    try:
        result = correlate(conn, tenant_id=tenant_id, event=ev, requester_principal=resolved_principal)


        if result.kind == "signal":
            record_signal_event(
                ev,
                tenant_id=tenant_id,
                channel=channel,
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        # C2 (report 07): resolves the repo through the cascade — explicit
        # override in the text → Component (finest) → Project → tenant default.
        # With no resolution, repo=None and the clarification gate asks (it
        # never guesses).
        repo, base_branch, repo_candidates = resolve_repo(
            conn, tenant_id=tenant_id, platform="jira",
            signals={"text": sanitized, "component": events.first_component(issue),
                     "project": channel},
        )
        try:
            work_item_id = admit_work_item(
                ev,
                tenant_id=tenant_id,
                source="jira",
                channel=channel,
                repo=repo,
                base_branch=base_branch,
                repo_candidates=repo_candidates,
                requester_principal=resolved_principal,
                # Plan 08 §A: deterministic task_class at intake — ticket labels
                # + Jira issue type (Bug→bug_fix, Story→feature_small…).
                task_class=classify_task_class(
                    labels=events.issue_labels(issue),
                    issue_type=events.issue_type(issue),
                ),
                sanitized_content=sanitized,
                conn=conn,
            )
        except NonTaskAdmissionRefused as refusal:
            # F2: comment de ticket (Jira trata todo comment como
            # clarification_answer) sem correlacao nunca vira tarefa.
            # Equivalente Jira da conversa: o TICKET (comment-chain).
            audit_emit(
                actor="system:adapter-jira",
                action="non_task_admission_refused",
                tenant_id=tenant_id,
                details={"kind": refusal.kind, "channel": channel,
                         "event_id": ev.event_id},
                conn=conn,
            )
            conn.commit()
            return {"ok": True, "path": "refused_non_task"}
        except AdmissionBlocked:
            return {"ok": True, "path": "blocked_kill_switch"}

        if result.provenance_work_item_id:
            audit_emit(
                actor=resolved_principal,
                action="work_item_provenance_link",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"previous_work_item_id": result.provenance_work_item_id},
            )
        return {"ok": True, "path": "new_task", "work_item_id": work_item_id}
    finally:
        conn.close()


# Statuses a retry label acts on: the three TERMINAL ones that are not a success.
# Nothing is running for any of them, so a fresh attempt cannot race a workflow.
#   - `failed`: the attempt died on its own (retry cap, activity error).
#   - `blocked`: stopped waiting on human intervention — the status comment says
#     so in as many words ("no resolvable approver — adjust CODEOWNERS"). Once the
#     human has done that, a fresh attempt is the only thing they can want, and
#     the DSE offers them no other lever.
#   - `escalated`: the escalation comment literally tells the human to "re-apply
#     the `dse` label to try again" (orchestrator local_activities.py), which does
#     nothing at all — that label converges on the `created:{issue id}` event_id
#     of the attempt that escalated. This label is what makes the instruction true.
# Deliberately excluded:
#   - `done`: the work merged. A second attempt would re-implement and re-open a
#     PR for a change that already shipped.
#   - every in-flight status: two workflows would race for the same branch and PR.
# The retry is not a bypass: the new work item goes through the whole pipeline,
# clarification and plan-approval gates included.
_RETRY_ELIGIBLE_STATUSES = ("failed", "blocked", "escalated")

# Actor of everything below. The poller reads the issue's CURRENT state, not its
# changelog, so it cannot know WHO added the label — naming the ticket reporter
# would blame someone who may never have asked, in a table nothing can correct
# (migrations/0028, append-only). Same convention as the poller's reconstructed
# approvals; see poller.py's module docstring on attribution. The reporter is
# still the work item's REQUESTER (BD-39: they must be able to answer its
# clarifications), which `work_item_admitted` records.
_RECONSTRUCTED_ACTOR = "system:adapter-jira-poller"

_RETRY_DECLINED_ACTION = "jira_retry_declined"


def _latest_work_item_for_ticket(conn, *, tenant_id: str, ticket_key: str) -> tuple[str, str, str | None, str | None] | None:
    """`(work_item_id, status, repo, base_branch)` of the newest work item for
    this ticket, or None if the DSE never took the ticket on.

    Same lookup shape as `ingest_gateway.correlate` (match on `source_ref`,
    newest first) — deliberately, so "which item does this ticket mean" has one
    answer across the adapter. `correlate` cannot be reused here: it collapses
    `done` and `failed` into one "terminal" verdict, and the retry has to tell
    those apart.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, repo, base_branch FROM work_items
            WHERE tenant_id = %s AND source_ref @> %s::jsonb
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tenant_id, json.dumps({"ticket_key": ticket_key})),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], row[1], row[2], row[3]


def _decline(
    conn,
    *,
    tenant_id: str,
    ticket_key: str,
    reason: str,
    work_item_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Answers the human ONCE for a retry label that will not be acted on.

    Returns True when this call is the one that wrote the row — the poller uses
    that to decide whether to touch Jira at all, so the answer and the label
    removal happen on the same sweep and never again.

    Idempotency comes from the ledger itself: one row per (ticket, reason, work
    item), ever. That has to be checked BEFORE emitting, because the poller
    re-evaluates a labelled ticket on every sweep and `audit_log` is append-only
    (migrations/0028) — a timer writing "not eligible" once a minute is exactly
    how one stuck item produced ~2,900 unremovable rows. The ceiling is a handful
    of rows for the rest of the ticket's life: three reasons, and a ticket has at
    most two work items (the original attempt and its one retry).

    The query touches one partition only (`tenant_id` is the LIST partition key)
    and rides `idx_audit_log_work_item` — hence the `work_item_id` predicate,
    spelled out rather than folded into a NULL-safe comparison, which Postgres
    cannot answer from that index. Without it this is a partition scan repeated
    every sweep for as long as a label the DSE cannot remove sits on a ticket.
    """
    item_predicate = "work_item_id IS NULL" if work_item_id is None else "work_item_id = %s"
    params: tuple = (tenant_id, _RETRY_DECLINED_ACTION, ticket_key, reason)
    if work_item_id is not None:
        params += (work_item_id,)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT 1 FROM audit_log
            WHERE tenant_id = %s AND action = %s
              AND details->>'ticket_key' = %s AND details->>'reason' = %s
              AND {item_predicate}
            LIMIT 1
            """,
            params,
        )
        if cur.fetchone() is not None:
            return False

    audit_emit(
        actor=_RECONSTRUCTED_ACTOR,
        action=_RETRY_DECLINED_ACTION,
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        details={"ticket_key": ticket_key, "reason": reason, **(extra or {})},
        conn=conn,
    )
    conn.commit()
    return True


def ingest_retry_trigger(
    issue: dict[str, Any],
    *,
    tenant_id: str,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
) -> dict:
    """Retry label -> a fresh attempt at a ticket whose work item ended badly.

    Until this existed, a `failed` work item could only be retried by editing the
    database by hand: the ticket already has a work item, so re-adding the
    trigger label converges on the same `event_id` and does nothing.

    ONE RETRY PER TICKET, EVER
    --------------------------
    The ceiling is enforced by `ingest_events.event_id` (UNIQUE, written inside
    `admit_work_item`'s transaction — see `events.build_retry_event`), so it holds
    without any cooperation from Jira: a label that cannot be removed, a restart,
    a 429 on every write, none of them can buy a second attempt.

    That ceiling is also the right answer on its own terms. If a human's retry
    fails as well, a third automated attempt is not what the situation needs —
    the ticket needs editing, or the failure needs a person. What the human must
    not get is silence, so every path that will not retry writes ONE row saying
    why (`_decline`).

    Returns `path="retried"` only when a new attempt was actually admitted, and
    `recorded=True` on the single sweep that wrote a decision — the poller keys
    its Jira writes off both, so no branch here can turn into a per-minute
    side effect.
    """
    conn = get_connection()
    try:
        ticket = events.ticket_key(issue)
        channel = events.project_key(issue)
        ev = events.build_retry_event(
            issue,
            actor_account_id=actor_account_id,
            resolved_principal=resolved_principal,
            display_name=display_name,
        )

        # THE guard, checked first because it is both the cheapest and the only
        # one that cannot be undone by a failure outside Postgres.
        already = recorded_work_item_id(conn, ev.event_id)
        if already is not None:
            return {
                "ok": True,
                "path": "already_retried",
                "work_item_id": already,
                "recorded": _decline(
                    conn,
                    tenant_id=tenant_id,
                    ticket_key=ticket,
                    reason="retry_already_used",
                    work_item_id=already,
                ),
            }

        latest = _latest_work_item_for_ticket(conn, tenant_id=tenant_id, ticket_key=ticket)
        if latest is None:
            return {
                "ok": True,
                "path": "no_work_item",
                "recorded": _decline(
                    conn, tenant_id=tenant_id, ticket_key=ticket, reason="no_work_item"
                ),
            }

        prior_id, prior_status, prior_repo, prior_base_branch = latest
        if prior_status not in _RETRY_ELIGIBLE_STATUSES:
            return {
                "ok": True,
                "path": "not_retryable",
                "work_item_id": prior_id,
                "status": prior_status,
                "recorded": _decline(
                    conn,
                    tenant_id=tenant_id,
                    ticket_key=ticket,
                    reason="status_not_retryable",
                    work_item_id=prior_id,
                    extra={"status": prior_status, "retryable_statuses": list(_RETRY_ELIGIBLE_STATUSES)},
                ),
            }

        # Checked here rather than left to `admit_work_item`, which audits every
        # blocked admission: on a paused channel that would be one row per sweep
        # for as long as the label sits there. A pause is temporary and the label
        # is not consumed — the retry simply happens once the operator resumes.
        killed, _reason = is_channel_killed(conn, tenant_id, channel)
        if killed:
            return {"ok": True, "path": "blocked_kill_switch", "recorded": False}

        sanitized = sanitize_content(ev.content_snapshot)
        # Inherit the failed attempt's repo instead of re-resolving: on a ticket
        # whose repo came from a human answering the clarification, the cascade
        # alone would land back on "ambiguous" and ask the same question again.
        repo, base_branch = prior_repo, prior_base_branch
        # Inheriting a repo means there is nothing left to route, so there is no
        # scope to carry either — the empty list is the honest value, and
        # binding it here keeps the name defined on both branches.
        repo_candidates: list[str] = []
        if not repo:
            repo, base_branch, repo_candidates = resolve_repo(
                conn, tenant_id=tenant_id, platform="jira",
                signals={"text": sanitized,
                         "component": events.first_component(issue),
                         "project": channel},
            )
        try:
            work_item_id = admit_work_item(
                ev,
                tenant_id=tenant_id,
                source="jira",
                channel=channel,
                repo=repo,
                base_branch=base_branch,
                repo_candidates=repo_candidates,
                requester_principal=resolved_principal,
                task_class=classify_task_class(
                    labels=events.issue_labels(issue),
                    issue_type=events.issue_type(issue),
                ),
                sanitized_content=sanitized,
                conn=conn,
            )
        except NonTaskAdmissionRefused as refusal:
            # F2: comment de ticket (Jira trata todo comment como
            # clarification_answer) sem correlacao nunca vira tarefa.
            # Equivalente Jira da conversa: o TICKET (comment-chain).
            audit_emit(
                actor="system:adapter-jira",
                action="non_task_admission_refused",
                tenant_id=tenant_id,
                details={"kind": refusal.kind, "channel": channel,
                         "event_id": ev.event_id},
                conn=conn,
            )
            conn.commit()
            return {"ok": True, "path": "refused_non_task"}
        except AdmissionBlocked:
            # Kill switch flipped between the check above and the insert. The
            # gateway already audited it; nothing to add.
            return {"ok": True, "path": "blocked_kill_switch", "recorded": False}

        # One row for one human action. `previous_work_item_id` is the same key
        # the `work_item_provenance_link` rows use, so the chain from the failed
        # attempt to this one stays queryable by the established convention
        # without emitting a second row for the same fact.
        #
        # `conn=conn` is not optional. Without it this opens a SECOND connection
        # after `admit_work_item` has already committed, and a failure there
        # (pool exhausted, connection reaped) leaves the retry running with no
        # ledger entry naming it a retry at all. `admit_work_item` owns its own
        # commit, so this row is a second transaction on the same connection —
        # the narrowest boundary available without changing the gateway. If the
        # process dies in that window the retry still cannot repeat (the
        # `ingest_events` row is committed) and `work_item_admitted` still
        # records the new item; only the "why" would be missing.
        audit_emit(
            actor=_RECONSTRUCTED_ACTOR,
            action="jira_retry_admitted",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={
                "reason": "retry_label",
                "previous_work_item_id": prior_id,
                "previous_status": prior_status,
                "ticket_key": ticket,
                # The inherited requester (BD-39), NOT whoever added the label —
                # the poller cannot know that from current state.
                "requester": resolved_principal,
            },
            conn=conn,
        )
        conn.commit()
        return {
            "ok": True,
            "path": "retried",
            "recorded": True,
            "work_item_id": work_item_id,
            "previous_work_item_id": prior_id,
        }
    finally:
        conn.close()


def _is_dse_authored(ticket_key: str, comment_id: str) -> bool:
    """True when this comment is the one the DSE itself wrote on the ticket.

    `comment_state` holds the ref the MutableCommentWriter created — exactly one
    status comment per work item, edited in place — so its id is the DSE's own
    signature. Comparing ids is precise where comparing authors is not: with a
    personal API token the bot and the human share an account.

    Best-effort: a database hiccup returns False, which restores the old
    (looping) behaviour rather than dropping a human's answer. Of the two
    failure modes, silently swallowing the reply is the worse one.
    """
    try:
        conn = get_connection()
    except Exception:  # noqa: BLE001 — never block ingestion on this lookup
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM comment_state "
                "WHERE surface = %s "
                "AND (comment_ref::jsonb->>'comment_id') = %s "
                "AND (comment_ref::jsonb->>'ticket_key') = %s "
                "LIMIT 1",
                (_SURFACE, str(comment_id), ticket_key),
            )
            return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 — malformed ref must not stall the flow
        return False
    finally:
        conn.close()


def ingest_comment(
    *,
    tenant_id: str,
    key: str,
    comment_id: str,
    body: str,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
    self_account_id: str | None = None,
) -> dict:
    """Comment on the issue -> signal (clarification) for an active WorkItem;
    with no active WorkItem, ignored (a comment on an issue that is not a DSE
    task).

    Breaks a FEEDBACK LOOP without ever silencing a human. The DSE posts its
    status and clarification comments through the Jira REST API, and Jira
    attributes them to whichever account owns the API token — so unlike Slack,
    where the bot's own messages carry a `bot_id`, a DSE comment is
    indistinguishable from a person's.

    Observed on BD-40: the DSE asked "I need acceptance criteria", the poller
    read that very comment back as the human's ANSWER, the criteria became the
    question itself, the Coder changed nothing, the Tester correctly failed a
    test asserting the change, and the run died at the retry cap.

    Filtering by AUTHOR looks like the fix and is a trap: when the token belongs
    to a real person — the normal setup — the bot and that person are the same
    account, so an author filter blocks their answers too and the task can never
    be unblocked by the one human who cares about it. Observed on BD-41.

    So the test is IDENTITY, not authorship: the writer records the id of the
    comment it created (`comment_state.comment_ref`), so the DSE can recognise
    its own words exactly, with no guessing and no collateral damage. Works the
    same whether the token belongs to a person or to a dedicated bot account.
    """
    if _is_dse_authored(key, comment_id):
        return {"ok": True, "path": "ignored_self_authored"}
    ev = events.build_comment_event(
        key=key,
        comment_id=comment_id,
        body=body,
        actor_account_id=actor_account_id,
        resolved_principal=resolved_principal,
        display_name=display_name,
    )
    sanitized = sanitize_content(ev.content_snapshot)
    conn = get_connection()
    try:
        # The pending-reply recovery re-reads whole threads on every sweep, so
        # on a task that is genuinely waiting it meets the same comments over
        # and over. Recording dedupes on `event_id`, but only after correlating
        # and auditing — which is what turned one stuck ticket into thousands of
        # `signal_duplicate_ignored` rows. Nothing below can change an outcome
        # that was already reached, so stop here.
        prior = recorded_work_item_id(conn, ev.event_id)
        if prior is not None:
            return {"ok": True, "path": "already_ingested", "work_item_id": prior}

        result = correlate(conn, tenant_id=tenant_id, event=ev, requester_principal=resolved_principal)


        if result.kind == "signal":
            record_signal_event(
                ev,
                tenant_id=tenant_id,
                channel=events.project_key({"key": key}),
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        conn.rollback()
        audit_emit(
            actor=resolved_principal,
            action="jira_comment_ignored_no_active_work_item",
            tenant_id=tenant_id,
            details={"ticket_key": key, "comment_id": comment_id},
        )
        return {"ok": True, "path": "ignored_no_active_work_item"}
    finally:
        conn.close()


def ingest_status_approval(
    issue: dict[str, Any],
    *,
    tenant_id: str,
    target_status: str,
    verdict: str,
    route: str | None,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
) -> dict:
    """Transition into the configured approval/rejection column -> kind=
    approval (UC5). Marks `approval_verdict`/`approval_route` on the payload
    (deterministic markers read by the dispatcher in WSA-E6-T3). With no active
    WorkItem for the ticket, ignored."""
    ev = events.build_status_approval_event(
        issue,
        target_status=target_status,
        actor_account_id=actor_account_id,
        resolved_principal=resolved_principal,
        display_name=display_name,
    )
    extra = {"approval_verdict": verdict}
    if route:
        extra["approval_route"] = route
    conn = get_connection()
    try:
        result = correlate(conn, tenant_id=tenant_id, event=ev, requester_principal=resolved_principal)

        if result.kind == "signal":
            record_signal_event(
                ev,
                tenant_id=tenant_id,
                channel=events.project_key(issue),
                work_item_id=result.work_item_id,
                sanitized_content=sanitize_content(ev.content_snapshot),
                extra_payload=extra,
                conn=conn,
            )
            return {"ok": True, "path": "signal_approval", "work_item_id": result.work_item_id, "verdict": verdict}

        conn.rollback()
        audit_emit(
            actor=resolved_principal,
            action="jira_status_transition_ignored_no_active_work_item",
            tenant_id=tenant_id,
            details={"ticket_key": events.ticket_key(issue), "target_status": target_status},
        )
        return {"ok": True, "path": "ignored_no_active_work_item"}
    finally:
        conn.close()
