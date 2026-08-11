"""WorkItem (control plane, §10.3) and the WSA-E1-T4 public API
(`DseTaskRequest`/`DseTaskStatus`) — a deliberately coarser projection of the
internal state.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class WorkItemStatus(str, Enum):
    """State machine §9.3."""

    new = "new"
    needs_clarification = "needs_clarification"
    ready = "ready"
    queued = "queued"
    # Phase 2 (WSB-E3-T2): plan approval gate by risk class. Durable state in
    # which the WorkItem parks waiting for a human to approve the high-risk
    # plan (WS-A was already routing `SIGNAL_PLAN_APPROVAL` based on it — the
    # constant in constants.py referenced this state before it existed in the
    # enum; foundation gap fixed during the Phase 2 integration).
    awaiting_plan_approval = "awaiting_plan_approval"
    implementing = "implementing"
    validating = "validating"
    # Fine-grained states of the PR/CI/review path. ``pr_ready`` stays below as
    # a historical wire alias: old Temporal histories and clients that already
    # persisted it keep decoding, but new executions use
    # pr_open -> ci_pending -> review_ready -> merge_pending.
    pr_open = "pr_open"
    ci_pending = "ci_pending"
    review_ready = "review_ready"
    merge_pending = "merge_pending"
    pr_ready = "pr_ready"
    review_feedback = "review_feedback"
    done = "done"
    blocked = "blocked"
    failed = "failed"
    escalated = "escalated"


# Single map from internal state -> coarse public state (WSA-E1-T4). Adding a
# new internal state here is mandatory (the contract test fails if some
# WorkItemStatus is missing from the map) — never leave it implicit.
PublicStatus = Literal["running", "blocked", "done", "failed"]

_PUBLIC_STATUS_MAP: dict[WorkItemStatus, PublicStatus] = {
    WorkItemStatus.new: "running",
    WorkItemStatus.needs_clarification: "blocked",
    WorkItemStatus.ready: "running",
    WorkItemStatus.queued: "running",
    # awaiting_plan_approval -> "blocked" (§10.3: "AwaitingPlanApproval /
    # Escalated -> blocked with the reason in the WorkItem detail").
    WorkItemStatus.awaiting_plan_approval: "blocked",
    WorkItemStatus.implementing: "running",
    WorkItemStatus.validating: "running",
    WorkItemStatus.pr_open: "running",
    WorkItemStatus.ci_pending: "running",
    WorkItemStatus.review_ready: "running",
    WorkItemStatus.merge_pending: "blocked",
    WorkItemStatus.pr_ready: "running",
    WorkItemStatus.review_feedback: "running",
    WorkItemStatus.done: "done",
    WorkItemStatus.blocked: "blocked",
    WorkItemStatus.failed: "failed",
    WorkItemStatus.escalated: "blocked",
}


def to_public_status(status: WorkItemStatus) -> PublicStatus:
    try:
        return _PUBLIC_STATUS_MAP[status]
    except KeyError as e:  # pragma: no cover - defensive, covered by contract test
        raise ValueError(f"WorkItemStatus {status} has no public projection defined") from e


class WorkItem(BaseModel):
    id: str  # doubles as the Temporal workflow_id
    tenant_id: str
    source: Literal["slack", "github", "jira"]
    source_ref: dict[str, Any]
    repo: str | None = None
    base_branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    requester: str  # resolved principal
    data_class: str = "internal"
    status: WorkItemStatus = WorkItemStatus.new
    risk_class: str | None = None
    plan: dict[str, Any] | None = None
    plan_hash: str | None = None
    expected_files: list[str] = Field(default_factory=list)
    pr_number: int | None = None
    pr_url: str | None = None
    ci_status: str | None = None
    state_version: int = 0
    last_error: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DseTaskRequest(BaseModel):
    """Public intake type — what an external consumer sees when asking for a task."""

    tenant_id: str
    source: Literal["slack", "github", "jira"]
    repo: str | None = None
    requester: str
    idempotency_key: str
    content_snapshot: str


class DseTaskStatus(BaseModel):
    """Public status type — consumed by the admin queue board (Phase 2) and adapters."""

    work_item_id: str
    status: PublicStatus
    detail: str | None = None  # reason, present when status == blocked/failed
    pr_number: int | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_work_item(cls, wi: WorkItem, detail: str | None = None) -> "DseTaskStatus":
        return cls(
            work_item_id=wi.id,
            status=to_public_status(wi.status),
            detail=detail,
            pr_number=wi.pr_number,
            updated_at=wi.updated_at,
        )


class MergedByHumanSignal(BaseModel):
    """Minimal envelope the workflow accepts to conclude a merge.

    The adapter already validates the signature and correlates tenant/repo/PR;
    the workflow still requires a resolved human identity and the same PR it is
    tracking. The extra fields allow a stronger verification without breaking
    the current payload or old histories.
    """

    merged_by: str
    pr_number: int
    repo: str | None = None
    head_sha: str | None = None
    merge_sha: str | None = None

    @field_validator("merged_by")
    @classmethod
    def _human_actor_required(cls, value: str) -> str:
        actor = value.strip()
        if not actor or actor.startswith("system:"):
            raise ValueError("merged_by must be a resolved human principal")
        return actor

    @field_validator("pr_number")
    @classmethod
    def _positive_pr(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("pr_number must be positive")
        return value
