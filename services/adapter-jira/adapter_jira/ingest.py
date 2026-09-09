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

import logging
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
    generation: int = 0,
) -> dict:
    """Issue with the trigger label -> task_request (Path A) or an idempotent
    signal if there is already an active WorkItem for the ticket. Mirror of
    adapter-github's `_handle_task_creating_event`.

    `generation` comes from `trigger_state.observe` and counts how many times a
    human took the label off and put it back. It reaches the event_id, which is
    what lets the same card be worked more than once.
    """
    ev = events.build_task_event(
        issue,
        actor_account_id=actor_account_id,
        resolved_principal=resolved_principal,
        display_name=display_name,
        generation=generation,
    )
    sanitized = sanitize_content(ev.content_snapshot)
    channel = events.project_key(issue)
    conn = get_connection()
    try:
        # The sweep re-reads a labelled card every minute. Without this the path
        # ran the whole repo-resolution cascade and emitted a ledger row on each
        # pass — 13 `work_item_admitted` rows in twelve minutes on BFA-1132,
        # none of them followed by a dispatch. `ingest_comment` has had this
        # guard all along; this one did not.
        already = recorded_work_item_id(conn, ev.event_id)
        if already is not None:
            return {
                "ok": True,
                "path": "already_ingested",
                "work_item_id": already,
                "generation": generation,
            }

        result = correlate(
            conn,
            tenant_id=tenant_id,
            event=ev,
            requester_principal=resolved_principal,
            terminal_statuses=_RESTARTABLE_STATUSES,
        )


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


logger = logging.getLogger("adapter_jira.ingest")


#: The five states a card can be in where "the DSE is done with this" is true
#: and a human asking again is the only thing that can happen next. Wider than
#: the conversational rule in `correlate` (done/failed/cancelled) on purpose:
#: replying in the thread of an `escalated` item is a comment on work that still
#: has a human owner, while taking the `dse` label off a card and putting it
#: back is that owner asking, with their hands, for another attempt. `done` is
#: in: the card came back, and only a human could have brought it.
_RESTARTABLE_STATUSES = ("done", "failed", "cancelled", "escalated", "blocked")


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
