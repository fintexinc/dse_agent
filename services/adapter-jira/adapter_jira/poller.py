"""WSA-E5-T2 — fallback poller (MANDATORY: the Jira webhook is best-effort and
can be dropped silently).

The poller periodically sweeps the recently updated issues of each configured
project and RECONCILES each one through the SAME idempotent path as the webhook
(`adapter_jira.ingest`) — since the `message_id`/`event_id` are derived from the
issue STATE (see `events.py`), webhook and poller converge on the same
`event_id` and whichever path arrives second dedupes. They never duplicate.

Overlap window (`grace_seconds`): each project's cursor is rewound by
`grace_seconds` on every round, so that the next sweep re-includes the edge of
the previous window — that way no update that landed exactly on the boundary is
lost (the `event_id` dedup absorbs the overlap at no cost).

Restarting a card is the one action here that is not a reconciliation, and it is
driven by a human gesture rather than by a timer: the `dse` label taken off and
put back. The evidence that a gesture was already acted on lives in Postgres
(`jira_trigger_state` plus `ingest_events.event_id`) and never in Jira — see
`trigger_state` and `_consume_trigger_label`.

Documented attribution limitation: the poller sees only the issue's current
STATE, not the changelog, so it does NOT know WHO made a status transition. An
approval reconstructed by the poller (dropped webhook) is attributed to the
system principal `system:adapter-jira-poller`; when the webhook was NOT dropped
(the normal path), it arrives with the real actor and, being idempotent, the
real actor's record prevails if it arrives first. For tasks, attribution is
stable (the issue reporter), without this limitation.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from dse_identity import resolve_principal
from ingest_gateway import pending_reply_work_items
from ingest_gateway.db import get_connection

from dse_audit import emit as audit_emit

from . import events, trigger_state
from .backend import JiraClientLike
from .ingest import ingest_comment, ingest_status_approval, ingest_task_trigger

logger = logging.getLogger("adapter_jira.poller")

_POLLER_PRINCIPAL = "system:adapter-jira-poller"

#: How far back a project with NO cursor looks on its first sweep.
#:
#: No cursor used to mean no date filter at all — "look at everything" — which
#: was harmless while every project here was a testbed with a handful of tickets
#: and became expensive the moment a real board was bound from the panel: the
#: search paginates the whole project and `_reconcile_issue` reads the comments
#: of every issue in it (1300+ requests on BFA, 2026-09-08, ingesting nothing,
#: since none of that history carries the trigger label).
#:
#: Connecting a board is about the work that comes next. The window is wide
#: enough for "I labelled the card, then bound the board" and short enough that
#: the project's history is never walked.
_FIRST_SWEEP_LOOKBACK = timedelta(minutes=15)


def _relative_bound(since: datetime | None, now: datetime) -> str | None:
    """JQL time bound as RELATIVE minutes (`-90m`) instead of a timestamp.

    JQL interprets an absolute literal like `"2026-07-25 19:40"` in the Jira
    ACCOUNT's timezone, never in UTC. The cursor is UTC, so on an account set to
    America/New_York the poller was asking for issues updated four hours in the
    FUTURE and matched nothing.

    That break was invisible for the worst possible reason: the very first sweep
    runs with no cursor and therefore no date filter at all, so it succeeded and
    ingested a ticket. Every sweep after it silently returned zero, and the
    poller looked healthy the whole time — cursor advancing, no errors, no
    issues.

    A relative bound carries no timezone, so there is nothing left to get wrong.
    Returns None when there is no cursor yet (first sweep sees everything).
    """
    if since is None:
        return None
    # Round UP so the window never ends before the cursor: losing an update is
    # unrecoverable, re-reading one is free (event_id dedup absorbs it).
    minutes = max(1, math.ceil((now - since).total_seconds() / 60))
    return f"-{minutes}m"


class JiraPoller:
    def __init__(
        self,
        client: JiraClientLike,
        *,
        tenant_id: str,
        projects: list[str],
        trigger_label: str,
        approved_status: str,
        rejected_status: str,
        grace_seconds: int = 120,
        reconcile_comments: bool = True,
    ):
        self._client = client
        self._tenant_id = tenant_id
        # The environment's list is the FALLBACK half of the sweep set; the
        # other half comes from the panel's bindings, re-read every round (see
        # `_projects_to_sweep`).
        self._configured_projects = projects
        self._trigger_label = trigger_label
        self._approved_status = approved_status
        self._rejected_status = rejected_status
        self._grace = timedelta(seconds=grace_seconds)
        self._reconcile_comments = reconcile_comments

    def _self_account_id(self) -> str | None:
        """accountId the DSE posts as, when the client can tell us.

        Optional on the client protocol so a fixture without it still works —
        the filter then goes inert rather than raising.
        """
        getter = getattr(self._client, "self_account_id", None)
        return getter() if callable(getter) else None

    def _projects_to_sweep(self) -> list[str]:
        """The projects this round sweeps: the panel's bindings ∪ the environment.

        Read every round, on purpose. Binding a board in the console is the only
        gesture an operator can make from a browser, and it used to decide just
        half of the connection — which repository a card lands in — while
        whether anyone READ that project came from `JIRA_POLL_PROJECTS`, a
        Secret outside this repository, consulted once at process start. So a
        board bound on the site went unread until someone edited the Secret and
        restarted the Deployment, with nothing anywhere saying so.

        The environment stays authoritative for a project with no binding (the
        `BD` testbed is one), and an unreachable database degrades to it rather
        than raising: this poller is itself the fallback for a dropped webhook.
        """
        projects = list(self._configured_projects)
        try:
            conn = get_connection()
        except Exception:  # noqa: BLE001 — the sweep set must never be the thing that fails
            logger.exception("could not open a connection to read the project bindings")
            return projects
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT binding_value FROM repo_bindings "
                    "WHERE tenant_id = %s AND platform = 'jira' AND binding_type = 'project' "
                    "ORDER BY binding_value",
                    (self._tenant_id,),
                )
                bound = [r[0] for r in cur.fetchall() if r[0]]
        except Exception:  # noqa: BLE001
            logger.exception("could not read the project bindings; sweeping the configured list")
            return projects
        finally:
            conn.close()

        for project in bound:
            if project not in projects:
                projects.append(project)
        if bound and set(projects) != set(self._configured_projects):
            logger.info("sweeping %s (bindings: %s)", projects, bound)
        return projects

    def poll_once(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        reconciled = 0
        for project in self._projects_to_sweep():
            # A project with no cursor is one that just joined — a board bound
            # in the panel, or a fresh deployment. It starts from a window, not
            # from the beginning of its history (see `_FIRST_SWEEP_LOOKBACK`).
            since = self._get_cursor(project) or (now - _FIRST_SWEEP_LOOKBACK)
            issues = self._client.search_updated(project, _relative_bound(since, now))
            for issue in issues:
                # Per-issue resilience (finding from the real run BD-39,
                # 2026-07-23): search_updated returns the batch in ORDER BY
                # updated ASC, so a freshly labeled issue sorts LAST. If an
                # earlier issue blows up during reconcile (malformed comment,
                # unexpected ADF, etc.), the failure must NOT abort the batch and
                # leave the freshly labeled one behind — isolate each issue and
                # carry on.
                try:
                    reconciled += self._reconcile_issue(issue, now=now)
                except Exception:  # noqa: BLE001 — one bad issue must not stall the project
                    logger.exception(
                        "reconcile failed for %s; continuing the batch", issue.get("key", "?")
                    )
            # The cursor rewinds by the grace window so as not to "lose" updates
            # that were still inside the window in this round.
            self._set_cursor(project, now - self._grace)
        # Independent of the window and of the cursor: whatever is blocked
        # waiting on a human gets its ticket re-read every cycle. This is the
        # only path that can rescue an answer the window has already passed.
        reconciled += self._recover_pending_replies()
        return reconciled

    def _reporter_identity(self, issue: dict) -> tuple[str, str, str | None]:
        """`(account_id, resolved_principal, display_name)` of the issue reporter.

        The reporter is MANDATORY for anything that creates a work item (BD-39):
        admitting one with `requester=system:adapter-jira-poller` means the
        ticket's own author is not authorized to answer its clarification, and the
        task sits in `needs_clarification` forever.
        """
        reporter = issue.get("fields", {}).get("reporter") or {}
        account_id = reporter.get("accountId")
        if not account_id:
            return _POLLER_PRINCIPAL, _POLLER_PRINCIPAL, reporter.get("displayName")
        return (
            account_id,
            resolve_principal("jira", account_id, reporter.get("displayName")),
            reporter.get("displayName"),
        )

    def _reconcile_issue(self, issue: dict, *, now: datetime) -> int:
        count = 0
        key = events.ticket_key(issue)

        # The label is observed on EVERY sweep, present or absent: seeing it
        # absent is what arms the next gesture. The generation only moves on the
        # absent → present edge, so a label that merely sits on a card — nobody
        # removed it, or we could not — advances nothing, sweep after sweep.
        label_present = events.has_trigger_label(issue, self._trigger_label)
        generation, _advanced = trigger_state.observe(
            tenant_id=self._tenant_id,
            issue_id=str(issue.get("id") or ""),
            ticket_key=events.ticket_key(issue),
            label_present=label_present,
        )
        if label_present:
            account_id, principal, display_name = self._reporter_identity(issue)
            result = ingest_task_trigger(
                issue,
                tenant_id=self._tenant_id,
                actor_account_id=account_id,
                resolved_principal=principal,
                display_name=display_name,
                generation=generation,
            )
            if result.get("path") == "new_task":
                # Only after the work item is durable. Removing first and failing
                # to admit would erase the human's request with nothing to show
                # for it.
                self._consume_trigger_label(
                    events.ticket_key(issue), work_item_id=result.get("work_item_id")
                )
            count += 1

        status = events.issue_status_name(issue)
        if status in (self._approved_status, self._rejected_status):
            verdict = "approved" if status == self._approved_status else "rejected"
            ingest_status_approval(
                issue,
                tenant_id=self._tenant_id,
                target_status=status,
                verdict=verdict,
                route="re_plan" if verdict == "rejected" else None,
                actor_account_id=_POLLER_PRINCIPAL,
                resolved_principal=_POLLER_PRINCIPAL,
            )
            count += 1

        if self._reconcile_comments:
            count += self._ingest_comments(key)
        return count

    def _consume_trigger_label(self, key: str, *, work_item_id: str | None) -> None:
        """Take the `dse` label off a card whose attempt is now durable.

        The label becomes a button: it disappears when the DSE picks the work up,
        and putting it back is how a human asks for another attempt. That is the
        convenience; the guard is the latch in Postgres.

        Nothing here touches the latch. The next sweep observes the card and
        does that on its own: label gone → disarmed, and the gesture is armed
        again; label still there → still armed, and the card stays quiet. So a
        removal that answers 200 without applying — the one sequence that could
        restart the same work every sweep, the shape behind the ~2,900 audit
        rows — is harmless by construction rather than by carefulness.

        The read-back is therefore not a guard but a diagnosis: it is how the
        operator learns the label did not come off, which is almost always a
        missing *Edit Issues* permission and which no log line explains a week
        later.
        """
        try:
            self._client.remove_label(key, self._trigger_label)
            still_there = self._trigger_label in (self._client.get_labels(key) or [])
        except Exception:  # noqa: BLE001 — the admission is already durable
            logger.exception("could not remove the trigger label from %s", key)
            still_there = True

        if still_there:
            audit_emit(
                actor=_POLLER_PRINCIPAL,
                action="jira_trigger_label_removal_failed",
                tenant_id=self._tenant_id,
                work_item_id=work_item_id,
                details={"ticket_key": key, "label": self._trigger_label},
            )

    def _ingest_comments(self, key: str) -> int:
        """Feed every comment on the ticket through the idempotent ingest path.

        Re-reading a comment already seen is free — it dedupes on `event_id` —
        which is what lets this be called both from the windowed sweep and from
        the pending-reply recovery below.
        """
        count = 0
        for c in self._client.get_comments(key):
            author = c.get("author") or {}
            account_id = author.get("accountId") or _POLLER_PRINCIPAL
            principal = (
                resolve_principal("jira", account_id, author.get("displayName"))
                if author.get("accountId")
                else _POLLER_PRINCIPAL
            )
            ingest_comment(
                tenant_id=self._tenant_id,
                key=key,
                comment_id=str(c["id"]),
                body=c.get("body", ""),
                actor_account_id=account_id,
                resolved_principal=principal,
                display_name=author.get("displayName"),
                self_account_id=self._self_account_id(),
            )
            count += 1
        return count

    def _recover_pending_replies(self) -> int:
        """Re-read the tickets of work items that are BLOCKED waiting on a human.

        The windowed sweep only ever sees issues updated in the last few
        minutes, so an answer that lands while the poller is down — or that is
        simply read late — falls out of the window and is never picked up again.
        The task then waits forever on a reply the platform already shows,
        silently, and only a hand-written database update unblocks it. That
        happened twice on BD-40 and BD-41 before this existed.

        These tickets are re-read regardless of the window. The set is tiny by
        construction (a task only sits here between question and answer), and
        `pending_reply_work_items` excludes `awaiting_plan_approval` — an
        approval is a decision, never recovered from re-read text.
        """
        try:
            conn = get_connection()
        except Exception:  # noqa: BLE001 — recovery must never break the sweep
            logger.exception("could not open a connection to recover pending replies")
            return 0
        try:
            pending = pending_reply_work_items(conn, tenant_id=self._tenant_id, source="jira")
        except Exception:  # noqa: BLE001
            logger.exception("could not list work items awaiting a reply")
            return 0
        finally:
            conn.close()

        count = 0
        for row in pending:
            key = (row.get("source_ref") or {}).get("ticket_key")
            if not key:
                continue
            try:
                count += self._ingest_comments(key)
            except Exception:  # noqa: BLE001 — one bad ticket must not stall the rest
                logger.exception("could not recover replies for %s; continuing", key)
        return count

    # --- persisted cursor (jira_poll_state) ---
    def _get_cursor(self, project: str) -> datetime | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_polled_at FROM jira_poll_state WHERE tenant_id = %s AND project_key = %s",
                    (self._tenant_id, project),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else None
        finally:
            conn.close()

    def _set_cursor(self, project: str, ts: datetime) -> None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jira_poll_state (tenant_id, project_key, last_polled_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (tenant_id, project_key)
                    DO UPDATE SET last_polled_at = EXCLUDED.last_polled_at
                    """,
                    (self._tenant_id, project, ts),
                )
            conn.commit()
        finally:
            conn.close()
