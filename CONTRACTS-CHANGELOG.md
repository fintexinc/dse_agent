# Contracts changelog — Fintex DSE

Version history for the packages published under `packages/` (stable
cross-workstream contracts, per `CONVENTIONS.md`). Maintained by WS-F
(WSF-E0) as part of the platform CI/CD foundation.

## Contract change rule

> **Contract changes require chief-architect approval.**

Concretely:

1. **Additive changes are always allowed without prior approval** (adding a
   new optional field, a new function, a new constant) — as long as nothing
   that already exists is removed, renamed, or has its type/signature changed
   (`CONVENTIONS.md`: "add a new field/type without removing or renaming what
   already exists"). This covers the `dse_audit` extension made by WS-F in
   this Phase 1 (`dse_audit.queries` — see the entry below).
2. **Any breaking change** (removing/renaming a public field, changing the
   signature of a public function, changing the semantics of a status/enum
   already consumed by another workstream) requires:
   - an isolated PR containing only the contract change (never mixed with
     business logic for a specific service);
   - explicit approval from the program's chief architect (not just from the
     lead of the workstream that needs the change) — P3 (no agent session
     approves its own work) applies here too: whoever proposes the contract
     change cannot be the one who approves it;
   - a MAJOR version bump (see semver below) and a new entry in this
     changelog **before** the merge, not after;
   - notification in the channels of the consuming workstreams (see "consumed
     by" in each entry) — the merge must not surprise anyone who depends on
     the contract.
3. No workstream should reimplement an already-published contract (e.g. a
   local copy of `ConversationEvent`, or a second write path into the audit
   ledger outside `dse_audit.emit`) — that breaks the "single source of
   truth" guarantee the contract exists to provide.

Versioning: semver (`MAJOR.MINOR.PATCH`) per package, declared in each one's
`pyproject.toml`. MAJOR = breaking; MINOR = additive; PATCH = fix with no
change to the public surface.

## Packages and current versions

| Package | Version | Owner | Consumed by |
|---|---|---|---|
| `dse_contracts` (`packages/contracts`) | 0.4.1 | Foundation | WS-A, WS-B, WS-C, WS-D, WS-E, WS-F |
| `dse_audit` (`packages/dse_audit`) | 0.1.0 | Foundation (minimal) → **extended by WS-F in Phase 1** | Everyone (via `emit`); `dse_audit.queries` (reconstruction/export) consumed by any compliance service/report |
| `dse_identity` (`packages/dse_identity`) | 0.1.0 | Foundation (minimal) | WS-A (adapters resolve `platform_user_id` before writing `actor`) |

## Entries

### `dse_contracts` 0.4.0 → 0.4.1 — `src/app/**` joins the default `ui_path_globs` (rc.93)

- **What:** `TriggerPreviewInput.ui_path_globs` default gains `src/app/**`
  (Angular CLI convention). No field added/removed/re-typed — only the
  default value of an existing optional list.
- **Why:** the second incarnation of the `wi_cc72b204` bug (the first added
  `**/*.component.ts`): an Angular state-only diff (reducers/selectors/types
  `.ts`, no template) matched no ui glob, fell to the deployable chain's
  `**/*.ts` and got an image without npm — `sh: 1: npm: not found`, 900s
  wait, degraded preview (measured on `wi_e15f4991`, PR #26). Java backends
  live under `src/main/**` and still classify as deployable.
- **Change type:** default-value change on an existing field (PATCH bump).
  Callers that pass explicit globs are unaffected; recorded Temporal
  payloads carry their own values and replay unchanged.

### `dse_contracts` 0.3.0 → 0.4.0 — `PlanArtifact` gains optional `estimated_lines` (rc.89)

- **What:** `PlanArtifact.estimated_lines: int | None = None` — the Planner's
  order-of-magnitude estimate of the diff (added+removed lines), parsed from
  the model's response with a sane clamp. `None` = no estimate (fixture,
  absent, garbage). Shown to the human approver in the plan-details modal and
  fed to `classify_risk_class`; `diff_budget_lines` stays as a LEGACY field for
  historical payload compatibility only (it was a hardcoded 400 that no caller
  ever sized — it is no longer rendered, no longer injected into the L2
  context and no longer drives risk).
- **Change type:** additive (rule 1 — MINOR bump, no prior approval needed).
  Historical payloads without the key revalidate with `None`; new plans carry
  the key in `model_dump()`, so `plan_hash` changes for NEW plans only (no
  consumer compares hashes across plan versions).

### `dse_contracts` 0.2.0 → 0.3.0 — `CoderTurnResult` gains optional `ledger_id`

- **What:** a new optional `ledger_id: int | None = None` on `CoderTurnResult`.
  Nothing is removed, renamed or re-typed, and old Temporal payloads decode
  unchanged.
- **Why:** the coder drives the bundled CLI with the gateway configured by env,
  so its spend never passed through the Python client that is the only writer of
  `model_call_ledger`. The console's cost rollup is computed from that table
  alone, so it reported **$0.50 against $27.91** of real spend — 56x — under a
  panel header that read "rollup reconciled with the ledger". Turns are now
  metered at the source, and this field is how the console projector tells a
  metered turn from a legacy one, so the same money is never counted twice.
- **Consumed by:** WS-B (orchestrator carries it up into the audit row), WS-C
  (sandbox-runtime writes it), console-projector (reads it as a dedup guard).
- **Change type:** additive (rule 1) — no prior approval required. MINOR bump.
  The version table below was stale at 0.1.0 while `pyproject.toml` already said
  0.2.0; corrected in passing.

### `dse_contracts` — `CiStatusResult.status` gains `no_ci` (CI gate fix)

- **What:** the `status` field of `CiStatusResult` can now carry a fourth value,
  `no_ci`, alongside `pending`, `green` and `red`. Nothing is removed or
  renamed and the field stays a plain `str` (no `Literal`, so nothing validates
  at runtime — the comment on the field is the whole contract).
- **Why:** `pending` meant two different facts at once — "nothing has reported
  yet" and "nothing will ever report". A PR opened against a repo with no CI
  configured returned an empty check-run array forever, the aggregator read it
  as "still running", and the wait had no way to end. On the pilot cluster that
  produced 8 PRs stuck in `ci_pending` and **0 of 25 work items completed in the
  deployment's lifetime**. `no_ci` makes the difference expressible so the
  workflow can treat it as terminal.
- **Consumed by:** WS-B (the orchestrator's review loop is the only consumer;
  `unknown_ci_status` there previously escalated on any value other than
  `green`, which is why the workflow change ships together with this one).
- **Change type: MINOR (additive) — ruled by the chief architect
  (André Saraiva), 2026-07-28.** The classification was genuinely ambiguous and
  was escalated rather than assumed: it reads as additive under rule 1 (nothing
  removed, renamed or re-typed; the field stays a plain `str`) but as breaking
  under rule 2 ("changing the semantics of a status/enum already consumed by
  another workstream"), because an existing input — an empty check-run array —
  now produces a different value than before.
  The ruling is rule 1. The reasoning of record: `CiStatusResult.status` has
  exactly one consumer (the WS-B review loop), it ships in the same image and is
  updated in the same change, so no external workstream can ever observe the old
  value for a new input. `packages/contracts` accordingly goes `0.1.0` → `0.2.0`.
  P3 was respected: the proposer did not approve this — the architect did, and
  GitHub independently refuses a self-approval on an authored PR.
- **Migration:** `migrations/0033_ci_no_ci_status.sql` widens the CHECK
  constraints on `wse_ci_status.status` and `work_items.ci_status` to accept
  `no_ci`. It is purely permissive, so it is safe to apply while the previous
  image is still running — and it MUST be applied before the new image, or the
  first `no_ci` write fails the work item.

### `dse_audit` 0.1.0 → additive extension (WSF-E1-T2, no version bump declared in pyproject — see note below)

- **What:** new module `dse_audit/queries.py` with
  `reconstruct_work_item_history(work_item_id) -> list[dict]`,
  `export_audit_range(tenant_id, start, end) -> list[dict]` and
  `export_audit_range_csv(...) -> str`. Re-exported from `dse_audit/__init__.py`
  alongside the pre-existing symbols (`emit`, `get_connection` — neither of
  which was removed/renamed).
- **Why:** Phase 1 exit criterion ("first audit-based reconstruction
  exercise passes") + compliance-grade export per tenant/period.
- **Change type:** additive (rule 1 above) — does not require prior
  chief-architect approval, but **is documented here for cross-workstream
  visibility**, since `packages/dse_audit` is a foundation directory and
  other workstreams may (reasonably) not expect changes in it.
- **Process note:** `dse_audit`'s `pyproject.toml` still declares
  `version = "0.1.0"` — WS-F's recommendation for the final consolidation:
  bump to `0.2.0` (MINOR, additive) in the integration PR, since a new
  version was in fact published.
- **Consumed by:** any service/report that needs to answer "what happened to
  WorkItem X" or produce an audit export — no real consumer integrated yet in
  this session (cross-workstream, integration happens in the consolidation
  phase).

### `services/platform` (dse-platform) 0.1.0 → new package (WSF-E2-T3a)

- **What:** `dse_secrets` — Vault client (`SecretsClient`, `get_secret`,
  `put_secret`, `delete_secret`). It is not a package under `packages/`
  because it is WS-F-specific (platform), but it is published as a stable
  consumption contract for WS-A/WS-C/WS-D (signature documented in
  `services/platform/README.md`).
- **Change type:** new package, not a change to an existing contract — does
  not require chief-architect approval for the initial v0.1.0, but future
  changes to `SecretsClient`'s public signature follow rule 2 above as soon
  as WS-A/WS-C/WS-D actually integrate.

## How to propose a breaking change

1. Open an issue/PR describing the affected field/signature, why an additive
   change is not sufficient, and every known consumer.
2. Tag the program's chief architect for review.
3. After approval, do the version bump + this changelog entry in the same PR
   as the contract change (before any PR that depends on the change).
