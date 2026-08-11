"""Detection of work items left behind by a workflow that no longer exists.

The layer above `reconcile.py`. That module recovers a lost human REPLY: the
task is fine, the message never arrived. Here the task itself is gone — four
work items were found sitting in `implementing` (x2), `queued` and `new` since
two days earlier, with no audit event for ~40 hours and no workflow in Temporal:
not open, and not closed either, because namespace retention is 24h and the
history had already been purged. Nothing in the codebase noticed. Nothing would
have, ever — every component waits for an event that no longer has a producer.

WHY THE SYMPTOM IS AUDIT SILENCE, NOT TEMPORAL
----------------------------------------------
"DescribeWorkflowExecution says NOT_FOUND" cannot be the test. After 24h a
workflow that completed perfectly and one that never existed answer NOT_FOUND
identically, so a Temporal probe alone would report finished work as stranded.
What actually distinguishes a live task from an abandoned one is that a live
task keeps writing audit rows: every transition, every activity decision, every
signal lands in the ledger. Silence for hours on a non-terminal status is the
observable fact, and it is the one this module queries.

WHY DETECTION AND ACTION ARE TWO FUNCTIONS
------------------------------------------
`stranded_work_items` only reads. It never restarts anything, and it writes
nothing at all — not even an audit row. A workflow whose history is gone cannot
be resumed safely: re-running it can repeat a coder turn on a branch that
already has one, or reopen a PR that a human closed on purpose. The engine has
no way to tell which side of that line a given item is on, so the decision does
not belong here.

`escalate_stranded` is the only action offered, and `escalated` is terminal on
purpose: it means "handed to a human", not "retried". It is the one honest move
available when the durable state of a task no longer exists.

WHAT THIS MODULE STILL CANNOT SEE
---------------------------------
An operator `pause` signal parks the workflow on `_boundary_gate`'s untimed
`wait_condition` and is recorded ONLY in workflow state (no `work_items` column,
no audit row — see `workflows._log_operator`). A paused item therefore looks
exactly like a stranded one from SQL, and no status excludes it. The mitigation
belongs to whatever wires the sweep: an OPEN Temporal execution is unambiguous
proof of life (it is only NOT_FOUND that is ambiguous after retention), so the
caller must probe before it escalates. Detection is read-only precisely so that
this check can sit between the two halves.
"""
from __future__ import annotations

from typing import Any

from dse_audit import emit as audit_emit

# Lifecycle is over: no next step may ever be guessed for these.
#
# Scoped name on purpose. `correlate._TERMINAL_STATUSES` is a DIFFERENT set
# (done/failed) answering a different question — "can this still receive a
# signal?", where blocked/escalated legitimately can. A bare `TERMINAL_STATUSES`
# exported from this package next to that one is an invitation to import the
# wrong answer; the prefix says which question this tuple settles ("is anybody
# still supposed to be working on it?").
STRANDED_TERMINAL_STATUSES = ("done", "failed", "blocked", "escalated")

# Non-terminal, and audit-SILENT BY DESIGN: a live workflow is parked here on a
# `workflow.wait_condition` waiting for a person, and writes nothing to the
# ledger while it waits. Silence is therefore not evidence of a lost workflow —
# it is the specified behaviour, and the clock would only be measuring how long
# the human has taken.
#
# The first version of this tuple listed only the three intake waits and the
# sweep would have ESCALATED every healthy open PR in the platform: the review
# park (`workflows.py`, `_set_status(review_ready, audit_action=
# "awaiting_human_review")` followed by an untimed `wait_condition`) and the
# merge park (`_set_status(merge_pending, audit_action=
# "approved_awaiting_merge")` then `wait_condition(lambda: self._merged ...)`)
# both write ONE row and then go quiet for as long as the reviewer takes. A
# weekend-long review is the single most normal state this machine has.
#
# `pr_ready` is on the list for a reason that is easy to miss: it is the
# pre-patch alias of THREE states. Executions that started before
# `fine-pr-ci-states-v1` / `fine-pr-open-state-v1` use `pr_ready` where new ones
# use `review_ready`, `merge_pending` and `pr_open`, and such executions are in
# flight right now. Two of those three are unbounded human parks, so `pr_ready`
# has to be treated as one. The cost is a real blind spot: a legacy execution
# that dies while its status happens to read `pr_ready` will not be detected.
# That is the correct trade — a missed detection is a task a human can still
# find, while a false escalation moves live work to a terminal status.
STRANDED_HUMAN_WAIT_STATUSES = (
    # intake: clarification / repo choice / plan approval gates
    "needs_clarification",
    "awaiting_repo_selection",
    "awaiting_plan_approval",
    # post-PR: human review and human merge
    "review_ready",
    "merge_pending",
    "pr_ready",
)

# Everything a sweep must leave alone, whether it is detecting or acting.
#
# Deliberately NOT excluded, even though they are also "waiting": `pr_open`,
# `ci_pending` and `review_feedback`. Nobody is waiting on a human there — the
# workflow itself owes the next step (evidence pipeline, a CI poll every
# `ci_poll_interval_seconds` writing `ci_status_observed`, a fix cycle), so
# silence on those IS the symptom.
_EXCLUDED_STATUSES = STRANDED_TERMINAL_STATUSES + STRANDED_HUMAN_WAIT_STATUSES

#: Single action name for the escalation row, so an operator can grep the ledger
#: for exactly this cause and the console can special-case it.
STRANDED_ESCALATION_ACTION = "work_item_escalated_stranded"

#: Recorded in `details.reason`. Names the cause the way an on-call reader needs
#: it: not "timed out" (nothing timed out) but "there is no longer a workflow".
STRANDED_ESCALATION_REASON = "no_live_workflow"


def stranded_work_items(
    conn, *, tenant_id: str, idle_for_seconds: int, limit: int
) -> list[dict[str, Any]]:
    """Work items that are supposed to be progressing but have gone silent.

    A row is returned when ALL of these hold:
      - its status is neither terminal nor a legitimate human wait (see the two
        tuples above), so somebody — the orchestrator — owes it a next step;
      - nothing has been written to `audit_log` about it for at least
        `idle_for_seconds`;
      - this sweep's own escalation row is not the newest thing in its ledger.

    WHY THE LAST CONDITION EXISTS. A status guard alone makes "escalated at most
    once" true only for as long as the item stays `escalated`, and `escalated` is
    a value in a mutable column. Any write that puts the item back into a
    non-terminal status — with no audit row, which is the normal case for a bare
    status projection — leaves it silent, detectable, and about to be escalated
    again: the shape that once put ~2,900 rows in an append-only ledger.

    The canonical writer (`orchestrator/local_activities.
    update_work_item_status`) now treats a terminal status as a one-way door and
    refuses exactly that write, but this module must not DEPEND on it. It is a
    different service's process discipline over a column that has no CHECK
    constraint and more than one writer (operators included), and the failure it
    guards against is invisible: nothing would show up in the ledger except the
    escalation rows themselves. So suppression keys on the LEDGER, which cannot
    be rewritten (migrations/0028) — our row stays there whatever happens to
    `status`. The two fixes are independent on purpose; either alone is one
    process being careful.

    It suppresses re-escalation without freezing it forever: the moment anything
    else writes an audit row for the item (real progress, a human, an operator),
    our row stops being the newest and the item becomes detectable again. So the
    number of escalation rows is bounded by the number of times the item actually
    came back to life, never by how often the timer fires.

    Idle time is measured from the newest audit row, falling back to
    `created_at` when there is none. The fallback is not a detail: the `new`
    item in the incident had a single admission row and then nothing, and an
    item that never got even that must still be measurable rather than silently
    invisible. `last_transition_at` is deliberately NOT the clock — it is
    written by whoever moves the status, and an item nobody is moving anymore is
    precisely the case being detected.

    Returns per row: `work_item_id` (named as in
    `reconcile.pending_reply_work_items`, so a caller can hand it straight to
    `escalate_stranded`), `status`, `source`, `source_ref`, `last_event_at`
    (None when the item has no audit row at all) and `idle_seconds` — always a
    number, because it is EXTRACT over `COALESCE(last_event_at, created_at)` and
    `created_at` is NOT NULL (migrations/0001_foundation.sql). `escalate_stranded`
    puts it through `int()`, so a None here would be a TypeError there.

    Oldest-silence-first, capped by `limit` — same blast-radius reasoning as the
    reply sweep: if something upstream dies and thousands of items go quiet at
    once, a sweep should crawl and let a human look, not escalate an entire
    tenant's queue in one pass.

    Reads only. See the module docstring for why resuming is not on offer, and
    why not even an audit row is written from here.
    """
    # WHAT THIS GUARD DOES: it rejects the degenerate threshold, nothing more. At
    # zero or below, `now() - make_interval(secs => %s)` is now() or later, so the
    # idle predicate stops filtering anything: every non-excluded item in the
    # tenant is returned (bar the ones the ledger guard suppresses), and the caller
    # escalates what it is handed.
    #
    # WHAT IT DOES NOT DO: it cannot tell too-short from long-enough. Any positive
    # threshold below the longest gap the ENGINE legitimately leaves between audit
    # rows still escalates healthy work in bulk, and that gap is not derivable
    # here — it is made of the orchestrator's activity start-to-close timeouts and
    # retry budgets, an L1 pipeline's duration and `ci_poll_interval_seconds`
    # (`DSE_CI_POLL_INTERVAL_SECONDS`, another service's configuration, raisable
    # without touching this file). Whoever wires the sweep owns that number, along
    # with the Temporal probe the module docstring requires between the two halves;
    # `limit` is what bounds the damage of getting it wrong.
    if idle_for_seconds <= 0:
        raise ValueError("idle_for_seconds must be positive")
    if limit <= 0:
        raise ValueError("limit must be positive")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT wi.id, wi.status, wi.source, wi.source_ref,
                   ev.last_event_at,
                   EXTRACT(EPOCH FROM (
                       now() - COALESCE(ev.last_event_at, wi.created_at)
                   ))::bigint AS idle_seconds
            FROM work_items wi
            LEFT JOIN LATERAL (
                SELECT max(a.ts) AS last_event_at,
                       max(a.ts) FILTER (WHERE a.action = %s) AS last_escalation_at
                FROM audit_log a
                WHERE a.tenant_id = wi.tenant_id AND a.work_item_id = wi.id
            ) ev ON TRUE
            WHERE wi.tenant_id = %s
              AND wi.status <> ALL(%s)
              AND (ev.last_escalation_at IS NULL
                   OR ev.last_escalation_at < ev.last_event_at)
              AND COALESCE(ev.last_event_at, wi.created_at)
                  < now() - make_interval(secs => %s)
            ORDER BY COALESCE(ev.last_event_at, wi.created_at) ASC
            LIMIT %s
            """,
            # `a.tenant_id = wi.tenant_id` is not redundant: audit_log is LIST
            # partitioned by tenant_id, and without it the lateral scans every
            # tenant's partition instead of one.
            #
            # `last_escalation_at IS NULL` is the never-escalated case (including
            # an item with no ledger row at all, which must stay detectable — the
            # `new` item in the incident nearly was one).
            (
                STRANDED_ESCALATION_ACTION,
                tenant_id,
                list(_EXCLUDED_STATUSES),
                float(idle_for_seconds),
                limit,
            ),
        )
        rows = cur.fetchall()

    return [
        {
            "work_item_id": r[0],
            "status": r[1],
            "source": r[2],
            "source_ref": r[3] if isinstance(r[3], dict) else {},
            "last_event_at": r[4],
            "idle_seconds": int(r[5]),
        }
        for r in rows
    ]


def escalate_stranded(
    conn, *, work_item_id: str, tenant_id: str, idle_seconds: int, actor: str
) -> bool:
    """Hand one stranded item to a human: status -> `escalated`, one audit row.

    Returns True when this call is the one that escalated the item, False when
    there was nothing to do — already terminal, already escalated, moved to a
    human wait in the meantime, not this tenant's item, or already reported by
    this sweep with nothing having happened to the item since.

    IDEMPOTENCY IS THE POINT. This runs on a timer, and the timer will see the
    same item again on the next cycle if anything about the sweep is retried. A
    single work item stuck in a loop like that once wrote ~2,900 rows into an
    append-only ledger in thirteen hours and buried the console timeline. So
    TWO guards live in the statement's WHERE clauses, the audit row is written
    only when the UPDATE actually changed a row, and a second call is a pure
    no-op:

      - the status guard, which also closes the window between detection and
        action: an item that a human answered (moving it to
        `needs_clarification` -> `ready`) or that the orchestrator finished
        while the sweep was iterating must not be yanked into a terminal status
        behind their back;
      - the ledger guard, which survives the status guard being undone by
        whatever else writes `work_items.status`. See `stranded_work_items`: only
        audit_log is append-only enough to carry "already reported".

    `state_version` is bumped and `last_transition_at` set to now(), matching
    `orchestrator/local_activities.update_work_item_status` — the canonical
    writer bumps both exactly when `status IS DISTINCT FROM` the new value, and
    here it always is, because `escalated` is itself in `_EXCLUDED_STATUSES` so
    the guard cannot match an already-escalated row. Skipping the bump would
    break the invariant every reader of `state_version` relies on to notice that
    a status it cached is stale.

    This function owns its transaction boundary (like `admit_work_item`): the
    status change and its audit row commit together, or neither does. An
    escalation left uncommitted would be invisible to the next cycle, which
    would escalate again — the audit spam above. Callers must therefore not
    batch other pending writes onto this connection.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH target AS (
                    SELECT id, status FROM work_items
                    WHERE id = %s AND tenant_id = %s AND status <> ALL(%s)
                    FOR UPDATE
                ),
                ledger AS (
                    SELECT max(ts) AS last_event_at,
                           max(ts) FILTER (WHERE action = %s) AS last_escalation_at
                    FROM audit_log
                    WHERE tenant_id = %s AND work_item_id = %s
                )
                UPDATE work_items wi
                SET status = 'escalated',
                    last_transition_at = now(),
                    state_version = wi.state_version + 1
                FROM target t, ledger l
                WHERE wi.id = t.id
                  AND (l.last_escalation_at IS NULL
                       OR l.last_escalation_at < l.last_event_at)
                RETURNING t.status
                """,
                # FOR UPDATE serializes two sweep replicas racing on the same
                # item: the loser re-evaluates the WHERE after the lock is
                # released, finds status = 'escalated', and returns no row —
                # so only one audit row exists no matter how many timers fire.
                #
                # `ledger` is an aggregate over one work item, so it always
                # yields exactly one row and the cross join stays 1:1. It reads
                # the pre-statement snapshot, which is what we want: the row this
                # call is about to write must not suppress this call.
                (
                    work_item_id,
                    tenant_id,
                    list(_EXCLUDED_STATUSES),
                    STRANDED_ESCALATION_ACTION,
                    tenant_id,
                    work_item_id,
                ),
            )
            row = cur.fetchone()

        if row is None:
            # Nothing written. Rolling back releases the snapshot this statement
            # opened instead of leaving the sweep idle-in-transaction across
            # every item it skips (which is most of them, most cycles).
            conn.rollback()
            return False

        status_before = row[0]
        audit_emit(
            actor=actor,
            action=STRANDED_ESCALATION_ACTION,
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={
                "reason": STRANDED_ESCALATION_REASON,
                "status_before": status_before,
                "idle_seconds": int(idle_seconds),
            },
            conn=conn,
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
