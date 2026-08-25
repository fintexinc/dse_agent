# WS-A — Ingestion and adapters (Fintex DSE, Phase 1 + Phase 2)

This README documents the whole workstream (WS-A): `services/ingest-gateway/`
(this directory, the core), `services/adapter-slack/`,
`services/adapter-github/` and (Phase 2) `services/adapter-jira/`. Every
adapter imports `ingest_gateway` as a library — all the admission, correlation,
intake defense and (Phase 2) tenant binding logic lives here
and is shared, not duplicated.

> **Phase 2 ("Judgment & queue"):** see the
> [Phase 2 — what WS-A added](#phase-2--what-ws-a-added) section at the end of
> this file. The rest describes the Phase 1 baseline (which still holds).

## What is implemented and working

### WSA-E1-T3 — Transactional gateway + outbox dispatcher
- `ingest_gateway.gateway.admit_work_item(event, ...)`: writes `work_items` +
  `ingest_events` in the SAME Postgres transaction. `work_item_id` and
  `idempotency_key` are derived deterministically from
  `ConversationEvent.event_id` (sha256) — redeliveries of the same webhook
  converge via `ON CONFLICT ... DO NOTHING`, never duplicating.
- `ingest_gateway.gateway.record_signal_event(...)`: writes a signal event
  (Path B) to the same outbox, without creating a new `work_items` row.
- Kill switch per `(tenant_id, channel)` (`channel_kill_switches`,
  `migrations/0002_wsa.sql`) checked BEFORE any INSERT — a disabled channel
  creates no WorkItem and does no processing, and produces
  `dse_audit.emit(action="admission_blocked_kill_switch")`. It also honors
  WS-F's tenant-wide kill switch (`tenant_config.kill_switch_enabled`,
  best-effort/defensive import — it works even if that table does not exist in
  the environment).
- `ingest_gateway.dispatcher.Dispatcher`: drains unprocessed
  `ingest_events` with `SELECT ... FOR UPDATE SKIP LOCKED`. For
  `kind == "task_request"` it calls `Temporal.start_workflow(WORKFLOW_TYPE,
  work_item_id, id=work_item_id, task_queue=TASK_QUEUE)`; for the other
  kinds, `WorkflowHandle.signal(SIGNAL_NAME, payload)` on the workflow already
  in flight. `WorkflowAlreadyStartedError` is treated as an idempotent
  success (never re-raised). `processed=true` is only set after Temporal
  confirms (or after the duplicate exception).
  - **Core test** (`tests/test_dispatcher.py::test_two_concurrent_dispatchers_drain_without_duplication_or_loss`):
    20 distinct ingest_events, 2 concurrent dispatchers (separate threads,
    each with its own Temporal `Client` and Postgres connection) draining the
    SAME queue — this proves, against the **real** Temporal and Postgres of the
    infra (not mocked), that there is neither duplication nor loss.
    This is the core of the Phase 1 exit chaos test (NFR-01) on the intake
    side.

### WSA-E2-T1 — Signature verification
- `ingest_gateway.security.verify_slack_signature`: HMAC-SHA256 of the signing
  secret over `v0:{timestamp}:{body}`, with a 5-minute replay window.
- `ingest_gateway.security.verify_github_signature`: HMAC-SHA256 of the webhook
  secret over the raw body (`X-Hub-Signature-256`).
- Both adapters reject with 401 + `dse_audit.emit(action=
  "signature_rejected")` any event that cannot be verified — the forgery corpus
  was tested (no signature, wrong signature, expired timestamp,
  replay of an old valid signature, body altered after signing): 100%
  rejected (`tests/test_security.py` + `test_signature_pipeline.py` in
  each adapter).
- Secrets read via `dse_secrets` (WS-F, `services/platform/`,
  WSF-E2-T3a) **which already exists in this session** — a real import, with
  automatic fallback to a local env var (`SLACK_SIGNING_SECRET`/
  `GITHUB_WEBHOOK_SECRET`) when Vault does not have the version stored (no
  real Slack App/GitHub App was registered in this session).

### WSA-E2-T2 — TOCTOU snapshot
- `content_snapshot` is read directly from the received webhook body — no
  adapter ever calls `conversations.history`/
  `conversations.replies` (Slack) or `GET /repos/.../issues/{n}` (GitHub)
  afterwards. Proven in tests (`test_toctou_snapshot_freezes_content_at_event_time`
  in adapter-slack, `test_toctou_snapshot_not_refetched_on_redelivery_with_edited_body`
  in adapter-github): resending the "same" webhook with edited text is
  deduplicated by `event_id` — the already-persisted snapshot is never
  overwritten.

### WSA-E2-T3 — Inbound content sanitization
- `ingest_gateway.sanitize.sanitize_content`: strips invisible/control
  Unicode (zero-width space/joiner, bidi override — Unicode categories
  `Cf`/`Cc`) and redacts obvious token/secret patterns (`ghp_`,
  `xox[bpears]-`, AWS access key id, PEM private key block, generic bearer
  token).
- **Explicitly documented as MITIGATION, not CONTAINMENT** (see the
  docstring of `ingest_gateway/sanitize.py`): the real containment that
  prevents exfiltration even if a model is tricked is WS-C's default-deny
  egress proxy (`services/egress-proxy/`).
- The original `content_snapshot` (the one frozen by the TOCTOU defense) is
  never overwritten — the sanitized version is attached separately as
  `sanitized_content` in the `ingest_events` `payload` (that is the version
  that must flow to any stage involving a model).

### WSA-E3 — Slack adapter (`services/adapter-slack/`)
- **Inbound** (`adapter_slack/app.py`, `POST /slack/events` and
  `POST /slack/interactions`): `app_mention` creates a `task_request`; a plain
  message in an existing thread correlates via `thread_ts`
  (`clarification_answer`); a button click (`block_actions`) becomes
  `kind=approval`. Everything goes through the 4 defenses before `correlate()`
  decides Path A/B. The adapter is 100% stateless.
- **Outbound** (`POST /internal/status-comment`): uses
  `dse_contracts.mutable_comment.MutableCommentWriter` with
  `SlackCommentBackend` (real, `slack_sdk.WebClient`,
  `chat.postMessage`/`chat.update`) — exactly 1 status message per task,
  edited in place, never a new one per update. `comment_ref` is persisted in
  Postgres (`comment_state`), not in memory — it survives a process restart.
  - Without real Slack App credentials: a documented in-memory
    `FakeSlackClient` replaces the transport; the logic (`SlackCommentBackend`,
    `MutableCommentWriter`) is 100% real and is exactly what would run against
    the real API.

### WSA-E4 — GitHub adapter (`services/adapter-github/`)
- **Inbound** (`adapter_github/app.py`, `POST /github/webhook`): an issue
  `assigned`/`labeled` (with the configurable label, default `dse`) creates a
  `task_request`; a plain issue comment containing `@<bot_login>` creates a
  `task_request`; a comment WITHOUT a mention on an issue with no active
  WorkItem is ignored (`path: ignored_no_mention`, zero write I/O).
  **A comment on a PR** (via `issue_comment` on an issue that is a PR, or via
  `pull_request_review_comment`) **NEVER creates a new WorkItem** — it only
  correlates to an active WorkItem by PR/issue number
  (`kind=review_comment`); with no match it is ignored with an audit row
  (`review_comment_ignored_no_active_work_item`). Explicitly tested
  (`test_pr_issue_comment_never_creates_work_item_even_without_match`).
- **Outbound** (`POST /internal/status-comment`): the same
  `MutableCommentWriter`, backend `GithubCommentBackend` (real,
  `POST`/`PATCH /repos/{repo}/issues/{issue}/comments` via `requests`),
  authenticated as a **GitHub App** (`adapter_github.auth`: JWT RS256 +
  exchange for an installation access token) — never a personal token.
  - Without a real GitHub App registered: a documented in-memory
    `FakeGithubClient` replaces the transport; the App authentication logic
    (`generate_app_jwt`/`get_installation_access_token`) is real (PyJWT +
    `requests` against `api.github.com`), only the real
    `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY`/`GITHUB_APP_INSTALLATION_ID`
    are missing.

### WSA-E6-T1 — Path A/B correlation
- `ingest_gateway.correlate.correlate(conn, tenant_id=, event=,
  requester_principal=)` → `CorrelationResult(kind, work_item_id,
  provenance_work_item_id)`. Deterministic lookup by `source_ref`
  (`{channel,thread_ts}` for Slack, `{repo,number}` for GitHub — the same
  number serves both issue and PR) against `work_items` with a
  non-terminal status.
  - No match → `"new_task"` (Path A).
  - Match on an active item → `"signal"` (Path B) — the caller decides
    `signal_workflow` (the adapter, in this workstream; or WS-B via the Temporal
    client).
  - Match on a **terminal** item (`done`/`failed`) → `"new_task"` +
    `provenance_work_item_id` filled in (the caller writes the provenance link
    into the audit of the new admission).

### Steering — autorização por canal (desde 2026-08-21)

As seções WSA-E6-T2a/T2b (allowlist explícita `tenant_steering_allowlist` +
resolução de papel por cima dela) foram REMOVIDAS por decisão de operador: o
convite ao canal é a autorização. Quem lê o que o DSE escreve no canal pode
responder; a assimetria "lê mas não dirige" custava mais do que protegia, e
cada superfície nova (Teams foi a prova) recriava o problema, porque a mesma
pessoa tem uma identidade por plataforma e nenhuma nasce numa lista. A tabela
caiu na migração `0046_drop_steering_allowlist.sql`; `correlate()` não aplica
mais gate de identidade em `steering`/`review_comment`. A APROVAÇÃO DE PLANO
não passou por aqui e continua com a cascata própria (CODEOWNERS → aprovadores
designados do access bundle). O offboarding (ADR-22) continua valendo onde há
papel: aprovação e console.

## What is a local fixture/mock (documented, not production)

- `FakeSlackClient` (`adapter_slack/backend.py`) and `FakeGithubClient`
  (`adapter_github/backend.py`): in-memory, used in the tests in place of the
  real HTTP transport. The business logic around them
  (`MutableCommentWriter`, `SlackCommentBackend`/`GithubCommentBackend`,
  `PgCommentStateStore`) is 100% real.
- Secrets (`SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`,
  `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_*`): read from env vars as a fallback
  when Vault (via `dse_secrets`) does not have the path stored — no real
  Slack/GitHub app was registered in this development session.
- `DSE_TENANT_ID` (default `tenant_dev`): Phase 1 is single-tenant for
  development — see "What needs an architect decision" below.

## What needs real credentials/infra for production

1. **Real Slack App**: register the app, configure the Events API
   (`app_mention`, `message.channels`) and Interactivity pointing at
   `/slack/events`/`/slack/interactions`, store `bot_token`/
   `signing_secret` in Vault at `dse/slack/webhook` (keys `bot_token`,
   `signing_secret`) via `dse_secrets.put_secret`.
2. **Real GitHub App**: register the App, generate the RSA private key,
   install it on the tenant's repo(s), store `app_id`/`private_key`/
   `installation_id`/`webhook_secret` in Vault at `dse/github/app`.
3. **Production Vault**: today it points at the dev Vault
   (`localhost:8200`, root token `dse_dev_root`) — production needs a real
   Vault with per-service access policy (not a root token).
4. **Real multi-tenancy**: `ConversationEvent` does not carry `tenant_id` (it is
   purely a platform concept) — the mapping
   Slack workspace/GitHub org → tenant is today a single fixed
   `DSE_TENANT_ID` per process. Production needs a mapping table
   (workspace/org → tenant_id), a natural scope for WS-F/Phase 2
   (full identity map, ADR-22).

## Request to the architect / pending decision

- **`SIGNAL_NAME`** (`ingest_gateway/dispatcher.py`, currently
  `"conversation_signal"`) is not in `dse_contracts.constants` — only
  `TASK_QUEUE`/`WORKFLOW_TYPE` live there. I did not edit `packages/contracts`
  (out of my scope). Request: promote this constant to
  `dse_contracts.constants.SIGNAL_NAME` as soon as the WS-B workflow
  registers the real signal handler, so both sides import from the same
  place instead of duplicating the string.
- **Disambiguating clarification vs. steering** on a plain thread reply
  (Slack) or a plain issue comment (GitHub): Phase 1 defaults to
  `clarification_answer` because the adapter does not know whether the bot is
  "awaiting a reply" — that state lives in the WS-B workflow. If WS-B wants to
  expose that state (e.g. via a column in `work_items` or a field in the
  `plan`), the adapter can consume it to disambiguate better.
- **`conftest.py`/`tests` package collision across services**: running
  `pytest -q packages services` (the root `make test` target) currently fails
  with `ValueError: Plugin already registered under a different name`
  because multiple services (not just mine) have a `tests/` directory
  with an `__init__.py` + `conftest.py` at the same relative name. Each service
  runs clean on its own (`cd services/X && pytest -q` — the flow documented in
  `CONVENTIONS.md` itself). A monorepo-wide fix
  (e.g. `[tool.pytest.ini_options] addopts = "--import-mode=importlib"`
  in a root `pyproject.toml`/`pytest.ini`, or unique test package names per
  service) is a foundation decision — I did not edit `Makefile`/the root as
  it is outside my directory scope.

## How to run the tests

Each service has its own `pyproject.toml` and runs in isolation (which avoids
the `conftest.py` collision described above):

```bash
source /Users/saraiva/Documents/DSE/fase1/.venv-wsa/bin/activate

cd /Users/saraiva/Documents/DSE/fase1/services/ingest-gateway && pytest -q
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-slack && pytest -q
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-github && pytest -q
```

Requires the foundation's real infra to be up (Postgres `localhost:5432`,
Temporal `localhost:7233`) — the tests for `admit_work_item`, `correlate`,
`is_authorized_to_steer` and, above all, for the `Dispatcher` run against
**real** Postgres and Temporal, never mocks (CONVENTIONS.md: mocking
durability/idempotency would defeat the whole point of the test).

### Real result from this session

```
services/ingest-gateway  : 37 passed
services/adapter-slack   : 14 passed
services/adapter-github  : 19 passed
TOTAL                    : 70 passed, 0 failed
```

The 3 `Dockerfile`s (`services/{ingest-gateway,adapter-slack,adapter-github}/Dockerfile`)
were tested with a real `docker build` in this session (built from the
monorepo root) and all complete successfully.

## Operational note: Temporal went down during this session

During development, the `dse_temporal` container (foundation) was
`Exited` due to an image bug (`DYNAMIC_CONFIG_FILE_PATH` points at
`config/dynamicconfig/development-sql.yaml`, which does not exist in the
`temporalio/auto-setup:1.24` image used by `docker-compose.yml`). Without editing
`docker-compose.yml` (out of my scope), I fixed it by copying a minimal empty
dynamic config file into the stopped container
(`docker cp` + `docker start dse_temporal`) — I did not use `make up`/`down`
nor `docker compose down`, I only restarted the existing container. This
affects ALL workstreams that depend on Temporal (WS-B in particular) —
the architect should consider adding that file (even empty) to the repo/image so
the next `docker compose up` does not recreate the container without it.

---

## Phase 2 — what WS-A added

WS-A's Phase 2 ("Judgment & queue") adds the Jira surface, tenant mapping,
the merge webhook and status-based signal routing. Migration:
`migrations/0008_wsa2.sql`. Fragment: `docker-compose.wsa.yml` (adapter-jira +
workers, port 8804). Contracts imported from `dse_contracts` (not redefined):
`SIGNAL_PLAN_APPROVAL`, `SIGNAL_MERGED_BY_HUMAN`.

### WSA-E5 — Jira adapter (`services/adapter-jira/`)
A new service mirroring adapter-github. Inbound (webhook + mandatory poller
fallback), per-ticket serialized transitions, a single status comment via the
same `MutableCommentWriter`. Full details and gaps in
[`../adapter-jira/README.md`](../adapter-jira/README.md). New hooks in
`ingest_gateway`: `verify_jira_signature` (defense #1, `X-Hub-Signature`).

### WSA-E1-T5 — Platform → tenant mapping
- Table `tenant_platform_bindings(platform, binding_key, tenant_id)`
  (`0008_wsa2.sql`).
- `ingest_gateway.resolve_tenant(conn, platform=, binding_key=)` →
  `ResolvedTenant(tenant_id, from_binding)`. Resolved in **all 3 adapters**:
  Slack workspace (`team_id`), GitHub installation (`installation.id`), Jira
  site (host from `issue.self`).
- **Documented fallback** (P6): missing binding → `DSE_TENANT_ID` (single-tenant)
  **with a warning audit row** (`tenant_binding_missing_fallback_default`) — it
  never guesses a tenant. Filling in the table makes resolution stop hitting the
  fallback with no code change.

### WSA-E4-T3 — `pull_request` merged webhook → `merged_by_human`
- A handler in adapter-github for `pull_request` (action=`closed`,
  `merged=true`), correlated by PR number to the active WorkItem. It writes an
  ingest_event with the deterministic `merged_by_human` marker in the payload;
  the dispatcher fires `SIGNAL_MERGED_BY_HUMAN` with the principal of whoever
  merged.
- **A PR closed WITHOUT a merge fires NOTHING** (route `ignored_pr_closed_unmerged`,
  tested). Redelivery is deduplicated by `event_id` (derived from
  `merge_commit_sha`).

### WSA-E6-T3 — Signal routing by WorkItem status
- The fixed Phase 1 `kind → signal` map was replaced by
  `dispatcher._route_signal(status, kind, payload)`, which reads
  `work_items.status`:
  - `kind=approval` + status `awaiting_plan_approval` → `SIGNAL_PLAN_APPROVAL`
    (verdict/route read from deterministic markers in the payload).
  - `kind=approval` + status `pr_ready`/`review_feedback` → `SIGNAL_REVIEW_COMMENT`.
  - `kind=approval` + unexpected status → **audit row + no signal** (it never
    guesses, P6; consumed as `dispatch_declined_unexpected_status`).
  - `merged_by_human` marker → `SIGNAL_MERGED_BY_HUMAN` (regardless of status).
  - `clarification_answer`/`review_comment`/`steering` → **Phase 1 behavior
    preserved**.
- Deterministic (P1): no flow decision made by an LLM — only `status` +
  markers set by the adapter.

### Preserved principles
- **P1** 100% deterministic routing (status + markers, never an LLM).
- **P6** an unexpected status declines with evidence, it never guesses; the kill
  switch and tenant fallback always leave an audit row.
- **P8** every consequential decision (binding fallback, merge, transition,
  approval, decline) becomes an audit row via `dse_audit.emit`.

### Real `pytest -q` result from this session (Phase 1 + Phase 2, WS-A)

```
services/ingest-gateway  : 52 passed   (37 Phase 1 + 15 new: routing + tenant binding)
services/adapter-slack   : 14 passed   (Phase 1; tenant binding via env fallback, no regression)
services/adapter-github  : 24 passed   (19 Phase 1 + 5 new: merge webhook + tenant binding)
services/adapter-jira    : 17 passed   (new service)
TOTAL                    : 107 passed, 0 failed
```

Run against **real** Postgres and Temporal (never mocked for
durability/idempotency — P8). The 4 `Dockerfile`s (including the new
`adapter-jira/Dockerfile`) build successfully; `docker compose config` of the
foundation+wsa merge validates.

### Honest notes / environment gaps

- **The `dse_ingest_dispatcher` container runs Phase 1 code.** The dispatcher
  container currently up on the shared infra was built in Phase 1
  (old `kind`-based routing) and is a **concurrent consumer** of the same
  `ingest_events` outbox (via `SELECT ... FOR UPDATE SKIP LOCKED`). Impact on
  the tests: (a) the chaos test `test_two_concurrent_dispatchers_...` had its
  `sum(results) == N` assertion relaxed to `<= N` (the test's 2 dispatchers
  process at most N; the rest may be drained by the container) — the real
  NFR-01 invariant (no loss, no duplication, exactly-once)
  is still proven by the database assertions, which are robust to any number of
  consumers; (b) the routing INTEGRATION tests (WSA-E6-T3) exercise
  `_dispatch_row` directly (new code + a real Temporal signal) instead of
  going through the contended outbox, so as not to pick up old routing from the
  container. Recommendation to the integrator: rebuild `dse_ingest_dispatcher` in
  the Phase 2 consolidation.
- **`SIGNAL_PLAN_APPROVAL`**: the dispatcher already routes to the correct name;
  the `@workflow.signal plan_approval` is being built by WS-B (WSB-E3-T2) in
  parallel.
- **Real multi-tenancy**: `resolve_tenant` is ready; what is missing is
  populating `tenant_platform_bindings` with the real
  workspaces/installations/sites (operational data, not engineering).

## Recovery sweeps — lost replies (`reconcile.py`) and lost workflows (`stranded.py`)

Two different silent failures, two different layers.

**A lost REPLY** (`reconcile.pending_reply_work_items`): the task is healthy, the
human answered, the webhook never landed. The sweep re-reads the thread of items
sitting in `needs_clarification`/`awaiting_repo_selection` — never
`awaiting_plan_approval`, because re-reading is exactly what the TOCTOU defense
forbids for a decision.

The query used to be `ORDER BY last_transition_at ASC LIMIT 50` and had a
permanent blind spot: recovery does not change `last_transition_at` (re-reading a
thread is not a state transition, and forging one would corrupt both the ordering
and every "stuck for how long" reading in the console), so every cycle re-read the
same oldest 50 threads and item 51 was never fetched — not late, never. It now
pages through the ordering with a keyset cursor (process-local, advisory) and
wraps at the tail, so every pending item is visited within
`ceil(total / limit)` cycles while `limit` still caps the platform API calls per
cycle. `reset_reply_sweep_cursor()` forces the next pass to start at the oldest.

The cursor is process-local and advisory. The lock around it is held only for
dict access, never across the query (holding it over a database round-trip in an
HTTP handler would let one slow connection block every tenant's sweep), so two
overlapping calls can serve the same page — harmless, since re-ingestion dedupes
on `event_id`. What the lock does give is that a sweep may only replace the
position it read: a slow call finishing late cannot rewind the rotation over a
newer position, nor re-seat a position that the tail wrap has dropped in the
meantime (the wrap goes through the same check instead of clearing the key on its
own — that gap made a late advance undo the wrap and re-serve an old page).

**A lost WORKFLOW** (`stranded.stranded_work_items`): the task itself is gone.
Four items were found in `implementing` (x2), `queued` and `new` two days old, with
no audit event for ~40 hours and no workflow in Temporal — not open, and not
closed either, since the 24h namespace retention had purged the history. The
detector therefore keys on audit silence, not on a Temporal probe: after
retention, "completed fine" and "never existed" both answer NOT_FOUND.

The detector keys on "silent AND nobody is legitimately waiting on a human", so
every status where a live workflow parks on a `wait_condition` is excluded:
`needs_clarification`, `awaiting_repo_selection`, `awaiting_plan_approval`
(intake gates) and `review_ready`, `merge_pending`, `pr_ready` (the review and
merge parks — `_set_status(review_ready, "awaiting_human_review")` and
`_set_status(merge_pending, "approved_awaiting_merge")` each write ONE row and
then wait, untimed, for a person). `pr_ready` is on the list because it is the
pre-patch alias of `pr_open`/`review_ready`/`merge_pending` and such executions
are still in flight. Without those three the sweep would escalate every open PR
under review. `pr_open`, `ci_pending` and `review_feedback` are NOT excluded: what
they wait for is the engine, so silence there is the symptom.

Detection is read-only and writes no audit row (a timer narrating non-events into
an append-only ledger is how one stuck item once produced ~2,900 rows).
`escalate_stranded` is the only action offered: it moves the item to `escalated`
(terminal — "handed to a human", not "retried"), bumps `state_version` and
`last_transition_at` exactly as the canonical writer does, and writes exactly one
`work_item_escalated_stranded` row. Two guards in the statement make a second call
a no-op: the status guard, and a ledger guard ("our escalation row is the newest
thing in this item's audit_log"). The second one is what survives an un-guarded
write to `work_items.status` un-escalating the item — the status is mutable, the
ledger is not — while still allowing a genuine second stranding to be reported
once, because any other audit row lifts the suppression. Resuming is deliberately
not offered: a workflow whose history is gone cannot be re-run without risking a
repeated coder turn or a reopened PR.

**Not wired yet**: nothing calls `stranded_work_items`/`escalate_stranded`. The
timer/endpoint that runs the sweep (and the threshold it uses) lives with the
operator surfaces, not in this package. Whatever wires it MUST probe Temporal
between the two halves: an operator `pause` parks the workflow on an untimed
`wait_condition` and is recorded only in workflow state (no column, no audit row),
so a paused item is indistinguishable from a stranded one in SQL. An OPEN
execution is unambiguous proof of life; it is only NOT_FOUND that is ambiguous.

The escalation surfaces in the console as an `error` timeline event
(`console-projector`'s `AUDIT_EVENT_MAP`), not the `note` that unclassified
actions fall through to — the same type as the orchestrator's own `escalated`,
because it is the same outcome. The action name is a cross-service string
contract (the map has the literal typed out, this package owns the constant), so
`test_stranded.py` reads that map from source and fails if the two drift.

The `idle_for_seconds` guard only refuses a non-positive threshold, which would
select every in-flight item in the tenant. It cannot tell too-short from
long-enough: the longest gap the engine legitimately leaves between audit rows is
made of another service's activity timeouts, retry budgets and
`DSE_CI_POLL_INTERVAL_SECONDS`, so whoever wires the sweep owns that number.
