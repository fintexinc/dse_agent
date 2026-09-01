"""WSF-E6-T2 — queue board operator controls, wired to Temporal signals +
durable state, EVERY ACTION AUDITED with the operator's identity (P8).

`OperatorConsole` takes a `SignalSender` (real or fake) and an `operator`
(console principal_id resolved via SSO — never a raw platform_user_id).
All the controls from the spec:
  pause / resume / cancel / retry_from_checkpoint / reassign_model /
  reassign_runtime / force_clarification / escalate / quarantine / release /
  kill switches (global, tenant, channel, task).

Order for every control: (1) audit the operator's INTENT (`operator_action`,
with actor = operator), (2) send the signal / write the durable state. If the
signal fails, the failure is propagated (fail-closed, P6) — but the intent audit
is already recorded (the evidence that the operator tried is never lost).
"""
from __future__ import annotations

from dse_audit import emit

from .. import kill_switches
from .api import _get_connection
from .signals import (
    SIGNAL_CANCEL,
    SIGNAL_ESCALATE,
    SIGNAL_FORCE_CLARIFICATION,
    SIGNAL_PAUSE,
    SIGNAL_REASSIGN_MODEL,
    SIGNAL_REASSIGN_RUNTIME,
    SIGNAL_RESUME,
    SIGNAL_RETRY_FROM_CHECKPOINT,
    SignalSender,
)


#: A signal nobody can receive. Same two shapes `ingest_gateway.dispatcher`
#: treats as permanent (B8): the execution finished, or never existed here.
def _is_permanent_signal_failure(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "already completed" in msg or "not found" in msg


#: Terminal statuses the cancel must never overwrite — a finished item stays
#: finished. Mirrors `stranded.STRANDED_TERMINAL_STATUSES`; kept as literals
#: because this package does not import the gateway.
_TERMINAL_STATUSES = ("done", "failed", "blocked", "escalated", "cancelled")


def mark_cancelled(*, work_item_id: str, tenant_id: str, reason: str | None, actor: str) -> bool:
    """status -> `cancelled` for an item whose workflow is gone, plus one audit
    row. Returns True when this call changed the row; False when the item was
    already terminal (nothing to do, nothing written)."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE work_items
                SET status = 'cancelled',
                    last_transition_at = now(),
                    state_version = state_version + 1
                WHERE id = %s AND tenant_id = %s AND status <> ALL(%s)
                """,
                (work_item_id, tenant_id, list(_TERMINAL_STATUSES)),
            )
            changed = cur.rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if changed:
        emit(
            actor=actor,
            action="cancelled_by_operator",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"reason": reason, "operator": actor, "workflow": "not_reachable"},
        )
    return changed


class OperatorConsole:
    def __init__(self, sender: SignalSender, *, operator: str):
        if not operator or not (operator.startswith("usr_") or operator.startswith("system:")):
            raise ValueError(
                "operator must be a resolved principal (usr_...) or system:<component> — "
                "never a raw platform_user_id (P8)"
            )
        self._sender = sender
        self._operator = operator

    # -- helper: audit the operator's intent (always before the effect) --
    def _audit_action(self, action: str, work_item_id: str | None, tenant_id: str, details: dict) -> None:
        emit(
            actor=self._operator,
            action=f"operator_{action}",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={**details, "operator": self._operator},
        )

    def _signal(self, work_item_id: str, name: str, arg=None) -> None:
        # workflow_id == work_item_id (foundation contract)
        self._sender.signal(work_item_id, name, arg)

    # ------------------------------------------------------------------
    # Controls wired to workflow signals (TASK scope)
    # ------------------------------------------------------------------
    def pause(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("pause", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_PAUSE, reason)

    def resume(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("resume", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_RESUME, reason)

    def cancel(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("cancel", work_item_id, tenant_id, {"reason": reason})
        try:
            self._signal(work_item_id, SIGNAL_CANCEL, reason)
        except Exception as exc:  # noqa: BLE001 — só a classe permanente é tratada
            if not _is_permanent_signal_failure(exc):
                raise
            # rc.130. The workflow is gone (terminated, purged, stranded) and
            # nobody will ever consume the signal — this is exactly the case
            # that used to send an operator to psql to write `cancelled` by
            # hand: 33 rows in production carried a value the enum did not
            # have, and the stranded sweep re-escalated each of them. The
            # cancel is the operator's decision either way; the row records
            # it, with the same terminal-status guard `stranded.escalate_stranded`
            # uses, and one audit row — the effect, after the intent above.
            mark_cancelled(
                work_item_id=work_item_id, tenant_id=tenant_id,
                reason=reason, actor=self._operator,
            )

    def retry_from_checkpoint(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("retry_from_checkpoint", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_RETRY_FROM_CHECKPOINT, reason)

    def force_clarification(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("force_clarification", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_FORCE_CLARIFICATION, reason)

    def escalate(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("escalate", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_ESCALATE, reason)

    def reassign_model(self, work_item_id: str, tenant_id: str, *, model: str) -> None:
        if not model:
            raise ValueError("reassign_model requires `model`")
        self._audit_action("reassign_model", work_item_id, tenant_id, {"model": model})
        self._signal(work_item_id, SIGNAL_REASSIGN_MODEL, model)

    def reassign_runtime(self, work_item_id: str, tenant_id: str, *, runtime: str) -> None:
        if not runtime:
            raise ValueError("reassign_runtime requires `runtime`")
        self._audit_action("reassign_runtime", work_item_id, tenant_id, {"runtime": runtime})
        self._signal(work_item_id, SIGNAL_REASSIGN_RUNTIME, runtime)

    # ------------------------------------------------------------------
    # Quarantine (TASK scope, with durable state + pause)
    # ------------------------------------------------------------------
    def quarantine(self, work_item_id: str, tenant_id: str, *, reason: str) -> None:
        """Writes the durable quarantine (survives restart) AND pauses the
        workflow. The quarantine is recorded even if the pause fails (the durable
        flag is the guarantee; the pause is best-effort to stop in-flight work)."""
        if not reason:
            raise ValueError("quarantine requires `reason` (P8)")
        self._audit_action("quarantine", work_item_id, tenant_id, {"reason": reason})
        kill_switches.quarantine_work_item(work_item_id, tenant_id, reason=reason, actor=self._operator)
        self._signal(work_item_id, SIGNAL_PAUSE, f"quarantined: {reason}")

    def release_quarantine(self, work_item_id: str, tenant_id: str, *, resume: bool = True) -> None:
        self._audit_action("release_quarantine", work_item_id, tenant_id, {"resume": resume})
        kill_switches.release_quarantine(work_item_id, tenant_id, actor=self._operator)
        if resume:
            self._signal(work_item_id, SIGNAL_RESUME, "quarantine_released")

    # ------------------------------------------------------------------
    # Kill switches across the 4 scopes
    # ------------------------------------------------------------------
    def kill_switch_global(self, *, enabled: bool, reason: str | None) -> None:
        self._audit_action("kill_switch_global", None, "platform", {"enabled": enabled, "reason": reason})
        kill_switches.set_global_kill_switch(enabled=enabled, reason=reason, actor=self._operator)

    def kill_switch_tenant(self, tenant_id: str, *, enabled: bool, reason: str | None) -> None:
        self._audit_action("kill_switch_tenant", None, tenant_id, {"enabled": enabled, "reason": reason})
        kill_switches.set_tenant_kill_switch(tenant_id, enabled=enabled, reason=reason, actor=self._operator)

    def kill_switch_channel(self, tenant_id: str, channel: str, *, active: bool, reason: str | None) -> None:
        self._audit_action("kill_switch_channel", None, tenant_id, {"channel": channel, "active": active, "reason": reason})
        kill_switches.set_channel_kill_switch(tenant_id, channel, active=active, reason=reason, actor=self._operator)

    def kill_switch_task(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        """TASK-scope kill switch = cancel the workflow (clean stop at a
        boundary, P6 — WS-B's cancel tears down the sandbox and marks failed)."""
        self.cancel(work_item_id, tenant_id, reason=reason or "task_kill_switch")
