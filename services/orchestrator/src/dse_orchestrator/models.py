"""Types that cross the workflow boundary (`@workflow.run` input/output and
`continue_as_new`). Plain dataclasses (not pydantic) on purpose: the workflow
runs inside the Temporal Python SDK's deterministic sandbox, and dataclasses
of primitive types are the best-supported path for the default data converter
without extra plugins. Rich pydantic types (WorkItem, PlanArtifact, the types
in `dse_contracts.activities`) are used freely in *Activity params/returns*,
which run outside the sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Coarse workflow phases — each one closes with `continue_as_new` to keep the
# event history small (WSB-E2-T1).
# ---------------------------------------------------------------------------
PHASE_INTAKE = "intake"
PHASE_IMPLEMENTATION = "implementation"
PHASE_REVIEW = "review"
PHASE_TERMINAL = "terminal"


@dataclass
class WorkItemLifecycleInput:
    """State carried across `continue_as_new` between phases. `status` uses the
    (string) values of `dse_contracts.work_item.WorkItemStatus`."""

    work_item_id: str
    tenant_id: str
    requester: str
    repo: str | None = None
    base_branch: str | None = None
    #: This request was routed to more than one repository. CARRIED, never
    #: recomputed, so it survives continue_as_new deterministically and an old
    #: history — which has no such key — replays as False.
    cross_repo: bool = False
    data_class: str = "internal"
    acceptance_criteria: str | None = None
    # S1 (Phase 5): issue title+body (already sanitized) — WHAT to build.
    # Loaded from ingest_events.payload by load_work_item; passed as the
    # Planner/Coder instruction. Without it the agents did not know the task.
    task_content: str = ""
    # Source issue number (from work_items.source_ref, via load_work_item).
    # finalize uses it as issue_ref -> "Closes #N" in the PR body (back-link +
    # auto-close on merge). Without it the PR went out with "(no linked source
    # issue)" even when it came from an issue.
    issue_number: int | None = None

    phase: str = PHASE_INTAKE
    status: str = "new"

    clarification_rounds: int = 0
    coder_retry_count: int = 0
    review_round: int = 0

    sandbox_id: str | None = None
    # S7 (Phase 5): serialized SandboxHandle (sandbox_id, work_item_id,
    # tenant_id, branch, container_id) retained from provision. L1 and finalize
    # need the COMPLETE handle (not just sandbox_id) to resolve the executor —
    # the old call site only kept sandbox_id, causing `Failed decoding arguments`
    # (missing RunL1PipelineInput.sandbox). Survives continue_as_new.
    sandbox_handle: dict = field(default_factory=dict)
    # Last successful CheckpointRef (dict) — the rebuild source. Lives in the
    # INPUT (not in an instance attribute) so it survives continue_as_new: a
    # rebuild in the review phase restores from the implementation checkpoint.
    last_checkpoint_ref: dict = field(default_factory=dict)
    branch: str | None = None
    # Immutable SHAs delimit every gate. Defaults preserve old histories; new
    # executions capture base_sha before the first turn and head_sha at every
    # checkpoint prior to L1.
    base_sha: str | None = None
    head_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    ci_status: str | None = None
    state_version: int = 0

    # accumulated textual payload of clarification answers (for the Coder to
    # eventually consume via Activity — Phase 1 does not interpret it with an
    # LLM inside the workflow, it only forwards it; P1: no flow decision by LLM).
    clarification_notes: list[str] = field(default_factory=list)

    terminal_detail: str | None = None

    # ------------------------------------------------------------------
    # Phase 2 — session split + plan approval gate (extended WSB-E2-T3 /
    # WSB-E3-T2/T3). These survive `continue_as_new` like the rest of the
    # workflow's deterministic state.
    # ------------------------------------------------------------------
    # Serialized PlanArtifact (produced by the read-only Planner BEFORE the
    # gate). Passed to Reviewer L2 together with the final diff — ONLY this +
    # the diff, never the Coder's history (P3).
    plan_json: dict = field(default_factory=dict)
    plan_hash: str | None = None
    # EFFECTIVE risk class (planner + deterministic defense-in-depth
    # classification — see policy.classify_risk). Drives the gate.
    risk_class: str = "low"
    # approvers resolved by the CODEOWNERS -> access bundle (WS-F) cascade,
    # filled in when the gate parks at awaiting_plan_approval.
    approvers: list[str] = field(default_factory=list)
    plan_rounds: int = 0  # how many times the Planner ran (re_plan increments)

    # Reviewer L2 (fresh context) — objections go back to the Coder, capped.
    l2_objections: list[str] = field(default_factory=list)
    l2_retry_count: int = 0

    # Why the LAST automated attempt failed: the tester's output, or the L1
    # checks that did not pass. Carried into the next Coder turn exactly as the
    # L2 objections above already were.
    #
    # Without this the two loops were not symmetric, and the difference decided
    # whether a task could ever recover. The human-review loop appended its
    # comments; the tester/L1 loop re-sent the original request byte for byte,
    # so a task that failed once failed identically until the cap — four rounds
    # of the same instruction, the last one changing no files at all. Cleared on
    # a pass so a later attempt is never told about a failure that is fixed.
    fix_context: list[str] = field(default_factory=list)

    # Porta 1 v2 (falso-negativo do wi_8edaef39): a união dos files_changed de
    # TODOS os turnos de Coder deste item — o proxy determinístico do diff
    # acumulado base..HEAD (o Coder é o único que escreve código de produção;
    # edição de teste é revertida). O detector de spec_conflict compara o
    # sujeito da spec contra ISTO, nunca contra o diff do turno corrente: um
    # retry que toca outro arquivo tirava o sujeito do diff-por-turno e cegava
    # o re-flag. Sobrevive a continue_as_new por estar no input.
    cumulative_files_changed: list[str] = field(default_factory=list)

    # Porta 1 v3: specs do cliente em conflito que já GANHARAM a primeira
    # chance do Coder (retry automático com as asserções). A reincidência de
    # qualquer uma delas parqueia. No input para sobreviver a continue_as_new —
    # perder isto daria chances infinitas e nunca parquearia.
    spec_conflict_deferred_specs: list[str] = field(default_factory=list)



    # ------------------------------------------------------------------
    # Phase 2 — budgets (WSB-E4-T1). `budget_max_usd` comes from the JSONB
    # `work_items.budget` (key "max_usd") read at admission; `spent_usd`
    # accumulates the cost reported by the gateway (WS-D) on every model
    # Activity. Checked at admission and at EVERY phase boundary — it never
    # cuts mid-Activity (P6).
    # ------------------------------------------------------------------
    budget_max_usd: float | None = None
    spent_usd: float = 0.0
    # Set once the deployment default has been RESOLVED (the resolve_budget_cap
    # Activity). It only matters in the DISABLED case: when the default is a real
    # number, `budget_max_usd is not None` already short-circuits every later
    # boundary. When an operator sets the env to 0 the cap stays None, and
    # without this flag all nine boundaries would re-schedule the Activity and
    # inflate the event history. Deliberately NOT set when the Activity fails, so
    # the resolution is retried at the next boundary instead of failing open for
    # the rest of the run.
    budget_default_resolved: bool = False
    # rc.89: resultado de estimate_plan_cost no gate de aprovação (mediana
    # histórica em USD). None = indisponível/não estimado. Vive no input para
    # o lembrete re-renderizar o MESMO número após continue_as_new.
    cost_estimate: dict | None = None
    #: Os arquivos do plano que caem sob `forbidden_paths` — a contradição que
    #: FORÇA o gate humano (patch `protected-paths-need-approval-v1`). Vive no
    #: input pelo mesmo motivo do `cost_estimate`: o lembrete re-renderiza a
    #: MESMA lista depois de um continue_as_new. Lista vazia = plano sem
    #: colisão, que é o caso de sempre.
    protected_paths: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Configurable caps/timers (WSB-E3-T1 / E2-T3 / E5-T1). They are part of the
    # workflow INPUT (not env vars read inside the sandbox) so that:
    #  (a) they are deterministic and survive `continue_as_new`/replay;
    #  (b) tests can inject short timers without monkeypatching env inside the
    #      workflow sandbox (which is neither safe nor supported).
    # `dse_orchestrator.config.OrchestratorConfig.from_env()` is the helper that
    # whoever starts the workflow (worker/WS-A dispatcher) uses to fill these
    # fields from the production environment.
    # ------------------------------------------------------------------
    clarification_round_cap: int = 3
    clarification_reminder_hours: float = 24.0
    clarification_escalation_days: float = 3.0
    coder_retry_cap: int = 3
    checkpoint_retry_cap: int = 2
    rebuild_retry_cap: int = 1
    activity_start_to_close_seconds: float = 3600.0
    # Remediation (spec §6): the substrate emits a periodic heartbeat during
    # long turns (sandbox_runtime.activity_heartbeat.run_sync_with_heartbeat).
    # 2026-07-23 (real runs wi_d05c/wi_257d/wi_150d): at 60s, coder/tester turns
    # with model calls fell into an INFINITE retry LOOP — the server cancelled
    # on heartbeat timeout an ACTIVITY THAT WAS STILL WORKING (and the retry
    # paid for the model again). 600s gives real slack; start_to_close (1h)
    # remains the hard ceiling of the turn. NOTE: this default IS the operative
    # value on the dispatcher path (start_workflow with a string → _coerce_input
    # uses the model default; apply_to_input/env only applies to callers that
    # pass the full input).
    activity_heartbeat_seconds: float = 600.0
    activity_schedule_to_close_seconds: float = 7200.0
    # Retries of business Activities must terminate. schedule-to-close remains
    # the time ceiling, and this cap bounds attempts/cost.
    activity_retry_cap: int = 3

    # CI pending is a durable wait, not permission to review. The default cap
    # equals 24h at one poll per minute; both live in the input for
    # deterministic replay and can be shortened in tests.
    ci_poll_interval_seconds: float = 60.0
    ci_pending_poll_cap: int = 1440
    ci_pending_polls: int = 0
    # Wall clock on the CI wait. `ci_pending_poll_cap` bounds a COUNT, which is
    # only a duration if every poll is fast and the worker never restarts — and
    # the counter resets on every CI-red fix cycle, so a flapping item can
    # outlive any nominal window indefinitely. These two bound the elapsed time
    # instead. Like the caps below, they are literals rather than env vars
    # because `config.apply_to_input` has no production caller (the dispatcher
    # starts the workflow with a bare work-item id, so this dataclass is built
    # from its defaults) — a DSE_CI_* env var would be dead on arrival.
    # <= 0 disables the deadline.
    ci_wait_started_at_epoch: float | None = None
    ci_wait_deadline_hours: float = 6.0
    # Last CI status the workflow wrote an audit row for. Edge detector for the
    # CI wait: the row is written when the wait STARTS and on every real
    # transition, never once per poll. It has to be here rather than in an
    # instance attribute because the wait now survives `continue_as_new` — an
    # instance attribute would restart every chained run announcing the entry
    # into a wait it never left.
    ci_last_audited_status: str | None = None
    # How often the CI wait re-writes the work_items row while nothing changes.
    # Stopping the per-poll write was right — 45 identical rows to move one
    # integer — but stopping it ENTIRELY left `validation_attempts.ci_pending`
    # reading 0 for the whole wait, so a panel opened at minute 30 of a
    # 45-minute wait showed a wait that had just started. That is not a stale
    # number, it is a wrong one. One write per 10 polls is the middle: an
    # `update_work_item_status` costs 6 history events against the 17 a whole
    # pending pass costs, so this adds 0.6 events per poll (17.0 -> 17.6, +3.5%)
    # and takes the passes that fit under Temporal's 51_200-event ceiling from
    # 3_011 to 2_909, while `ci_pending_poll_cap` (1440) still wants only
    # ~25_300. The per-poll cost and that last relation are MEASURED by
    # test_ci_wait_event_budget.py rather than trusted from here. In exchange the
    # row is never more than 10 polls behind — 10 MINUTES only because
    # `ci_poll_interval_seconds` is 60 and, like every knob here, has no live
    # env path; the two defaults are what the promise is made of, so
    # test_ci_wait_periodic_persist.py asserts their product.
    # In the INPUT rather than a module constant for the reason the caps above
    # are: it decides how many commands the loop issues, so an execution must
    # keep the value it started with. A constant edited later would change the
    # command stream of a wait that is REPLAYING and desync it.
    # <= 0 disables the periodic write (back to writing only at the edges).
    ci_wait_persist_every_n_polls: int = 10
    # Event count at which the CI wait closes the run with `continue_as_new`.
    # Temporal terminates an execution at 51_200 events with no audit row and no
    # escalation, and `ci_pending_poll_cap` bounds a count of POLLS, not of
    # events — at ~41 events per poll the ceiling arrived at poll ~1237, before
    # the 1440-poll cap could ever fire. 40_000 sits deliberately inside the band
    # infra/ALERTING-RULES.md §3 already defines: above the warning at 35_840
    # (70%), below the critical at 46_080 (90%). That is the behaviour the rule
    # was written for — "leaving time for the workflow to Continue-As-New before
    # hitting the hard limit" — so a long wait trips the warning and heals
    # itself, and a CRITICAL now means the mitigation did not fire (a signal
    # parked in instance state, or history growing outside this loop). It also
    # leaves ~11_000 events for the fix cycle + evidence refresh + merge that may
    # follow the wait, which costs a few hundred.
    history_continue_as_new_threshold: int = 40_000

    # Phase 2 (WSB-E3-T2/E3-T3/E4) — new caps/policy. `require_approval_risk_classes`
    # is the POLICY of which risk classes require human approval; it lives in the
    # INPUT (outside the model — P1), filled by config.from_env by the caller.
    require_approval_risk_classes: tuple[str, ...] = ("high",)
    plan_round_cap: int = 3  # capped re_plan (rejection path — WSB-E3-T3)
    l2_retry_cap: int = 2  # L2 objections -> Coder, capped

    # Deadline of the awaiting_plan_approval gate. Before this existed the gate
    # was an UNBOUNDED park: a plan nobody answered sat there forever, nobody
    # was reminded, and the console rendered it as merely blocked.
    # Defaults chosen for a business process, mirroring the clarification gate
    # (same humans, same surface): a reminder after 24h and escalation at 72h.
    # 72h is the smallest window that survives a weekend — an approver who goes
    # offline Friday evening is reminded on Saturday and still has Monday to
    # decide before the item escalates. Anything shorter escalates healthy items
    # every weekend; anything longer lets a plan rot for most of a sprint.
    # <= 0 in `plan_approval_timeout_hours` DISABLES the deadline (unbounded
    # wait, the pre-timeout behaviour) for a tenant on a slower approval cycle.
    # A reminder that does not fit inside the deadline (e.g. 24h reminder with a
    # 12h timeout) is moved to half the deadline, not clamped to it — see
    # `workflows._approval_windows`.
    # THESE TWO LITERALS ARE THE ONLY WAY TO SET THE WINDOW, and there is
    # deliberately no env var beside them. There was one pair
    # (`DSE_PLAN_APPROVAL_REMINDER_HOURS` / `_TIMEOUT_HOURS`); it was removed
    # because it could not take effect: the only starter is
    # `ingest_gateway.dispatcher._dispatch_row`, which calls
    # `start_workflow(WORKFLOW_TYPE, work_item_id)` with a bare string, so
    # `_coerce_input` builds this dataclass from its defaults and
    # `config.apply_to_input` — the function that would carry env values in — has
    # no production caller at all. That is true of every knob in config.py
    # (activity_heartbeat_seconds, plan_round_cap, …), but a dead knob over a
    # deadline that ESCALATES work is the one an operator would actually reach
    # for. Changing the deployed window means changing these literals; making env
    # config real means changing that ONE dispatcher call to build an input and
    # pass it through apply_to_input, which turns every knob live at once (out of
    # this service's scope).
    plan_approval_reminder_hours: float = 24.0
    plan_approval_timeout_hours: float = 72.0

    # ------------------------------------------------------------------
    # Phase 3 — WSB-E4-T2: iteration caps + evidence refresh debounce (ADR-26)
    # and the evidence pipeline state (preview/demo/visual diff) that survives
    # `continue_as_new` like the rest of the deterministic state.
    # ------------------------------------------------------------------
    # Cap on review rounds (human changes_requested/CI-red loop). Every `while`
    # in the workflow has a cap: clarification (clarification_round_cap), L1 fix
    # (coder_retry_cap), L2 objections (l2_retry_cap), re_plan (plan_round_cap)
    # and now review rounds. Exhausted -> escalated.
    review_round_cap: int = 20
    # Debounce window (seconds) to batch review comments before ONE fix cycle +
    # ONE evidence refresh (ADR-26: 6 comments in one window = at most 1
    # refresh). 0 = no window (compat/legacy tests); production gets the value
    # from config.from_env via apply_to_input.
    evidence_debounce_seconds: float = 0.0
    # Cap on evidence refreshes BEYOND the initial one. Exceeded -> clean
    # decline (audited, P6) without blocking the PR — the evidence just goes
    # "stale".
    evidence_refresh_cap: int = 5
    evidence_refreshes: int = 0
    # Preview autofix (decisão de operador, 2026-08-12): preview degradado por
    # causa de APP volta ao coding sozinho — um agente classifica (conteúdo),
    # o workflow decide (política). Teto DEDICADO além dos existentes; o
    # evidence_refresh_cap continua valendo por cima do re-preview.
    preview_autofix_cap: int = 2
    preview_autofix_rounds: int = 0
    # Freio de no-op: dois fixes seguidos sem files_changed param o laço
    # (espelho do _noop_coder_turns do laço de implementação).
    preview_autofix_noop_turns: int = 0

    # Evidence pipeline state (also projected into work_item_evidence,
    # migration 0014, via the local Activity record_evidence_state).
    preview_status: str | None = None   # PreviewRef.status
    preview_url: str | None = None
    evidence_passed: bool | None = None
    evidence_video_key: str | None = None
    evidence_trace_key: str | None = None
    visual_baseline_key: str | None = None
    # last known files_changed (initial Coder or last fix cycle) — a
    # human-requested refresh has no new commit, so it reuses this set for
    # trigger_preview's deterministic paths-filter decision (FR-20).
    last_files_changed: list[str] = field(default_factory=list)

    # Feeds the history alert (infra/ALERTING-RULES.md §3, with WS-F):
    # Continue-As-New count for this chain of executions, emitted together with
    # the history length/size at each boundary via emit_history_metric.
    continue_as_new_count: int = 0

    # ------------------------------------------------------------------
    # Phase 4 — PR quality metrics (pilot gate "PR quality thresholds",
    # addendum 03). Deterministic counters that survive `continue_as_new` like
    # the rest of the state; emitted via OTel at the PR's terminal boundary
    # (merge/escalation) by the local Activity emit_pr_quality_metric.
    # ------------------------------------------------------------------
    changes_requested_count: int = 0
    # epoch (seconds) of when the PR was finalized (workflow.now() —
    # deterministic/replay-safe read). Used for time-to-merge; None until the PR
    # exists.
    pr_finalized_at_epoch: float | None = None


@dataclass
class WorkItemLifecycleResult:
    work_item_id: str
    status: str
    detail: str | None = None
    pr_number: int | None = None


@dataclass
class OperatorEvent:
    """Latest operator events, exposed via query for observability."""

    action: str
    actor: str
    reason: str | None = None
