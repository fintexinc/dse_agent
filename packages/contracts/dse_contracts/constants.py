"""Cross-workstream integration constants — fixed names everybody uses so that
no real-time coordination is needed."""

TASK_QUEUE = "dse-core-task-queue"
WORKFLOW_TYPE = "WorkItemLifecycleWorkflow"

# Temporal signal names of the WorkItemLifecycleWorkflow (services/orchestrator/
# src/dse_orchestrator/workflows.py) — the Temporal Python SDK uses the
# `@workflow.signal` method name as the signal name by default (without an
# explicit `name=`), so these strings must match the method names over there
# exactly. Found during the Phase 1 integration: the ingest-gateway (WS-A) and
# the review_signal (WS-E) each had their own signal-name constant
# (`"conversation_signal"` and `"review_decision"`, respectively) that matched
# NO real `@workflow.signal` — every signal they sent was silently dropped by
# Temporal (a name with no registered handler is not an error, it just triggers
# nothing). Promoted here as the single source of truth; both workstreams were
# fixed to import from here.
SIGNAL_CLARIFICATION_ANSWER = "clarification_answer"
SIGNAL_REVIEW_COMMENT = "review_comment"
SIGNAL_MERGED_BY_HUMAN = "merged_by_human"

# Phase 2 (addendum 01 §2.2): plan approval gate signal (WSB-E3-T2).
# Payload: {"verdict": "approved"|"rejected", "route": "re_plan"|"re_clarify"|
# "cancel" (required when rejected — WSB-E3-T3), "comment": str,
# "actor": resolved principal}. The WS-A dispatcher routes `kind=approval` to
# this signal when work_items.status == 'awaiting_plan_approval'
# (WSA-E6-T3) — never again by `kind` alone.
SIGNAL_PLAN_APPROVAL = "plan_approval"


# Known limitation (document it, do not hide it — P8): correctly routing a
# correlated ConversationEvent (Path B) should depend on the CURRENT state of
# the WorkItem (waiting for clarification? waiting for review?), not only on
# the event `kind` — a `kind=approval` coming from a Slack button, for example,
# is today heuristically mapped to SIGNAL_REVIEW_COMMENT in the WS-A dispatcher
# because Phase 1 has no plan approval gate (that is Phase 2, WSB-E3-T2) —
# if/when it exists, this mapping has to consult `work_items.status` instead of
# deciding by `kind` alone.

# OTel span attributes that WSD-E3 (model-gateway) emits and WSF-E7
# (dashboards/alerting) consumes — a contract between WS-D and WS-F without
# them having to run together.
OTEL_ATTR_TENANT = "dse.tenant_id"
OTEL_ATTR_WORK_ITEM = "dse.work_item_id"
OTEL_ATTR_STAGE = "dse.stage"
OTEL_ATTR_MODEL = "dse.model"
OTEL_ATTR_COST_USD = "dse.cost_usd"
OTEL_ATTR_TOKENS_IN = "dse.tokens_in"
OTEL_ATTR_TOKENS_OUT = "dse.tokens_out"
# Promoted from WS-D in Phase 2 (addendum 01 §4) — it was already emitted as the
# ad-hoc attribute "dse.task_class" in Phase 1.
OTEL_ATTR_TASK_CLASS = "dse.task_class"
