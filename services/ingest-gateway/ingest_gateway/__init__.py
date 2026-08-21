"""WS-A ingest-gateway: transactional gateway (outbox), Temporal dispatcher,
intake defenses (signature, TOCTOU snapshot, sanitization), Path A/B
correlation and steering allowlist.

Reused as a library by the adapters (services/adapter-slack,
services/adapter-github) — they call `admit_work_item`/`correlate` directly
against the same Postgres, which keeps the adapters 100% stateless (no state
lives in the adapter process).
"""
from .db import get_connection
from .kill_switch import is_channel_killed
from .gateway import admit_work_item, record_signal_event, AdmissionBlocked, NonTaskAdmissionRefused
from .correlate import correlate, CorrelationResult
from .security import (
    verify_slack_signature,
    verify_github_signature,
    verify_jira_signature,
    verify_teams_signature,
    SignatureCheck,
)
from .sanitize import sanitize_content
from .tenant_binding import resolve_tenant, default_tenant, ResolvedTenant
from .repo_resolver import resolve_repo, parse_explicit_repo
from .task_class import classify_task_class, TASK_CLASSES
from .reconcile import (
    recorded_work_item_id,
    pending_reply_work_items,
    reset_reply_sweep_cursor,
    RECOVERABLE_STATUSES,
    NON_RECOVERABLE_STATUSES,
)
# The status tuples keep their `STRANDED_` prefix at package level: `correlate`
# has its own, incompatible notion of "terminal" (done/failed — "can this still
# receive a signal?") and a bare `TERMINAL_STATUSES` in this namespace would let
# a caller import one while meaning the other.
from .stranded import (
    stranded_work_items,
    escalate_stranded,
    STRANDED_TERMINAL_STATUSES,
    STRANDED_HUMAN_WAIT_STATUSES,
    STRANDED_ESCALATION_ACTION,
    STRANDED_ESCALATION_REASON,
)

__all__ = [
    "recorded_work_item_id",
    "pending_reply_work_items",
    "reset_reply_sweep_cursor",
    "RECOVERABLE_STATUSES",
    "NON_RECOVERABLE_STATUSES",
    "stranded_work_items",
    "escalate_stranded",
    "STRANDED_TERMINAL_STATUSES",
    "STRANDED_HUMAN_WAIT_STATUSES",
    "STRANDED_ESCALATION_ACTION",
    "STRANDED_ESCALATION_REASON",
    "get_connection",
    "is_channel_killed",
    "admit_work_item",
    "record_signal_event",
    "AdmissionBlocked",
    "NonTaskAdmissionRefused",
    "correlate",
    "CorrelationResult",
    "verify_slack_signature",
    "verify_github_signature",
    "verify_jira_signature",
    "verify_teams_signature",
    "SignatureCheck",
    "sanitize_content",
    "resolve_tenant",
    "resolve_repo",
    "parse_explicit_repo",
    "classify_task_class",
    "TASK_CLASSES",
    "default_tenant",
    "ResolvedTenant",
]
