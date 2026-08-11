"""WSA-E1-T3 — Outbox dispatcher.

Drains unprocessed `ingest_events` with `SELECT ... FOR UPDATE SKIP LOCKED`
(allows N concurrent processes/threads draining the same queue without
duplicating or losing anything — it is the core of the NFR-01 chaos test on
the intake side) and, for each event:

  - `kind == "task_request"` -> `Temporal.start_workflow(WORKFLOW_TYPE,
    work_item_id, id=work_item_id, task_queue=TASK_QUEUE)`. Temporal rejects a
    duplicate workflow_id (`WorkflowAlreadyStartedError`) — treated as an
    idempotent success (never re-raised).
  - any other kind (`clarification_answer`/`approval`/`review_comment`/
    `steering`) -> a signal to a workflow already in flight
    (`WorkflowHandle.signal(SIGNAL_NAME, payload)`), correlated by
    `ingest_events.work_item_id` (already resolved by `correlate()` /
    `record_signal_event()` at ingestion time).

`processed=true` is only set AFTER the StartWorkflow/Signal is confirmed (or
after the duplicate exception) — never before, so events are not lost on a
crash between the dequeue and the Temporal confirmation.

Phase 2 (WSA-E6-T3): the fixed `kind -> signal` map from Phase 1 was replaced
by `_route_signal(status, kind, payload)`, which consults `work_items.status`
to pick the right gate — a `kind=approval` with status
`awaiting_plan_approval` fires `SIGNAL_PLAN_APPROVAL` (plan gate, WSB-E3-T2),
with status `pr_ready`/`review_feedback` it fires `SIGNAL_REVIEW_COMMENT`, and
with any other status it is declined with an audit row (never guesses — P6).
The merge webhook (WSA-E4-T3) arrives with the `merged_by_human` marker in the
payload and fires `SIGNAL_MERGED_BY_HUMAN`.
`clarification_answer`/`review_comment` preserve the Phase 1 behavior. All
signal names come from `dse_contracts.constants` (single source).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, NamedTuple

from dse_audit import emit as audit_emit
from dse_contracts import TASK_QUEUE, WORKFLOW_TYPE
from dse_contracts.constants import (
    SIGNAL_CLARIFICATION_ANSWER,
    SIGNAL_MERGED_BY_HUMAN,
    SIGNAL_PLAN_APPROVAL,
    SIGNAL_REVIEW_COMMENT,
)
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from .db import get_connection

logger = logging.getLogger("ingest_gateway.dispatcher")

_TASK_REQUEST_KIND = "task_request"

# WSA-E6-T3 — WorkItem status in which a `kind=approval` routes to the PLAN
# approval gate (Phase 2, WSB-E3-T2) instead of the review gate.
_STATUS_AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
_STATUS_PR_READY = "pr_ready"
# States in which an approval on the PR is still a legitimate review signal
# (the reviewer may approve after having previously requested changes).
_APPROVAL_REVIEW_STATES = {_STATUS_PR_READY, "review_feedback"}


class DispatchOutcome:
    STARTED = "started"
    DEDUPED_ALREADY_STARTED = "deduped_already_started"
    SIGNALED = "signaled"
    SIGNAL_FAILED = "signal_failed"
    IGNORED_NOT_A_DECISION = "ignored_not_a_decision"
    # WSA-E6-T3: kind=approval arrived with a WorkItem status where the
    # dispatcher cannot know (deterministically) which gate to route to — NEVER
    # guesses (P6). Consumed (processed=true) with an audit row, not retried.
    DECLINED_UNEXPECTED_STATUS = "declined_unexpected_status"
    # Item 4 (rc do canal mínimo): o workflow do signal está MORTO ("already
    # completed"/"workflow not found" — a classe B8). Antes isto era
    # SIGNAL_FAILED, ou seja, retry INFINITO de uma entrega impossível.
    # Permanente: consumido com auditoria + aviso no canal.
    SIGNAL_PERMANENTLY_UNDELIVERABLE = "signal_permanently_undeliverable"


_CHANGES_REQUESTED_STATES = {"changes_requested", "request_changes", "changes-requested"}


class SignalRoute(NamedTuple):
    signal_name: str | None  # None => does not signal
    payload: dict[str, Any] | None
    reason: str  # label for audit/observability


def _content_of(raw_payload: dict[str, Any]) -> str:
    return raw_payload.get("sanitized_content") or raw_payload.get("content_snapshot", "")


def _actor_principal(raw_payload: dict[str, Any]) -> str | None:
    actor = raw_payload.get("actor") or {}
    return actor.get("resolved_principal")


# C4 (report 07): repo/branch markers in a clarification answer.
# Accepts `repo=owner/name` or `repo: owner/name` (same for branch/base_branch).
# owner/name follows the GitHub format (slug). Deterministic.
_RE_REPO = re.compile(r"\brepo(?:sitory)?\s*[:=]\s*([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)", re.I)
_RE_BRANCH = re.compile(r"\b(?:base[_-]?)?branch\s*[:=]\s*([A-Za-z0-9._/-]+)", re.I)


def _extract_repo_markers(content: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not content:
        return out
    m = _RE_REPO.search(content)
    if m:
        out["repo"] = m.group(1)
    b = _RE_BRANCH.search(content)
    if b:
        out["base_branch"] = b.group(1)
    return out


def _review_signal_payload(raw_payload: dict[str, Any], content: str) -> dict[str, Any] | None:
    """FLAT format that `WorkItemLifecycleWorkflow.review_comment` reads.
    Returns None when the review comment carries no formal verdict (it is not a
    decision) — same rule as Phase 1."""
    review_state = str(raw_payload.get("source_ref", {}).get("review_state", "")).lower()
    if review_state in _CHANGES_REQUESTED_STATES:
        return {"verdict": "changes_requested", "comment": content}
    if review_state == "approved":
        return {"verdict": "approved", "comment": content}
    return None


def _plan_approval_payload(raw_payload: dict[str, Any], content: str) -> dict[str, Any]:
    """Payload of the plan approval gate (SIGNAL_PLAN_APPROVAL), format
    documented in `dse_contracts.constants`: {verdict, route (required when
    rejected), comment, actor}. `verdict`/`route` come from DETERMINISTIC
    markers set by the adapter (`extra_payload` — e.g. the value of the Slack
    "approve"/"reject" button or the destination Jira column), defaulting to
    `approved` when the adapter did not provide them (WSA-E6-T3)."""
    verdict = str(raw_payload.get("approval_verdict", "approved")).lower()
    if verdict not in ("approved", "rejected"):
        verdict = "approved"
    p: dict[str, Any] = {"verdict": verdict, "comment": content, "actor": _actor_principal(raw_payload)}
    if verdict == "rejected":
        # WSB-E3-T3: route is required when rejected.
        p["route"] = raw_payload.get("approval_route") or "re_plan"
    return p


def _route_signal(status: str | None, kind: str, raw_payload: dict[str, Any]) -> SignalRoute:
    """WSA-E6-T3 — signal routing by WorkItem STATUS (no longer by `kind`
    alone, as in the Phase 1 heuristic). Deterministic (P1): no flow decision
    made by an LLM — it only consults `work_items.status` + markers set by the
    adapter.

    Preserves the Phase 1 behavior for `clarification_answer` and
    `review_comment`. What changes is `kind=approval`: it now depends on the
    status (plan gate vs. review gate), and a merge webhook (`merged_by_human`
    marker) fires `SIGNAL_MERGED_BY_HUMAN` (WSA-E4-T3)."""
    content = _content_of(raw_payload)

    # WSA-E4-T3: pull_request merged webhook -> merged_by_human. Deterministic
    # marker set by adapter-github; independent of status (the merge is the
    # third pause point, the workflow only consumes it while waiting).
    if raw_payload.get("merged_by_human"):
        return SignalRoute(
            SIGNAL_MERGED_BY_HUMAN,
            {"merged_by": raw_payload.get("merged_by"), "pr_number": raw_payload.get("pr_number")},
            "merged_by_human",
        )

    if kind == "clarification_answer":  # PRESERVES Phase 1 + C4 (report 07)
        payload = {"text": content, "acceptance_criteria": content}
        # C4: Slack/Jira tasks are born without a repo — the clarification
        # answer is the rescue path. Extracts DETERMINISTIC markers from the
        # text (`repo=org/x`, `branch=main`) so the workflow can fill in
        # input.repo/base_branch and the completeness gate passes. P1: regex
        # only, no LLM.
        payload.update(_extract_repo_markers(content))
        return SignalRoute(SIGNAL_CLARIFICATION_ANSWER, payload, "clarification")

    if kind == "review_comment":  # PRESERVES Phase 1
        payload = _review_signal_payload(raw_payload, content)
        if payload is None:
            return SignalRoute(None, None, "review_comment_no_verdict")
        return SignalRoute(SIGNAL_REVIEW_COMMENT, payload, "review_comment")

    if kind == "steering":  # PRESERVES Phase 1 (best-effort, handled as a review)
        return SignalRoute(SIGNAL_REVIEW_COMMENT, {"text": content, "comment": content}, "steering")

    if kind == "approval":  # WSA-E6-T3: routes by status
        # A rota de veredito de PARQUE saiu em 2026-08-10 com o parque de spec:
        # não há mais status `spec_conflict` para o qual rotear, nem botões que
        # emitam `park_verdict`. O que sobra aqui é aprovação de plano e review.
        if status == _STATUS_AWAITING_PLAN_APPROVAL:
            return SignalRoute(SIGNAL_PLAN_APPROVAL, _plan_approval_payload(raw_payload, content), "plan_approval")
        if status in _APPROVAL_REVIEW_STATES:
            return SignalRoute(SIGNAL_REVIEW_COMMENT, {"verdict": "approved", "comment": content}, "approval_as_review")
        # unexpected status -> does NOT guess (P6). Consumed with an audit row.
        return SignalRoute(None, None, "unexpected_status")

    return SignalRoute(None, None, "unhandled_kind")


def _is_permanent_signal_failure(exc: BaseException) -> bool:
    """True quando o destino do signal NÃO EXISTE MAIS — retry é inútil por
    construção. Casa as duas mensagens que o Temporal devolve para workflow
    encerrado/inexistente (a segunda foi medida hoje mesmo, no cancel do
    wi_8a7036e7: "workflow not found for ID"). Qualquer outra exceção continua
    transitória → retry."""
    msg = str(exc).lower()
    return "already completed" in msg or "workflow not found" in msg


def _notify_undeliverable(work_item_id: str, reason: str) -> None:
    """Item 4 — a falha de entrega vira MENSAGEM na conversa do item, editando
    a mesma mensagem mutável que o humano clicou (via o endpoint do adapter; o
    writer edita in-place e limpa os botões). Nunca o clique que parece ter
    funcionado e não funcionou.

    Slack apenas nesta rc (GitHub/Jira herdam o mecanismo depois); best-effort
    de ponta a ponta — o aviso jamais derruba o drain."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source, source_ref FROM work_items WHERE id = %s",
                    (work_item_id,),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
        if not row or row[0] != "slack":
            return
        source_ref = row[1] if isinstance(row[1], dict) else {}
        channel = source_ref.get("channel")
        if not channel:
            return
        url = os.environ.get("DSE_ADAPTER_SLACK_URL", "http://adapter-slack:8801")
        import httpx
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0)) as http:
            http.post(
                f"{url}/internal/status-comment",
                json={
                    "work_item_id": work_item_id,
                    "channel": channel,
                    "body": f"⚠️ Could not apply: {reason}",
                    "actor": "system:ingest-gateway-dispatcher",
                },
            ).raise_for_status()
    except Exception:  # noqa: BLE001 — aviso é acessório; a auditoria é a fonte de verdade
        logger.warning("undeliverable notice failed for %s", work_item_id, exc_info=True)


async def _dispatch_row(
    client: Client, *, work_item_id: str, event_id: str, kind: str, status: str | None, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Returns `(outcome, extra_details)`. `extra_details` feeds the audit row
    emitted by `drain_once` (includes the routed signal and the reason)."""
    if kind == _TASK_REQUEST_KIND:
        try:
            await client.start_workflow(
                WORKFLOW_TYPE,
                work_item_id,
                id=work_item_id,
                task_queue=TASK_QUEUE,
            )
            return DispatchOutcome.STARTED, {}
        except WorkflowAlreadyStartedError:
            # Idempotent by design (WSA-E1-T3): a redelivery of the same
            # ingest_event (or a race between 2 dispatchers) is never
            # re-raised as an error.
            return DispatchOutcome.DEDUPED_ALREADY_STARTED, {}

    route = _route_signal(status, kind, payload)

    if route.signal_name is None:
        if route.reason == "unexpected_status":
            # P6 decline-never-truncate: clean boundary, leaves evidence — e,
            # desde o item 4, também AVISO no canal: o decline mudo foi
            # exatamente o que o caso A1 produziu 2x (clique "funcionou",
            # nada aconteceu, ninguém soube).
            _notify_undeliverable(
                work_item_id,
                f"the item is in '{status}' and is not waiting for that verdict",
            )
            return DispatchOutcome.DECLINED_UNEXPECTED_STATUS, {"status": status, "reason": route.reason}
        return DispatchOutcome.IGNORED_NOT_A_DECISION, {"reason": route.reason}

    handle = client.get_workflow_handle(work_item_id)
    try:
        await handle.signal(route.signal_name, route.payload)
        return DispatchOutcome.SIGNALED, {"signal": route.signal_name, "reason": route.reason, "status": status}
    except Exception as exc:
        if _is_permanent_signal_failure(exc):
            # Workflow morto (B8): retry é inútil por construção. Consome com
            # auditoria e avisa o canal — antes disto, o evento ficava
            # `processed=false` PARA SEMPRE, em silêncio.
            logger.warning(
                "signal permanently undeliverable for work_item_id=%s event_id=%s: %s",
                work_item_id, event_id, exc,
            )
            _notify_undeliverable(
                work_item_id,
                "the task has already finished — the verdict can no longer be applied",
            )
            return DispatchOutcome.SIGNAL_PERMANENTLY_UNDELIVERABLE, {
                "signal": route.signal_name, "error": str(exc)[:200],
            }
        logger.exception(
            "signal_workflow failed for work_item_id=%s event_id=%s (will be retried)",
            work_item_id, event_id,
        )
        return DispatchOutcome.SIGNAL_FAILED, {"signal": route.signal_name}


class Dispatcher:
    """Usage: `await Dispatcher(client).drain_once()` — drains one batch (up to
    `batch_size` rows) in a single `FOR UPDATE SKIP LOCKED` transaction.
    Run it in a loop (`run_forever`) as a process/worker separate from the
    adapter."""

    def __init__(self, client: Client, *, batch_size: int = 25, conn_factory=get_connection):
        self._client = client
        self._batch_size = batch_size
        self._conn_factory = conn_factory

    async def drain_once(self) -> int:
        conn = self._conn_factory()
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ie.id, ie.work_item_id, ie.event_id, ie.kind, ie.payload, wi.tenant_id, wi.status
                    FROM ingest_events ie
                    JOIN work_items wi ON wi.id = ie.work_item_id
                    WHERE NOT ie.processed
                    ORDER BY ie.id
                    FOR UPDATE OF ie SKIP LOCKED
                    LIMIT %s
                    """,
                    (self._batch_size,),
                )
                rows = cur.fetchall()

            if not rows:
                conn.rollback()
                return 0

            processed = 0
            for ingest_event_id, work_item_id, event_id, kind, payload, tenant_id, status in rows:
                outcome, extra_details = await _dispatch_row(
                    self._client,
                    work_item_id=work_item_id,
                    event_id=event_id,
                    kind=kind,
                    status=status,
                    payload=payload,
                )

                if outcome == DispatchOutcome.SIGNAL_FAILED:
                    # Does not mark processed — the next drain retries it.
                    # (No backoff/dead-letter in Phase 1 — see README.)
                    continue

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ingest_events SET processed = true, processed_at = now() WHERE id = %s",
                        (ingest_event_id,),
                    )

                audit_emit(
                    actor="system:ingest-gateway-dispatcher",
                    action=f"dispatch_{outcome}",
                    tenant_id=tenant_id,
                    work_item_id=work_item_id,
                    details={"event_id": event_id, "kind": kind, "ingest_event_id": ingest_event_id, **extra_details},
                    conn=conn,
                )
                processed += 1

            conn.commit()
            return processed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def drain_all(self, max_rounds: int = 1000) -> int:
        """Drains until the queue is empty (or `max_rounds` iterations, a guard
        against an infinite loop if something keeps reintroducing work).

        Finding from the Phase 1 integration: stopping at the first empty round
        is safe in production (`run_forever` calls `drain_once` forever, a
        transient empty round only delays — it never loses anything, see
        `dispatcher_main.py`), but it is unstable for this method's "drain
        everything now and tell me you're done" usage under real concurrency:
        with 2+ dispatchers, a round can see 0 rows just because the other
        dispatcher is holding the lock on the last available rows at that
        instant (SKIP LOCKED skips them, it does not count them as
        nonexistent) — that does not mean the queue is empty. Requiring 2
        CONSECUTIVE empty rounds shrinks that race window to a negligible level
        without reintroducing unbounded waiting (P6 decline-never-truncate
        still holds: `max_rounds` is still the hard ceiling). Residual risk
        documented honestly: this is a heuristic, not a formal guarantee — in
        production the dispatcher runs via `run_forever` (continuous loop, never
        calls `drain_all`), so a transient gap here is never permanent loss,
        only a delay until the real process's next round."""
        total = 0
        consecutive_empty = 0
        for _ in range(max_rounds):
            n = await self.drain_once()
            total += n
            if n == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
                await asyncio.sleep(0.05 * consecutive_empty)
            else:
                consecutive_empty = 0
        return total

    async def run_forever(self, poll_interval_seconds: float = 1.0) -> None:  # pragma: no cover - production loop
        import asyncio

        while True:
            n = await self.drain_once()
            if n == 0:
                await asyncio.sleep(poll_interval_seconds)
