"""WSA-E4-T1/T2 — GitHub adapter: inbound (GitHub App webhooks) and outbound
(a single status comment, edited in-place, under the GitHub App identity).
100% stateless adapter — same convention as adapter-slack.

Core rule of WSA-E4-T1: a comment on a PR (via `issue_comment` on an issue
that is a PR, or via `pull_request_review_comment`) NEVER creates a new
WorkItem — it only correlates (`signal`) to an active WorkItem by PR/issue
number, or is ignored (with audit) if there is no active one.

`POST /internal/reconcile` (bottom of this file) closes the gap left by a lost
webhook delivery: it re-reads the threads of work items blocked waiting on a
human REPLY and feeds them through the same intake. Never approvals — see the
comment block above the endpoint.
"""
from __future__ import annotations

import json
import logging
import time

from dse_audit import emit as audit_emit
from dse_contracts import EventKind, mutable_comment
from dse_identity import resolve_principal
from fastapi import FastAPI, HTTPException, Request
from ingest_gateway import (
    AdmissionBlocked,
    NonTaskAdmissionRefused,
    admit_work_item,
    recorded_work_item_id,
    classify_task_class,
    correlate,
    get_connection,
    pending_reply_work_items,
    record_signal_event,
    resolve_tenant,
    sanitize_content,
    verify_github_signature,
)
from pydantic import BaseModel

from .backend import GithubCommentBackend, GithubReaderLike, build_real_github_client
from .comment_store import SURFACE, PgCommentStateStore
from .config import get_bot_mention_login, get_task_label, get_tenant_id, get_webhook_secret
from .events import (
    build_event_from_issue_assigned_or_labeled,
    build_event_from_issue_comment,
    build_event_from_pr_merged,
    build_event_from_pr_review,
    build_event_from_pr_review_comment,
)
from .ratelimit import GithubRateLimited

logger = logging.getLogger("adapter_github")

app = FastAPI(title="dse-adapter-github")

# How long each endpoint may keep ITS OWN caller waiting on GitHub, counted from
# the start of the request. These are the callers' limits, not GitHub's — the
# whole point of `adapter_github.ratelimit` taking a deadline is that only the
# call site knows them.
#
# `/internal/reconcile` is invoked by the reply-reconciler CronJob, which abandons
# the request after 120s (infra/helm/dse/templates/reply-reconciler.yaml). The
# deadline stops the sweep from STARTING another thread, not from finishing the one
# it is in, and one thread is `get_issue` plus up to `max_pages` comment pages —
# six requests at a 10s socket timeout each, 60s. 45s of budget therefore keeps the
# pathological case (deadline passes one request into the last thread) at 45 + 60 =
# 105s, inside the 120s, with the listing query already counted against the budget.
RECONCILE_BUDGET_S = 45.0

# `/internal/status-comment` is called by the orchestrator best-effort with an 8s
# HTTP timeout (services/orchestrator .../local_activities.py). Waiting LONGER
# than the caller will is how the "exactly 1 status comment per issue" invariant
# broke: the orchestrator gave up at 8s, `MutableCommentWriter` never reached
# `save_ref`, and the next transition found no ref and posted a SECOND comment.
# The budget covers the installation-token exchange AND the comment call, which
# is why it is one deadline built here and threaded through both.
STATUS_COMMENT_BUDGET_S = 3.0


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "adapter-github"}


def _reject(reason: str) -> None:
    audit_emit(
        actor="system:adapter-github",
        action="signature_rejected",
        tenant_id=get_tenant_id(),
        details={"reason": reason, "surface": "github_webhook"},
    )
    raise HTTPException(status_code=401, detail=f"signature_verification_failed:{reason}")


def _resolve_tenant_for(payload: dict) -> str:
    """WSA-E1-T5 — resolves the tenant from the GitHub App installation
    (`payload["installation"]["id"]`) via `tenant_platform_bindings`. A missing
    binding falls back to `DSE_TENANT_ID` with a warning audit row (documented
    single-tenant fallback)."""
    installation_id = (payload.get("installation") or {}).get("id")
    conn = get_connection()
    try:
        rt = resolve_tenant(
            conn,
            platform="github",
            binding_key=str(installation_id) if installation_id is not None else None,
        )
        conn.commit()
        return rt.tenant_id
    finally:
        conn.close()


def _handle_task_creating_event(conv_event, *, principal: str, tenant_id: str,
                                base_branch: str | None = None, task_class: str = "chore",
                                signal_only: bool = False) -> dict:
    """Path used by events that MAY legitimately open a new WorkItem (issues
    assigned/labeled, a comment with a mention on a plain issue).
    """
    sanitized = sanitize_content(conv_event.content_snapshot)
    conn = get_connection()
    try:
        # Recovery sweeps re-read whole threads, so a task that is genuinely
        # waiting meets the same messages on every cycle. Recording dedupes on
        # `event_id`, but only after correlating and auditing — on Jira that
        # turned one stuck ticket into thousands of `signal_duplicate_ignored`
        # rows. Nothing below can change an outcome already reached.
        prior = recorded_work_item_id(conn, conv_event.event_id)
        if prior is not None:
            return {"ok": True, "path": "already_ingested", "work_item_id": prior}

        result = correlate(conn, tenant_id=tenant_id, event=conv_event, requester_principal=principal)


        # The reconciler's leash. It recovers REPLIES to a task that already
        # exists and must never manufacture work: a re-read thread can stop
        # correlating (the item raced into a terminal status, or a newer item on
        # the same thread is already done), and without this guard every message
        # in that thread would fall through to admit_work_item below — one new
        # WorkItem and one real agent turn per comment, from text nobody just
        # sent. The webhook leaves this off, where a message that correlates to
        # nothing genuinely is a new task.
        if signal_only and result.kind != "signal":
            conn.commit()
            return {"ok": True, "path": "not_correlated"}

        if result.kind == "signal":
            # `recorded` is False when this exact event_id was already in the
            # outbox (redelivery). Only the reconciler reads it — it must count
            # and audit a RECOVERY, not a re-read of something already ingested.
            recorded = record_signal_event(
                conv_event,
                tenant_id=tenant_id,
                channel=conv_event.source_ref["repo"],
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id,
                    "recorded": recorded}

        if conv_event.kind != EventKind.task_request:
            # new_task is only allowed when the event is genuinely a creation
            # trigger (assigned/labeled/@mention) — a plain comment with no
            # mention and no active WorkItem is ignored.
            conn.rollback()
            audit_emit(
                actor=principal,
                action="comment_ignored_no_mention_no_active_work_item",
                tenant_id=tenant_id,
                details={"repo": conv_event.source_ref["repo"], "number": conv_event.source_ref["number"]},
            )
            return {"ok": True, "path": "ignored_no_mention"}

        try:
            work_item_id = admit_work_item(
                conv_event,
                tenant_id=tenant_id,
                source="github",
                channel=conv_event.source_ref["repo"],
                requester_principal=principal,
                repo=conv_event.source_ref["repo"],
                base_branch=base_branch,
                task_class=task_class,
                sanitized_content=sanitized,
                conn=conn,
            )
        except AdmissionBlocked:
            return {"ok": True, "path": "blocked_kill_switch"}
        except NonTaskAdmissionRefused as refusal:
            # F2: comentario (clarification_answer/approval/review) que nao
            # correlacionou nunca vira tarefa. Equivalente GitHub da conversa:
            # a ISSUE -- o guard de mention ja ignora a maioria; aqui audita e
            # devolve 200 sem efeito.
            audit_emit(
                actor=principal,
                action="non_task_admission_refused",
                tenant_id=tenant_id,
                details={"kind": refusal.kind,
                         "repo": conv_event.source_ref.get("repo"),
                         "number": conv_event.source_ref.get("number"),
                         "event_id": conv_event.event_id},
                conn=conn,
            )
            conn.commit()
            return {"ok": True, "path": "refused_non_task"}

        if result.provenance_work_item_id:
            audit_emit(
                actor=principal,
                action="work_item_provenance_link",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"previous_work_item_id": result.provenance_work_item_id},
            )

        return {"ok": True, "path": "new_task", "work_item_id": work_item_id}
    finally:
        conn.close()


def _resolve_pr_correlation_ref(conn, *, tenant_id: str, repo: str, pr_number: int) -> dict | None:
    """Post-S7 audit: the WorkItem's source_ref stores the number of the
    originating ISSUE, not of the PR (issue #5 -> PR #6) — correlating by PR
    number never matched. The deterministic bridge is `wse_pr_tracking`
    (populated by the finalizer, 1 PR per WorkItem): it resolves PR -> work_item
    and returns that WorkItem's EXACT source_ref as the correlation_ref
    (containment by equality). None when the PR is not tracked (e.g. a PR opened
    by a human)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT wi.source_ref FROM wse_pr_tracking t
            JOIN work_items wi ON wi.id = t.work_item_id
            WHERE t.tenant_id = %s AND t.repo = %s AND t.pr_number = %s
            ORDER BY t.created_at DESC LIMIT 1
            """,
            (tenant_id, repo, int(pr_number)),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _handle_pr_comment_event(conv_event, *, principal: str, tenant_id: str) -> dict:
    """Path used by comments on a PR (issue_comment on a PR or
    pull_request_review_comment) — NEVER creates a new WorkItem (WSA-E4-T1)."""
    sanitized = sanitize_content(conv_event.content_snapshot)
    conn = get_connection()
    try:
        # First: tracked PR -> source_ref of the owning WorkItem. Fallback: the
        # event's own source_ref WITHOUT extra fields (e.g. review_state), which
        # would break correlate's `@>` containment.
        ref = _resolve_pr_correlation_ref(
            conn, tenant_id=tenant_id,
            repo=conv_event.source_ref["repo"], pr_number=conv_event.source_ref["number"],
        ) or {"repo": conv_event.source_ref["repo"], "number": conv_event.source_ref["number"]}
        result = correlate(conn, tenant_id=tenant_id, event=conv_event,
                           requester_principal=principal, correlation_ref=ref)


        if result.kind == "signal":
            recorded = record_signal_event(
                conv_event,
                tenant_id=tenant_id,
                channel=conv_event.source_ref["repo"],
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id,
                    "recorded": recorded}

        # result.kind == "new_task" -> ZERO new WorkItems from a PR comment, by
        # design (even when there is no match).
        conn.rollback()
        audit_emit(
            actor=principal,
            action="review_comment_ignored_no_active_work_item",
            tenant_id=tenant_id,
            details={"repo": conv_event.source_ref["repo"], "number": conv_event.source_ref["number"]},
        )
        return {"ok": True, "path": "ignored_no_active_work_item"}
    finally:
        conn.close()


def _handle_merge_event(conv_event, *, principal: str, tenant_id: str, pr_number: int) -> dict:
    """WSA-E4-T3 — pull_request merged: correlates by PR number to the ACTIVE
    WorkItem and fires `merged_by_human` (via a deterministic marker in the
    payload, read by the dispatcher). NEVER creates a new WorkItem; with no
    matching active WorkItem it is ignored with audit (documented route)."""
    conn = get_connection()
    try:
        result = correlate(conn, tenant_id=tenant_id, event=conv_event, requester_principal=principal)

        if result.kind == "signal":
            record_signal_event(
                conv_event,
                tenant_id=tenant_id,
                channel=conv_event.source_ref["repo"],
                work_item_id=result.work_item_id,
                extra_payload={"merged_by_human": True, "merged_by": principal, "pr_number": pr_number},
                conn=conn,
            )
            return {"ok": True, "path": "signal_merged_by_human", "work_item_id": result.work_item_id}

        # new_task (no match) OR a terminal match -> the merge fires nothing.
        conn.rollback()
        audit_emit(
            actor=principal,
            action="merge_ignored_no_active_work_item",
            tenant_id=tenant_id,
            details={"repo": conv_event.source_ref["repo"], "number": pr_number},
        )
        return {"ok": True, "path": "ignored_no_active_work_item"}
    finally:
        conn.close()


def _ingest_issue_comment(payload: dict, *, tenant_id: str, signal_only: bool = False) -> dict:
    """The single ingestion path for a comment on an issue/PR.

    Both entry points go through here — the `issue_comment` webhook and the
    reply reconciler (`/internal/reconcile`) — so that a recovered comment is
    treated EXACTLY like a delivered one: same builder, same PR-vs-issue split,
    same mention promotion, same sanitize, same correlate/record_signal_event.
    Any divergence between the two would be a second, less-tested intake with
    its own rules, which is precisely what must not exist.

    `payload` is the `issue_comment` webhook shape; the reconciler rebuilds it
    from the API objects (repository/issue/comment) it read back.
    """
    sender = payload["comment"]["user"]["login"]
    principal = resolve_principal("github", sender, sender)
    conv_event, is_pr_comment = build_event_from_issue_comment(payload, resolved_principal=principal)

    if is_pr_comment:
        return _handle_pr_comment_event(conv_event, principal=principal, tenant_id=tenant_id)

    mention = f"@{get_bot_mention_login()}".lower()
    # The mention promotion is for the WEBHOOK only. Mentioning the bot is the
    # normal way a human phrases a reply on GitHub, and promoting a recovered
    # reply to `task_request` makes it undeliverable: the dispatcher matches on
    # kind BEFORE routing the signal, calls start_workflow, gets
    # WorkflowAlreadyStartedError, marks the row deduped — and the reply never
    # reaches the waiting workflow. The reconciler would then count and audit it
    # as "recovered", so the one mechanism built to expose a silent failure
    # would be asserting that it had been fixed. Left as a clarification answer,
    # it routes and actually unblocks the task.
    if not signal_only and mention in conv_event.content_snapshot.lower():
        conv_event = conv_event.model_copy(update={"kind": EventKind.task_request})
    labels = [lbl.get("name", "") for lbl in (payload.get("issue") or {}).get("labels") or []]
    task_class = classify_task_class(labels=labels)
    return _handle_task_creating_event(
        conv_event, principal=principal, tenant_id=tenant_id, task_class=task_class,
        signal_only=signal_only,
    )


@app.post("/github/webhook")
async def github_webhook(request: Request) -> dict:
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    event_type = request.headers.get("X-GitHub-Event", "")

    check = verify_github_signature(webhook_secret=get_webhook_secret(), body=body, signature_header=signature)
    if not check.verified:
        _reject(check.reason)

    payload = json.loads(body)
    action = payload.get("action")
    tenant_id = _resolve_tenant_for(payload)

    if event_type == "issues" and action in ("assigned", "labeled"):
        if action == "labeled":
            label_name = payload.get("label", {}).get("name", "")
            if label_name != get_task_label():
                return {"ok": True, "path": "ignored_label"}
        sender = payload["sender"]["login"]
        principal = resolve_principal("github", sender, sender)
        conv_event = build_event_from_issue_assigned_or_labeled(
            payload, delivery_id=delivery_id, resolved_principal=principal
        )
        # S6 (Phase 5): GitHub issues carry no base_branch; the repo default
        # comes in the webhook payload itself (repository.default_branch) — no
        # extra API call. Fill it on the WorkItem so the completeness gate (S2)
        # does not ask for clarification needlessly and the clone (S4) knows the
        # base branch.
        default_branch = (payload.get("repository") or {}).get("default_branch")
        # §A: classify from the issue's label list (not just the triggering label).
        labels = [lbl.get("name", "") for lbl in (payload.get("issue") or {}).get("labels") or []]
        task_class = classify_task_class(labels=labels)
        return _handle_task_creating_event(
            conv_event, principal=principal, tenant_id=tenant_id,
            base_branch=default_branch, task_class=task_class,
        )

    if event_type == "issue_comment" and action == "created":
        return _ingest_issue_comment(payload, tenant_id=tenant_id)

    if event_type == "pull_request_review_comment" and action == "created":
        sender = payload["comment"]["user"]["login"]
        principal = resolve_principal("github", sender, sender)
        conv_event = build_event_from_pr_review_comment(payload, resolved_principal=principal)
        return _handle_pr_comment_event(conv_event, principal=principal, tenant_id=tenant_id)

    if event_type == "pull_request_review" and action == "submitted":
        # Post-S7 audit: formal review (Request changes / Approve in the UI) —
        # the only event carrying `review.state`. Same signal path as PR
        # comments (NEVER creates a WorkItem — WSA-E4-T1); the dispatcher
        # converts review_state into a verdict (changes_requested/approved) and
        # a state=commented goes through with no verdict (documented no-op route).
        sender = payload["review"]["user"]["login"]
        principal = resolve_principal("github", sender, sender)
        conv_event = build_event_from_pr_review(payload, resolved_principal=principal)
        return _handle_pr_comment_event(conv_event, principal=principal, tenant_id=tenant_id)

    if event_type == "pull_request" and action == "closed":
        # WSA-E4-T3: only the merge fires a signal. A PR closed WITHOUT a merge
        # fires NOTHING (documented route).
        pr = payload["pull_request"]
        pr_number = pr["number"]
        repo = payload["repository"]["full_name"]
        if not pr.get("merged"):
            audit_emit(
                actor="system:adapter-github",
                action="pr_closed_without_merge_ignored",
                tenant_id=tenant_id,
                details={"repo": repo, "number": pr_number},
            )
            return {"ok": True, "path": "ignored_pr_closed_unmerged"}

        merged_by_login = (pr.get("merged_by") or {}).get("login") or payload.get("sender", {}).get("login", "")
        principal = resolve_principal("github", merged_by_login, merged_by_login)
        merge_sha = pr.get("merge_commit_sha") or delivery_id
        conv_event = build_event_from_pr_merged(payload, resolved_principal=principal, merge_sha=merge_sha)
        return _handle_merge_event(conv_event, principal=principal, tenant_id=tenant_id, pr_number=pr_number)

    if event_type in ("installation", "installation_repositories"):
        # Marking a repository on the App's installation page leaves NO other
        # trace: nothing here writes a row for it, and GitHub's own delivery log
        # only reaches back ~7 days, so after a week the click is unrecoverable.
        # An allowlist of exactly these two, never an emit on the fallthrough
        # below: `pull_request/edited` is fired by the DSE's own PATCH on the PR
        # body, so a catch-all would fill `audit_log` with the DSE auditing
        # itself and bury the one row this exists for.
        installation = payload.get("installation") or {}
        # Neither event carries `payload["repository"]` — the whole payload is
        # read through `.get()` chains for that reason.
        repository_selection = payload.get("repository_selection") or installation.get("repository_selection")
        audited = 0
        for key in ("repositories_added", "repositories_removed", "repositories"):
            # The delivery's `action` is NOT the direction of the individual row:
            # both arrays always ship in the payload and the loop reads both on
            # purpose, so an `action: "added"` delivery that also carries
            # `repositories_removed` would stamp "added" on a repo that was
            # unmarked — backwards, in the one row this branch exists to write.
            # The array a repo came from is the direction; for the `installation`
            # event's single `repositories` list the action IS it (created/deleted).
            change = {"repositories_added": "added", "repositories_removed": "removed"}.get(key) or action
            for repo in payload.get(key) or []:
                try:
                    audit_emit(
                        actor="system:adapter-github",
                        action="github_installation_repositories",
                        tenant_id=tenant_id,
                        details={
                            "installation_id": installation.get("id"),
                            "full_name": (repo or {}).get("full_name"),
                            "private": (repo or {}).get("private"),
                            "action": action,
                            "change": change,
                            "repository_selection": repository_selection,
                        },
                    )
                    audited += 1
                except Exception:  # noqa: BLE001 — `emit` re-raises; a lost trace must not become a 500
                    logger.exception(
                        "installation webhook: could not audit %s", (repo or {}).get("full_name")
                    )
        return {"ok": True, "path": "installation_repositories_audited", "audited": audited}

    return {"ok": True, "path": "ignored_unhandled_event_type"}


class StatusCommentRequest(BaseModel):
    work_item_id: str
    repo: str
    issue_number: int
    body: str
    actor: str


@app.post("/internal/status-comment")
def upsert_status_comment(req: StatusCommentRequest) -> dict:
    """WSA-E4-T2: exactly 1 status comment per issue/PR, edited in-place, under
    the GitHub App identity (`build_real_github_client` uses an installation
    access token, never a personal PAT).

    One `STATUS_COMMENT_BUDGET_S` deadline covers the token exchange and the
    comment call together, which is what keeps a throttled post from outliving
    the orchestrator's 8s timeout and reappearing as a SECOND comment on the next
    transition."""
    client = build_real_github_client(deadline=time.time() + STATUS_COMMENT_BUDGET_S)
    backend = GithubCommentBackend(client)
    store = PgCommentStateStore()
    writer = mutable_comment.MutableCommentWriter(backend, store, SURFACE)

    comment_ref = writer.upsert(req.work_item_id, {"repo": req.repo, "number": req.issue_number}, req.body)

    audit_emit(
        actor=req.actor,
        action="status_comment_upserted",
        tenant_id=get_tenant_id(),
        work_item_id=req.work_item_id,
        details={"surface": SURFACE, "repo": req.repo, "issue_number": req.issue_number},
    )
    return {"ok": True, "comment_ref": comment_ref}


# --- reply reconciler ---------------------------------------------------------
#
# A human answers the clarification question and NOTHING happens: the webhook
# delivery was lost (GitHub retried while the adapter was down, or the delivery
# failed outright), so the task sits in `needs_clarification` forever, silently,
# with the answer visible on the issue for anyone who thinks to look. It took a
# hand-written UPDATE on the database to unblock, twice in one afternoon.
#
# The reconciler re-reads the threads of the few work items that are blocked
# waiting on a human and feeds whatever it finds through the normal intake. It
# recovers REPLIES only — `pending_reply_work_items` returns
# needs_clarification/awaiting_repo_selection and deliberately excludes
# `awaiting_plan_approval`, because re-reading is exactly the operation the
# TOCTOU defense (WSA-E2-T2) forbids for an approval: a plan approved from
# re-read text is a decision manufactured from a message an attacker was free to
# edit after the fact. A lost approval stays lost and a human re-approves.

_RECONCILER_ACTOR = "system:adapter-github-reconciler"


def _is_bot_comment(comment: dict) -> bool:
    """True for a comment written by the DSE itself (or any other app).

    Author-based, which is safe HERE and was a trap on Jira (BD-41): a GitHub
    App comments under its own bot account — `user.type == "Bot"` and a login
    suffixed `[bot]` — so filtering by author never touches a human. On Jira the
    bot and the human share the API token's account, so the same filter would
    silence the very person the task is waiting for.

    Without it the reconciler would read the DSE's own clarification QUESTION
    back as the human's ANSWER on every cycle — the exact feedback loop that
    burned BD-40 on the Jira poller.
    """
    user = comment.get("user") or {}
    if str(user.get("type", "")).lower() == "bot":
        return True
    login = str(user.get("login", "")).lower()
    # `GITHUB_BOT_LOGIN` covers an install whose DSE identity is a plain user
    # account (no `[bot]` suffix, type "User") instead of a GitHub App.
    return login.endswith("[bot]") or login == get_bot_mention_login().lower()


def _recover_thread(reader: GithubReaderLike, row: dict, *, tenant_id: str) -> int:
    """Re-reads one blocked work item's thread. Returns how many events were
    genuinely recovered (i.e. reached the outbox for the first time)."""
    source_ref = row.get("source_ref") or {}
    repo = source_ref.get("repo")
    number = source_ref.get("number")
    if not repo or number is None:
        return 0
    number = int(number)

    # The issue object carries `pull_request` (PR vs plain issue) and `labels`,
    # the two fields the webhook path reads besides the comment itself — fetched
    # once per work item, not once per comment.
    issue = reader.get_issue(repo, number)

    recovered = 0
    for comment in reader.list_issue_comments(repo, number):
        if _is_bot_comment(comment):
            continue
        result = _ingest_issue_comment(
            {
                "action": "created",
                "repository": {"full_name": repo},
                "issue": issue,
                "comment": comment,
            },
            # Recovery only: never open work, never promote a mention to a task.
            signal_only=True,
            tenant_id=tenant_id,
        )
        if not result.get("recorded"):
            # `recorded` is set only when a signal reached the outbox for the
            # first time. Everything else — a comment already ingested (dedup by
            # event_id), an unauthorized author, a comment the webhook path would
            # ignore anyway — recovered nothing, so nothing is claimed. Repeated
            # sweeps over the same thread therefore report zero and audit
            # nothing, which is what makes this safe to run on a timer.
            continue
        audit_emit(
            actor=_RECONCILER_ACTOR,
            action="reply_recovered",
            tenant_id=tenant_id,
            work_item_id=result.get("work_item_id") or row.get("work_item_id"),
            details={
                "surface": SURFACE,
                "repo": repo,
                "number": number,
                "comment_id": comment.get("id"),
                "blocked_status": row.get("status"),
                "path": result.get("path"),
            },
        )
        recovered += 1
    return recovered


@app.post("/internal/reconcile")
def reconcile_pending_replies() -> dict:
    """Recovers clarification replies whose webhook never arrived.

    Best-effort end to end: one unreadable thread (deleted issue, revoked
    permission, malformed source_ref) must never cost the other blocked work
    items their recovery, so every item is isolated and the sweep carries on.
    Nothing here returns 5xx — a caller on a timer would only retry into the
    same failure, and `ok: False` says more than a stack trace at the other end.

    ONE budget for the WHOLE sweep (`RECONCILE_BUDGET_S`), not one per request:
    the client is built once with a single deadline — shared with the token
    exchange and with every `list_issue_comments` page — and the loop below
    refuses to start another thread once that deadline has passed. Without both
    halves, a throttled installation turned this endpoint into tens of minutes of
    sleeping inside a request the CronJob abandons after 120s. Stopping early
    loses nothing: `pending_reply_work_items` rotates through the pending set, so
    the next cycle continues from where this one gave up."""
    tenant_id = get_tenant_id()
    deadline = time.time() + RECONCILE_BUDGET_S

    try:
        conn = get_connection()
        try:
            pending = pending_reply_work_items(conn, tenant_id=tenant_id, source="github")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — a broken sweep must not become a 5xx loop
        logger.exception("reconcile: could not list the work items awaiting a reply")
        return {"ok": False, "checked": 0, "recovered": 0}

    if not pending:
        # No thread to read -> no reason to spend a GitHub App token exchange.
        return {"ok": True, "checked": 0, "recovered": 0}

    try:
        reader = build_real_github_client(deadline=deadline)
    except Exception:  # noqa: BLE001
        logger.exception("reconcile: could not authenticate as the GitHub App")
        return {"ok": False, "checked": 0, "recovered": 0}

    recovered = 0
    checked = 0
    for row in pending:
        if time.time() >= deadline:
            # The client would no longer sleep on a throttle, but it would still
            # spend requests per remaining thread. Starting no new thread past the
            # deadline is what makes the sweep fit inside the CronJob's 120s.
            logger.warning(
                "reconcile: out of budget after %d/%d threads; the next cycle continues",
                checked, len(pending),
            )
            break
        checked += 1
        try:
            recovered += _recover_thread(reader, row, tenant_id=tenant_id)
        except GithubRateLimited:
            # GitHub throttles per INSTALLATION, so every remaining thread faces
            # the same limit with the same token. Marching on would spend the rest
            # of the request failing identically — and this used to be swallowed
            # by the `except Exception` below, which is how the sweep kept going
            # through one throttled thread after another.
            logger.warning(
                "reconcile: github is throttling this installation; stopping after %d/%d threads",
                checked, len(pending),
            )
            break
        except Exception:  # noqa: BLE001 — one bad thread must not stall the rest
            logger.exception(
                "reconcile: could not recover %s; continuing", row.get("work_item_id")
            )
    return {"ok": True, "checked": checked, "recovered": recovered}
