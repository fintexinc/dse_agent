"""Contract maps fase1 -> console (Plan 06 §4.4/§4.5).

These maps ARE the interface between the two products — versioned and pinned by
a contract test (test_mappers.py): if an enum changes on either side, CI breaks
here instead of the dashboard breaking in production (lesson from the
remediation).
"""
from __future__ import annotations

from typing import Any

# fase1 WorkItemStatus (17) -> console DseTaskStatus (11) + human-readable phase.
# Never guesses: every fase1 status HAS an entry; a new status without one fails
# the contract test (it is never silently swallowed).
STATUS_MAP: dict[str, tuple[str, str | None]] = {
    "new": ("new", None),
    "needs_clarification": ("needs_clarification", "clarification"),
    "ready": ("ready", None),
    "queued": ("queued", None),
    "awaiting_plan_approval": ("blocked", "plan approval"),
    # Parque da porta 1/beco 1: parado esperando veredito humano — mesma
    # família do plan approval. Faltou quando o status entrou no enum (rc.46);
    # o KeyError deixou o control-plane vermelho DETERMINÍSTICO por dias,
    # mascarado pela fama de flaky do grupo — e o console congelado na última
    # projeção para todo item parqueado.
    "implementing": ("running", "coding"),
    "validating": ("running", "validating"),
    "pr_open": ("pr_ready", "pr open"),
    "ci_pending": ("pr_ready", "ci pending"),
    "review_ready": ("pr_ready", "review"),
    "merge_pending": ("pr_ready", "merge pending"),
    "pr_ready": ("pr_ready", "review"),
    "review_feedback": ("review_feedback", "fixing review"),
    "done": ("done", None),
    "blocked": ("blocked", "no approver"),
    "failed": ("failed", None),
    "escalated": ("blocked", "escalated"),
}

# audit_log.action -> console TimelineEvent.type. Actions outside the map become
# `note` (never silence — the "never without a state description" principle).
AUDIT_EVENT_MAP: dict[str, str] = {
    "work_item_admitted": "created",
    "clarification_requested": "clarification_requested",
    "clarification_reminder_sent": "clarification_requested",
    "clarification_complete": "clarification_answered",
    "planner_turn_completed": "plan",
    "planner_completed": "plan",
    "plan_auto_approved": "plan",
    "awaiting_plan_approval": "plan",
    # O plano precisa escrever em caminho protegido e por isso PARA no gate,
    # qualquer que seja a classe de risco. Sem esta linha cairia em `note`, que
    # o console rotula como observação — e isto é decisão de gate, não remark.
    "plan_requires_protected_paths": "plan",
    "planner_contract_rejected": "error",
    "coder_turn_completed": "file_change",
    "coder_fix_applied": "file_change",
    "tester_turn_completed": "test_result",
    "l1_pipeline_run": "test_result",
    "l1_completed": "test_result",
    "ci_status_observed": "test_result",
    "l2_review_completed": "feedback",
    "changes_requested": "feedback",
    "review_decision_signaled": "feedback",
    "pr_finalized": "pr_opened",
    "pr_refinalized": "pr_opened",
    "pr_adopted": "pr_opened",
    "status_comment_upserted": "comment",
    "tracking_comment_posted": "comment",
    "merged_by_human": "status_changed",
    "escalated": "error",
    # ingest-gateway's stranded sweep (`stranded.STRANDED_ESCALATION_ACTION`, and
    # that side's test_stranded.py fails if this entry stops matching it): the work
    # item's workflow no longer exists and a human now owns it. The outcome is the
    # same as the orchestrator's own `escalated` above — item terminal, work handed
    # over — so it gets the same event type. Left out of this map it fell through
    # to `note`, the fallback for actions nobody classified, which reads as a
    # remark; the console prints the event type as the timeline entry's label, so
    # this is the word next to the message an on-call reader sees.
    "work_item_escalated_stranded": "error",
    "cancelled_by_operator": "error",
    # The Tester turn ended by something the runtime imposed (OOM kill, a suite
    # that never terminated, the worker's own exec deadline) rather than by an
    # assertion. It either escalates the item to a human or spends the one paid
    # Coder turn a hang is allowed — both are errors, and the whole point of the
    # row is that a person reads it. Unmapped it fell through to `note`, the
    # bucket for actions nobody classified, which the console labels as a remark.
    "tester_infra_outcome": "error",
    # A Coder turn that burned tokens and THEN failed. Temporal may still retry
    # it, so the item is not necessarily lost — but real money was spent with
    # nothing to show, which is the definition of an error on this timeline.
    "coder_turn_failed_after_spend": "error",
    "coder_retry_cap_exhausted": "error",
    "activity_retries_exhausted": "error",
    "model_path_fail_closed": "error",
    "budget_exhausted": "error",
}

# Whitelist, so an arbitrary details payload can never dump itself into a
# timeline message. `status_before`/`idle_seconds` are here for the stranded
# escalation: "no_live_workflow" alone does not tell the reader that the item had
# been silent for 40 hours in `implementing`, which is the whole story.
_DETAIL_KEYS = (
    "reason", "pr_number", "url", "cost_usd", "status", "risk_class", "passed",
    "status_before", "idle_seconds",
)


def map_status(fase1_status: str) -> tuple[str, str | None]:
    """(console_status, current_phase). The KeyError on an unmapped new status is
    deliberate — the projector logs it and keeps the last projection
    (fail-visible)."""
    return STATUS_MAP[fase1_status]


def map_audit_event(action: str, details: dict[str, Any] | None) -> tuple[str, str]:
    """(event_type, human-readable message). Deterministic; no LLM involved (P1)."""
    ev_type = AUDIT_EVENT_MAP.get(action, "note")
    details = details or {}
    extras = ", ".join(f"{k}={details[k]}" for k in _DETAIL_KEYS if details.get(k) not in (None, ""))
    message = action.replace("_", " ")
    if extras:
        message = f"{message} ({extras})"
    return ev_type, message


def split_title(content: str, fallback: str) -> tuple[str, str]:
    """Title = first non-empty line (<=120 chars); description = the rest."""
    lines = [ln for ln in (content or "").strip().splitlines()]
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    if not first:
        return fallback, ""
    rest = "\n".join(lines[1:]).strip()
    return first[:120], rest
