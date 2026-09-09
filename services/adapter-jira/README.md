# adapter-jira (WS-A, Phase 2 — WSA-E5)

The DSE's third intake surface (§10.1, UC2/UC5), mirroring the structure of
`adapter-github`. All shared logic (admission, correlation, the 4
intake defenses, tenant binding) comes from `ingest_gateway` — the adapter is 100%
stateless. General WS-A documentation lives in
[`../ingest-gateway/README.md`](../ingest-gateway/README.md); this file
covers only what is Jira-specific.

## What it does

- **Inbound webhook** (`POST /jira/webhook`, `adapter_jira/app.py`):
  - `jira:issue_created` / `jira:issue_updated` carrying the trigger label (`dse`) ->
    `task_request`.
  - A status transition into the configured approval column
    (`JIRA_PLAN_APPROVED_STATUS`, e.g. "Plan approved") -> `kind=approval`
    with `approval_verdict=approved` (UC5 on the Jira surface). The rejection
    column (`JIRA_PLAN_REJECTED_STATUS`) -> `approval_verdict=rejected` +
    `approval_route=re_plan`. The dispatcher (WSA-E6-T3) routes this to
    `SIGNAL_PLAN_APPROVAL` when the WorkItem is in `awaiting_plan_approval`.
  - `comment_created` -> `clarification_answer`, correlated by ticket key.
  - Correlation by **ticket key** (`source_ref = {"ticket_key": "DSE-123"}`).
  - The 4 defenses: signature (`X-Hub-Signature` HMAC-SHA256,
    `ingest_gateway.verify_jira_signature`), TOCTOU snapshot (content read from the
    payload, never re-fetched), sanitization (`sanitize_content`), idempotency
    (deterministic `event_id`).

- **MANDATORY fallback poller** (`adapter_jira/poller.py`,
  `python -m adapter_jira.poller_main`): the Jira webhook is best-effort. The poller
  sweeps the configured projects and reconciles each issue through the **same
  idempotent path** as the webhook (`adapter_jira/ingest.py`). Because the `event_id`s are
  derived from the issue's **state** (issue id + status + comment id —
  never from the webhook's changelog), webhook and poller converge on the same
  `event_id` and whichever path arrives second dedups. **They never duplicate** (proven in
  `tests/test_poller_webhook_idempotency.py`, in both directions).

- **Restarting a card** — the gesture is the `dse` label itself: take it off,
  put it back. The DSE removes the label as soon as it picks the work up, so the
  label reads as a button rather than as a permanent marking.
  - It works from any of the five terminal states (`done`, `failed`,
    `cancelled`, `escalated`, `blocked`) and as many times as a human makes the
    gesture. There is no ceiling; the old `dse-retry` label allowed exactly one
    restart per card, for the lifetime of the card, and is gone.
  - What makes it safe is that it is an **edge, not a level**: `jira_trigger_state`
    remembers whether the label was on the card last sweep, and the generation
    behind the `event_id` only advances on absent → present. A label that simply
    sits there — because nobody took it off, or because the service account
    lacks *Edit Issues* — advances nothing, sweep after sweep. That distinction
    is the whole defence: a timer re-triggering the same action once a minute is
    what once wrote ~2,900 rows into an append-only ledger.
  - The removal is never the guard. The poller does not disarm the latch when it
    removes the label; the next sweep does, by **observing** the label gone. So a
    PUT that answers 200 without applying is harmless by construction. When the
    label survives the removal, `jira_trigger_label_removal_failed` says so —
    almost always a missing *Edit Issues* permission.
  - An attempt still **in flight** is never restarted: two workflows on one
    branch and one pull request is worse than a late restart.
  - See `tests/test_the_label_gesture_restarts_a_card.py` (including 200
    simulated sweeps with every label removal failing, and the deploy-day
    property that generation 0 keeps the historic `event_id` spelling).

- **Outbound**:
  - **Per-ticket serialized transitions** (`adapter_jira/transitions.py`,
    `POST /internal/transition` enqueues; `python -m
    adapter_jira.transition_main` drains). Jira Cloud rejects concurrent
    transitions on the same issue; the worker guarantees, via a per-ticket
    advisory lock (`pg_try_advisory_lock(hashtext(ticket_key))`), that only one transition
    per ticket runs at a time — different tickets proceed in parallel.
    Idempotent enqueue by `dedup_key`.
    A target status **the project's workflow does not contain** is a configuration
    fact, not a transient failure: the row is settled once (`processed`, with the
    reason in `last_error`), audited once as `jira_transition_unavailable` with the
    project's status list *and* what the card can reach, and the transitions queued
    behind it on the same ticket proceed.
    A status that **exists but is not reachable from where the card is standing**
    is a different claim and stays queued (`jira_transition_not_reachable`): `GET
    /issue/{key}/transitions` only returns the edges out of the current status, so
    settling on it would discard a mirror update that succeeds as soon as the card
    moves. The configuration question is answered by
    `GET /project/{key}/statuses`; when that cannot be read the row is kept, never
    settled. Neither transient case retries forever — attempts are counted and the
    row is settled with `jira_transition_abandoned` when they run out, instead of
    silently ceasing to be selected. The work item's status in Postgres is the
    system of record; the Jira column is a mirror, and failing to mirror never
    stalls the task. See `tests/test_transition_unavailable.py`.
  - **Rate limiting**: `RealJiraClient._request` is the only HTTP entry point. A
    429 honours `Retry-After`, or falls back to bounded exponential backoff with
    jitter. The wait budget (30s) is **per client and per 60s window, shared by
    every request** — a per-request budget bounds nothing, since a sweep makes many
    requests (five rate-limited calls at 30s each is 150s inside a 60s interval).
    So the guarantee is: one client spends at most 30s of any 60 seconds waiting on
    429s; past that it raises `JiraRateLimited` immediately and the budget refills
    as the window slides. That bounds the 429 cost of a sweep, not its total
    duration — a sweep over many tickets still takes as long as its requests take.
    `self_account_id` is resolved at most once per process, **failures included**
    (the poller asks per comment; a success-only cache re-paid the backoff every
    time). Other HTTP errors are never routed into the retry path.
    See `tests/test_rate_limit.py`.
  - **Single status comment** (`POST /internal/status-comment`): the SAME
    `dse_contracts.mutable_comment.MutableCommentWriter` as the
    Slack/GitHub adapters, with a new `JiraCommentBackend` (surface `jira`, same
    `comment_state` table).

## Running locally

```bash
source /Users/saraiva/Documents/DSE/fase1/.venv-wsa/bin/activate
pip install -e ../../packages/contracts -e ../../packages/dse_audit -e ../../packages/dse_identity \
            -e ../../services/platform -e ../../services/ingest-gateway -e .
JIRA_WEBHOOK_SECRET=dev_only_fixture JIRA_TRIGGER_LABEL=dse \
  DSE_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
  uvicorn adapter_jira.app:app --port 8804
```

Endpoints: `POST /jira/webhook`, `POST /internal/status-comment`,
`POST /internal/transition`, `GET /health`.

## Tests

```bash
cd /Users/saraiva/Documents/DSE/fase1/services/adapter-jira && pytest -q
```

Most of the suite requires a real Postgres (`localhost:5432`, migrations
`0002_wsa.sql` + `0008_wsa2.sql` applied). Jira itself is 100% fixture
(`FakeJiraClient`); the business logic (backend, transition serialization,
poller, ingestion) is the real thing.

The suites for the retry label, unavailable transitions and 429 backoff run
**without a database** — `TransitionWorker` takes a `conn_factory` and the retry
path's `get_connection` is patched, so the connection is a recording fake
(`tests/helpers.py:FakeConn`). Asserting "wrote nothing" is half of what those
features promise, and only a statement log can prove it.

## What is a local fixture/mock (not production)

- `FakeJiraClient` (`adapter_jira/backend.py`): in-memory, replaces the
  HTTP transport in the tests. `RealJiraClient` (Jira Cloud REST API v3,
  `requests` + Basic auth with a service account) is what runs in production.
- Secrets (`JIRA_WEBHOOK_SECRET`, `JIRA_BASE_URL`, `JIRA_ACCOUNT_EMAIL`,
  `JIRA_API_TOKEN`): read from Vault (`dse/jira/service_account`) via
  `dse_secrets`, with a fallback to env vars — no real Jira site was
  registered in this session.

## Gaps / what needs real infra (documented, not hidden)

1. **A real Jira Cloud site**: register the service account with a scoped
   (project-level) token, create the dynamic webhook with a secret (for the
   `X-Hub-Signature`), and store `base_url`/`email`/`api_token`/`webhook_secret`
   in Vault under `dse/jira/service_account`. Without that, `RealJiraClient` is not
   exercised end to end.
2. **Approval attribution by the poller**: the poller only sees the issue's current
   state, not the changelog, so it does NOT know WHO made a transition. An approval
   reconstructed by the poller (webhook dropped) is attributed to the system
   principal `system:adapter-jira-poller`; the webhook, when it is not dropped,
   carries the real actor and wins because it arrives first (dedup by `event_id`).
   Tasks do not have this limitation (attributed to the reporter, which is stable).
3. **Transition ordering across workers**: the advisory lock guarantees Jira's hard
   invariant (never concurrent on the same ticket). Strict ordering of
   near-simultaneously enqueued transitions is preserved within a
   worker (by `id`); across distinct workers it is best-effort — in production
   a single transition worker runs by default.
4. **The restart gesture is the poller's alone**: only the sweep advances the
   generation in `jira_trigger_state`, so a restart takes up to one poll interval
   (default 60s). Wiring it into `jira:issue_updated` would make it immediate and
   is deliberately not done: Jira redelivers webhooks, and a delivery from before
   a removal still carries the label in its payload — letting it advance would
   mint an attempt nobody asked for. The webhook may still ingest a brand-new
   card, which is where latency actually matters.
5. **A refused gesture answers in the ledger, not on the ticket.** When the card
   has an attempt still in flight, the sweep records that and does not comment.
   Deliberate: a comment written by the adapter is indistinguishable from a
   human's to the poller (Jira attributes it to the token owner, and only
   comments created by the `MutableCommentWriter` are recorded in
   `comment_state`), so on a ticket waiting for a clarification the explanation
   would be read back as the answer — the BD-40 loop. Posting one safely means
   routing it through the same writer, which is a change to the status-comment
   contract rather than to this gesture.
6. **Label removal needs the *Edit Issues* permission.** Without it every removal
   fails, and the card keeps a label the DSE has already acted on. Nothing
   restarts — the latch stays armed precisely because the label never went
   absent — but the human's next gesture is lost: they take the label off and put
   it back between two sweeps, and the sweep sees present → present. The
   recourse is visible (the label is still on the card) and
   `jira_trigger_label_removal_failed` names the cause once per admission. Grant
   the permission, or expect a restart to need the label removed for longer than
   one poll interval.
7. **`project_statuses` costs one extra request** per queued transition whose
   target is not directly reachable, and it is deliberately not cached: an operator
   who creates the missing column must not have to restart the worker for the
   mirror to start working again.
8. **`SIGNAL_PLAN_APPROVAL` handler**: the dispatcher routes to the correct signal
   name (`dse_contracts.SIGNAL_PLAN_APPROVAL`); the corresponding
   `@workflow.signal` is being built by WS-B (WSB-E3-T2) in parallel. Until it exists, a
   routed approval is delivered to Temporal and (with no handler) dropped — with no error.
