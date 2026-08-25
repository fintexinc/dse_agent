# services/platform — WS-F (Security, compliance, platform and operations)

Phase 1 implementation of the WS-F P0 items. Read `../../CONVENTIONS.md`
first if you have not — this README assumes the vocabulary/contracts defined
there.

> **Phase 2 and Phase 3 live in sections at the end of this README**
> ("## Phase 2 — what was added", "## Phase 3 — what was added").
> Phase 1 below remains valid and intact. Current result of the full WS-F
> suite (Phases 1+2+3): `121 passed, 2 skipped` (the 2 skips are
> egress-proxy adversarial tests that require WS-C's real sandbox —
> inherited from Phase 1).

## What is implemented and working (against real infrastructure)

| Item | Where | Proof |
|---|---|---|
| **WSF-E1-T2 — reconstruction from audit** (Phase 1 exit criterion) | `packages/dse_audit/dse_audit/queries.py` (`reconstruct_work_item_history`, `export_audit_range`, `export_audit_range_csv`) | `packages/dse_audit/tests/test_queries.py` — writes the full sequence `admitted → clarified → plan → implementing → l1_passed → pr_opened → review_approved → merged` into real Postgres and proves that a single `SELECT ... ORDER BY ts` reproduces the exact order |
| **WSF-E2-T3a — secrets backend** | `dse_secrets/client.py` (`SecretsClient`, `get_secret`, `put_secret`, `delete_secret`) | `tests/test_dse_secrets_client.py` — runs against the real dev Vault (`localhost:8200`, `dse_dev_root`), put→get roundtrip, versioning, delete, and a clear error when the token is missing |
| **WSF-E2-T3a — plaintext secrets scanner** | `../../scripts/scan_for_plaintext_secrets.py` | `tests/test_scan_for_plaintext_secrets.py` (8 cases: Slack token, AWS key, PEM, known dev placeholder, `.env.example` ignored, an `os.environ/` reference is not a false positive, gitignored directory ignored) + actually run against the monorepo (see the "Real finding" section below) |
| **`tenant_config` — budgets/fairness/kill-switch** | `migrations/0007_wsf.sql` + `dse_platform/tenant_config.py` (`get_tenant_config`, `upsert_tenant_config`, `set_kill_switch`) | `tests/test_tenant_config.py` — idempotent upsert, kill-switch writes 2 audit rows (`kill_switch_enabled`/`kill_switch_disabled`) with `actor`/`reason`, against real Postgres |
| **WSF-E0 — platform CI** | `../../.github/workflows/ci.yml` | `lint` job (py_compile + best-effort ruff + secrets scanner) and `test` job (brings up the real infrastructure via `docker compose`, `scripts/migrate.py`, `pip install -e` for every `packages/*`/`services/*` with a `pyproject.toml`, `pytest -q packages services`) — YAML validated with `yaml.safe_load` |
| **WSF-E0 — contracts changelog** | `../../CONTRACTS-CHANGELOG.md` | Current versions (`dse_contracts`/`dse_audit`/`dse_identity` 0.1.0), the chief-architect approval rule for breaking changes, and the entry for the additive `dse_audit.queries` extension made in this session |
| **WSF-E5-T1/T2 — Helm chart (topology A)** | `../../infra/helm/dse/` | `helm lint infra/helm/dse` → 0 failures; `helm template` → 33 valid documents (parsed with `yaml.safe_load_all`), tested with 4 flag combinations (`secrets.externalSecrets.enabled`, `vault.externallyManaged`, `postgres.persistence.enabled`, `ingress.enabled`) — **real `helm` CLI installed and used** (not simulated) |
| **WSF-E5-T2 — upgrade runbook** | `../../infra/RUNBOOK-UPGRADE.md` | References (does not duplicate) `services/orchestrator/RUNBOOK.md` (WS-B) for Worker Versioning/drain-and-cutover |
| **WSF-E5-T2 — OSS BOM** | `../../infra/OSS-BOM.md` | Licenses for Postgres/Temporal/Redis/Vault/OTel + the main Python libraries, including an honest warning about Vault (BUSL) and Redis (RSAL/SSPL) |
| **WSF-E7-T1 — OTel collector** | `../../docker-compose.wsf.yml` + `../../infra/otel-collector-config.yaml` + `infra/helm/dse/templates/otel-collector.yaml` | Config validated (`docker compose config --quiet` → exit 0); receives OTLP grpc/http, uses the `dse_contracts.constants.OTEL_ATTR_*` attributes as the contract for whoever emits |
| **WSF-E7-T1 — alerting rules** | `../../infra/ALERTING-RULES.md` | 3 rules (budget exhaustion, unresolved egress denies, approaching the Temporal history limit) specified against the real OTel attributes |
| **WSF-E2 — egress proxy adversarial tests (sign-off role)** | `tests/test_egress_proxy_adversarial.py` | 14 cases written against the assumed interface (HTTP forward proxy on `:8806`) — **all SKIPPED** in this session because `services/egress-proxy` (WS-C) is not up yet (`localhost:8806` refuses the connection) |

## Real finding from the secrets scanner (not hidden)

Running `python3 scripts/scan_for_plaintext_secrets.py --root .` from the
monorepo root finds **1 real occurrence**:

```
services/model-gateway/litellm_config.yaml:53  [generic_api_key_assignment]  api_key: "sk-eco-local-dev-not-a-real-key
```

It is an obviously fake value (`not-a-real-key`, local `eco/echo-model` tier
with no cost, not a production secret) belonging to WS-D — outside the WS-F
directory, so I did not edit the file (co-existence rule: I only edit my own
directory). The scanner is working correctly (the "hardcoded key/password"
pattern matches even fake values, by design — deciding "this is acceptable"
has to be a human call, not a "looks fake" heuristic). I flagged it via a
separate task for WS-D to swap in an `os.environ/DSE_ECHO_API_KEY` reference
(consistent with the rest of the file).

The scanner uses `git ls-files` (tracked) UNION `git ls-files --others
--exclude-standard` (untracked but not ignored) as its universe of
"versionable" files — this avoids false positives in `.venv-*/` (which contain
dozens of example `api_key: os.environ/...` lines inside the installed
`litellm` package itself) without depending on anything actually being
committed yet (no workstream commits in this phase — `git status --porcelain`
shows only `??` untracked files).

## What runs on a local fixture/mock

- **Vault**: uses the foundation's `vault` in **dev** mode (`localhost:8200`,
  root token `dse_dev_root`) — dev mode does not persist to disk securely and
  uses a single unseal key. `dse_secrets.client.SecretsClient` works
  identically against a production Vault (real HTTP API via `hvac`); only the
  *server* is dev-mode in this session, not the client.
- **egress-proxy**: `services/egress-proxy` (WS-C) was not up when this suite
  was written and run — the 14 adversarial tests in
  `tests/test_egress_proxy_adversarial.py` skip with a clear reason. The exact
  interface (plain HTTP forward proxy vs. REST API) is an ASSUMPTION
  documented at the top of the file, not confirmed with WS-C.
- **Alerting backend**: `infra/ALERTING-RULES.md` is rule documentation, not
  live alerts — no Alertmanager/Datadog/Grafana is wired into the
  `otel-collector` (which today only does `debug` export/stdout).
- **Real budget (`dse.cost_usd`)**: no service is recording real provider cost
  yet (no AWS/Bedrock account provisioned — WS-D uses the local
  `eco/echo-model` tier, zero cost) — alerting rule 1 is specified but has no
  real data to fire on yet.

## What needs real credentials/infrastructure for production

- **Production Vault**: swap `vault.devMode: true` (chart) for
  `vault.externallyManaged: true` pointing at the customer's real HA Vault (or
  OpenBao, see `infra/OSS-BOM.md`). A production `VAULT_TOKEN` must never be
  the root token — create a dedicated per-service policy.
- **External Secrets Operator**: `secrets.externalSecrets.enabled: true`
  requires ESO installed in the customer's cluster (the `ExternalSecret` CRD)
  — the chart renders the manifest but does not install the operator (out of
  scope for this application chart).
- **Real alerting backend**: the customer's choice (Grafana/Datadog/
  Alertmanager) + the corresponding exporter in the `otel-collector` — see the
  final section of `infra/ALERTING-RULES.md`.
- **Real K8s cluster**: no customer cluster was available in this session —
  the Helm charts were validated with `helm lint`/`helm template` (correct
  syntax and rendering) but **never applied with `helm install` against a real
  cluster**. The `helm CLI` was installed in this environment (`brew install
  helm`, version 4.2.3) specifically for that validation.
- **WS-C's real egress-proxy**: re-run `tests/test_egress_proxy_adversarial.py`
  as soon as `services/egress-proxy` comes up on `:8806` — and review the
  interface assumptions documented at the top of the file against what WS-C
  actually publishes (they may not match, especially the token
  reuse/credential-broker tests, which depend on an API contract that is not
  documented yet).

## `dse_secrets` — stable consumption contract (cross-workstream)

WS-A, WS-C and WS-D should import this to read webhook secrets/service
tokens/provider credentials instead of plaintext env vars:

```python
from dse_secrets import get_secret, put_secret, SecretsClient

# simple usage (builds a client from VAULT_ADDR/VAULT_TOKEN)
creds = get_secret("dse/slack/webhook")        # -> {"signing_secret": "..."}
put_secret("dse/github-app/private-key", {"pem": "..."})

# heavy usage (reuses the connection)
client = SecretsClient()                        # or SecretsClient(vault_addr=..., token=...)
client.get_secret("dse/model-gateway/bedrock")
client.delete_secret("dse/rotated-key")          # soft-delete, KV v2 keeps history
```

Configuration (env vars — never hardcode):

- `VAULT_ADDR` (default `http://localhost:8200`)
- `VAULT_TOKEN` (production) or `VAULT_DEV_ROOT_TOKEN` (local dev only)
- `VAULT_KV_MOUNT` (default `secret` — the KV v2 mount that dev Vault brings
  up by default; production should use a dedicated mount)

It raises `dse_secrets.VaultUnavailableError` (it never lets an
`hvac`/`requests` exception escape without context) on any failure — missing
path, invalid token, Vault down.

## `dse_platform.tenant_config` — budgets/fairness/kill-switch

```python
from dse_platform import get_tenant_config, upsert_tenant_config, set_kill_switch

cfg = upsert_tenant_config("acme", monthly_budget_usd=500)
set_kill_switch("acme", enabled=True, reason="budget exceeded", actor="system:budget-monitor")
```

Every kill-switch change writes an audit row (`kill_switch_enabled`/
`kill_switch_disabled`) through `dse_audit.emit` in the same transaction —
never silently (P8). Enabling the kill switch without a `reason` raises
`ValueError`.

## `dse_audit.queries` — additive WS-F extension on the foundation package

Process note (see `../../CONTRACTS-CHANGELOG.md` for the full text): this
program's general co-existence instructions say not to edit
`packages/dse_audit` (it is listed as "shared foundation" in the boilerplate
common to every workstream). But `CONVENTIONS.md` — the document that this
same process told us to read first as the source of truth — is explicit:
*"packages/dse_audit/ | Foundation (minimal) → **WS-F extends it**"*, and task
WSF-E1-T2 literally asks for `packages/dse_audit/dse_audit/queries.py`. I
followed `CONVENTIONS.md` (more specific, and the mandatory step-0 document),
and kept the change to the smallest possible additive footprint to reduce the
risk of collision with the other 5 workstreams editing in parallel:

- **I did not touch** `dse_audit/client.py` (the only write path, `emit`,
  stays exactly as it was).
- **I only added** one new file (`dse_audit/queries.py`) and one new test
  (`tests/test_queries.py`).
- **In `__init__.py`** I only appended the 3 new symbols to the existing ones
  — `emit` and `get_connection` are still exported, nothing was removed or
  renamed (the "additive is always allowed" rule from
  `CONTRACTS-CHANGELOG.md`).

If this gets reverted during consolidation (because the chief architect
decides the strict reading of the forbidden-directory list should have won),
the code is isolated enough to be removed without touching anything else.

## How to run the tests

```bash
# 1. isolated WS-F venv (do not reuse the foundation's .venv/)
python3.12 -m venv /Users/saraiva/Documents/DSE/fase1/.venv-wsf
source /Users/saraiva/Documents/DSE/fase1/.venv-wsf/bin/activate

# 2. install the foundation packages + this service
pip install -e packages/contracts -e packages/dse_audit -e packages/dse_identity
pip install -e services/platform
pip install pytest

# 3. env vars (infrastructure is already up — see CONVENTIONS.md)
export DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse
export DSE_AUDIT_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse
export DSE_PLATFORM_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse
export VAULT_ADDR=http://localhost:8200
export VAULT_DEV_ROOT_TOKEN=dse_dev_root

# 4. apply the WS-F reserved migration (idempotent)
python3 scripts/migrate.py

# 5. run the tests
pytest -q packages/dse_audit services/platform

# 6. validate the Helm chart (requires `helm` — installed via `brew install helm`
#    in this session; if absent, at least run the manual YAML validation)
helm lint infra/helm/dse
helm template dse-test infra/helm/dse | python3 -c "import yaml,sys; list(yaml.safe_load_all(sys.stdin))" && echo "YAML OK"

# 7. secrets scanner
python3 scripts/scan_for_plaintext_secrets.py --root .
```

## Real result (last run in this session)

```
$ pytest -q packages/dse_audit services/platform
.............ssssssssssssss.............                                 [100%]
26 passed, 14 skipped in 1.81s
```

The 14 skipped are the egress-proxy adversarial tests (`services/egress-proxy`
from WS-C was not answering on `localhost:8806` at run time — not a failure,
it is the documented expected skip). **Zero failing tests.**

```
$ helm lint infra/helm/dse
==> Linting infra/helm/dse
[INFO] Chart.yaml: icon is recommended
1 chart(s) linted, 0 chart(s) failed
```

```
$ python3 scripts/scan_for_plaintext_secrets.py --root .
[scan_for_plaintext_secrets] FAILED — 1 possible plaintext secret(s):
  services/model-gateway/litellm_config.yaml:53 [...]
```
(exit code 1 — correct and expected behavior; see "Real finding" above).

## Structure

```
services/platform/
  dse_secrets/          Vault client (WSF-E2-T3a)
  dse_platform/          tenant_config (budgets/fairness/kill-switch)
  tests/
    test_dse_secrets_client.py
    test_scan_for_plaintext_secrets.py
    test_tenant_config.py
    test_egress_proxy_adversarial.py   (WSF-E2 — sign-off, skips if WS-C is down)
  pyproject.toml

packages/dse_audit/dse_audit/queries.py   (additive WSF-E1-T2 extension)
packages/dse_audit/tests/test_queries.py  (reconstruction exercise)

migrations/0007_wsf.sql   (tenant_config)

scripts/scan_for_plaintext_secrets.py

infra/
  helm/dse/              Helm chart (topology A)
  otel-collector-config.yaml
  RUNBOOK-UPGRADE.md
  OSS-BOM.md
  ALERTING-RULES.md

docker-compose.wsf.yml   (otel-collector)

.github/workflows/ci.yml
CONTRACTS-CHANGELOG.md
```

---

## Phase 2 — what was added

WS-F Phase 2 ("access bundles, ADR-22/SSO, multi-tenant isolation, queue
board") is **additive** on top of Phase 1. Nothing from Phase 1 was
removed/renamed. Reserved migration: `migrations/0013_wsf2.sql`. New port:
**8890** (queue board).

### Delivery map (Phase 2)

| Task | Where | Proof |
|---|---|---|
| **WSF-E3-T2 — Per-tenant/channel access bundles** | `dse_platform/access_bundles.py` + `migrations/0013_wsf2.sql` (`dse_access_bundle`) | `tests/test_access_bundles.py` — CRUD, channel-over-default resolution, deny-by-default (no bundle denies repo/mode), `blocked_actions` (e.g. `direct_merge_to_protected_branch`), **an empty approver cascade BLOCKS** (`NoApproverError`, P3), an offboarded user is removed from the cascade |
| **WSF-E3-T3 — ADR-22 + console SSO/OIDC** | `infra/ADR-22-identity.md` (design), `dse_platform/sso.py` (`OIDCVerifier`/`login`/`offboard`/`provision_console_user`), `dse_platform/dev_idp.py` (dev OIDC IdP), login in `queue_board/app.py` | `tests/test_sso.py` — real RSA verification (signature/iss/aud/exp), account matching on a stable `sub`, JIT login, **offboarding denies login AND removes from approver resolution**, contractor expiry |
| **WSF-E4-T3 — Multi-tenant isolation suite (NFR-03)** | `dse_platform/tenant_isolation.py` | `tests/test_tenant_isolation.py` — layer by layer (queues/fairness keys, artifacts/prefixes, skills, retrieval, audit, tokens) with **ACTIVE cross-tenant attempts** that fail (`CrossTenantViolation`) and are audited (`cross_tenant_access_denied`) |
| **WSF-E6-T1 — Queue board API** | `dse_platform/queue_board/api.py` | `tests/test_queue_board.py` — §9.3 projection (all states), `to_public_status` reused, budgets + aggregated cost, `active_work_items`, quarantine, audit trail |
| **WSF-E6-T2 — Operator controls → Temporal signals** | `dse_platform/queue_board/operator.py` + `signals.py` + `dse_platform/kill_switches.py` | `tests/test_queue_board.py` + `tests/test_kill_switches.py` — pause/resume/cancel/retry/reassign model+runtime/force_clarification/escalate/quarantine + **kill switches at all 4 scopes** (global/tenant/channel/task); every action audited with the operator's identity; the intent is audited even if the signal fails |
| **WSF-E6-T3 — Minimal UI (server-rendered, 8890)** | `dse_platform/queue_board/app.py` + `asgi.py` + `services/platform/Dockerfile` + `docker-compose.wsf.yml` fragment | `tests/test_queue_board_app.py` — SSO gate (401 without a session), per-tenant page with all §9.3 states, end-to-end control (form POST → OperatorConsole → signal + audit), offboarding kills the session on the next request (403) |
| **Populates `tenant_config` for fairness (WS-B)** | `dse_platform/tenant_isolation.fairness_key` + `tenant_config` (Phase 1) | fairness key namespaced per tenant (`tenant::<id>`), read by WS-B; the suite proves it does not collide across tenants |

### Non-negotiable principles in this phase

- **P1 (deterministic-or-human):** every enforcement decision
  (repo/mode/action/admission/cross-tenant) is a set/string comparison in code
  — no LLM. The plan gate **never auto-approves**: an empty approver cascade
  raises `NoApproverError` (`require_plan_approver`).
- **P3 (no producer approves own work):** WS-B's approver cascade falls back
  to the bundle's `designated_approvers`; empty = blocked.
- **P6 (decline-never-truncate):** enforcement fails cleanly at the boundary
  (`AccessDenied`/`CrossTenantViolation`/`LoginDenied`), never halfway.
- **P8 (evidence over assertion):** every consequential decision (bundle
  upsert, access denial, kill switch change, offboarding, operator action,
  cross-tenant violation) writes audit through `dse_audit.emit`.

### Phase 2 consumption contracts (cross-workstream)

```python
# WS-A (ingest-gateway) and WS-D (model-gateway): check admission at all 4 scopes
from dse_platform import is_admission_blocked
block = is_admission_blocked(tenant_id, channel)      # None = admissible
if block: refuse(block.scope, block.reason)            # 'global'|'tenant'|'channel'

# WS-A/WS-C: allowed repos; WS-B: modes and blocked actions
from dse_platform import require_repo_allowed, check_mode_allowed, require_action_allowed
require_repo_allowed(tenant_id, "org/repo", channel=ch, work_item_id=wid)
require_action_allowed(tenant_id, "direct_merge_to_protected_branch", channel=ch)

# WS-B (plan gate, WSB-E3-T2): CODEOWNERS -> designated approvers cascade
from dse_platform import require_plan_approver          # raises NoApproverError if empty
approvers = require_plan_approver(tenant_id, channel=ch, codeowners=owners, work_item_id=wid)

# WS-B (worker-side fairness): per-tenant namespacing key
from dse_platform import fairness_key                   # "tenant::<id>", never collides

# WS-C: tenant-scoped enforcement of skills/retrieval (denies + audits cross-tenant)
from dse_platform import fetch_skill_scoped, query_retrieval_scoped
```

### SSO — how to plug in a real IdP

The console validates an OIDC `id_token` (RS256) against the IdP's JWKS.
Configured by env (see `queue_board/asgi.py`):

```
DSE_OIDC_ISSUER=https://login.customer.com
DSE_OIDC_AUDIENCE=dse-admin-console          # client_id
DSE_OIDC_JWKS_FILE=/etc/dse/idp-jwks.json    # or DSE_OIDC_JWKS='{"keys":[...]}'
DSE_CONSOLE_SESSION_SECRET=<>=32 bytes>
```

Without these vars login is disabled (503) — appropriate for a deployment with
no IdP yet. In dev/test, `dse_platform.dev_idp.DevIdP` mints id_tokens + JWKS
to exercise the same verifier (see `tests/test_sso.py`).

### Honest gaps (fixture/mock or external dependency — Phase 2)

- **No real IdP (Keycloak/Okta/Entra/Ping) provisioned in this session.** The
  OIDC contract (RSA signature + `iss/aud/exp`) is genuinely exercised against
  `DevIdP` (real RSA keypair, `PyJWT` + `cryptography`). For production: point
  `OIDCVerifier` at the IdP's `jwks_uri` and swap the `/login` handler for an
  OIDC redirect (authorization code flow) — verify/session/offboarding do not
  change. **SAML** comes in through an OIDC broker (Keycloak/Dex/oauth2-proxy)
  in front — the console does not parse SAML (see ADR-22 §1). Real **SCIM**
  (automatic role provisioning) is a per-customer integration; the schema
  (`dse_console_identity.roles`) already supports it, the SCIM endpoint is not
  included.
- **SSO × chat/VCS account matching is not unified** — the foundation's
  `identity_links` CHECK `platform IN ('slack','github','jira')` (0001, not
  editable in this phase) prevents writing `platform='sso'`. SSO principals
  are created directly in `principals` via `sso.ensure_sso_principal`, with the
  matching held in `dse_console_identity.sso_subject`. Documented as debt in
  ADR-22 §2 — resolvable once the foundation adds `'sso'` to the CHECK.
- **Real signal delivery from the queue board (`TemporalSignalSender`)**
  requires `temporalio` (the `[temporal]` extra) and a live workflow. The tests
  use `FakeSignalSender` (marked) — the validation + audit + durable state path
  (quarantine/kill switch) is 100% real; only the signal transport is fake, so
  we do not need a workflow per test. Run against real Temporal at
  consolidation.
- **Current budget spend** (`get_tenant_budget.spent_usd`) is aggregated from
  audit rows via `details->>'cost_usd'` (e.g. `coder_turn_completed`) — the
  same source the OTel collector consumes. It is an honest approximation while
  no real provider records cost (the same Phase 1 limitation; no AWS/Bedrock
  account — WS-D uses the local `eco/echo-model` tier, zero cost).
- **UI with no design system, by decision (WSF-E6-T3):** raw HTML assembled in
  Python, tables, POST forms. It is an operations tool, not a product.
  `python-multipart` is the only new dependency, and only for forms.
- **The channel kill switch writes to WS-A's `channel_kill_switches` table**
  (data plane, same database) — we did not edit WS-A's file/migration. The
  composite `is_admission_blocked` reads global (ours) → tenant (ours) →
  channel (WS-A).

### New dependencies (Phase 2)

`PyJWT>=2.8`, `cryptography>=42` (OIDC RS256 verification), `python-multipart`
(queue board forms), optional extra `temporalio>=1.7` (`[temporal]`, real
signal delivery). Reinstall with:
`pip install -e "services/platform[temporal]"`.

### Running the queue board locally

```bash
# via docker (the WS-F fragment already declares the queue-board service on 8890)
#   do NOT run make up (it would tear down the other agents' infrastructure) — build only this one:
docker compose -f docker-compose.yml -f docker-compose.wsf.yml build queue-board
docker compose -f docker-compose.yml -f docker-compose.wsf.yml up -d queue-board
# or directly with uvicorn (WS-F venv):
uvicorn dse_platform.queue_board.asgi:app --port 8890
```

### Structure added (Phase 2)

```
services/platform/
  dse_platform/
    access_bundles.py        (WSF-E3-T2)
    sso.py                    (WSF-E3-T3 — OIDC verify, login, offboard)
    dev_idp.py                (WSF-E3-T3 — dev OIDC IdP, fixture)
    kill_switches.py          (WSF-E6-T2 — 4 scopes + quarantine)
    tenant_isolation.py       (WSF-E4-T3 — layer-by-layer enforcement)
    queue_board/
      api.py                  (WSF-E6-T1 — §9.3 projection, budgets, trail)
      signals.py              (WSF-E6-T2 — real/fake SignalSender)
      operator.py             (WSF-E6-T2 — controls + per-operator audit)
      app.py                  (WSF-E6-T3 — FastAPI + minimal HTML, SSO gate)
      asgi.py                 (uvicorn entrypoint on 8890)
  Dockerfile                  (queue board image)
  tests/
    test_access_bundles.py  test_sso.py  test_kill_switches.py
    test_tenant_isolation.py  test_queue_board.py  test_queue_board_app.py

migrations/0013_wsf2.sql      (dse_access_bundle, dse_console_identity,
                               dse_kill_switch_global, dse_work_item_quarantine)
infra/ADR-22-identity.md      (SSO/SCIM/offboarding design doc)
docker-compose.wsf.yml        (+ queue-board service on 8890)
```

---

## Phase 3 — what was added

WS-F Phase 3 ("Evidence"): **ADR-28 complete** (scheduled secret rotation +
preview secrets via ESO), **retention by data classification**
(WSF-E8-T2/§12.2) and **activation of the Temporal history alert**
(ALERTING-RULES §3). All additive on top of Phases 1+2. Reserved migration:
`migrations/0018_wsf3.sql`. No new ports.

### Delivery map (Phase 3)

| Task | Where | Proof |
|---|---|---|
| **WSF-E2-T3b(a) — SCHEDULED rotation of service secrets** | `dse_platform/secret_rotation.py` (`rotate_secret`/`rotate_from_manifest`) + `dse_platform/jobs_scheduler.py` + the `platform-jobs` service in `docker-compose.wsf.yml` | `tests/test_secret_rotation.py` — against the REAL Vault: **a concurrent active reader looping through 5 rotations sees zero error window** (the task's literal acceptance criterion), 1 audit row per rotation (`service_secret_rotated`) that NEVER leaks the material, an identical/empty generator is refused (P6), the manifest isolates failures, the scheduled entrypoint is exercised. Also run INSIDE the container (`docker exec dse_platform_jobs python -m dse_platform.jobs_scheduler --once` → rotated `dse/service/queue-board-session` v1→v2) |
| **WSF-E2-T3b(b) — preview secrets via ESO** | `infra/k8s-local/setup-eso.sh` (ESO **pinned to 2.8.0** via helm, ACTUALLY installed in the `dse-preview` k3d cluster) + `infra/k8s-local/eso/*.yaml` (ClusterSecretStore `dse-vault` + example) | `tests/test_eso_preview_secrets.py` — a k8s Secret **materializes in a preview namespace from the compose Vault** (dse_net network, `http://vault:8200`), rotation in Vault propagates within the refreshInterval, and a **negative scope test**: an ExternalSecret pointing at `dse/service/*` NEVER becomes Ready (the ESO token is scoped by the `dse-preview-read` policy to `secret/data/dse/preview/*` — no root token ever enters the cluster) |
| **WSF-E8-T2 — retention by classification** | `dse_platform/retention.py` + `migrations/0018_wsf3.sql` (`tenant_config.retention` JSONB + index on `ingest_events.received_at`) | `tests/test_retention.py` (16 tests, real Postgres) — per-tenant/class policy with shape validation, anonymization of `ingest_events.payload` (tombstone; only `processed`, only that class, idempotent), purge of `wse_artifacts` (JOIN `work_items` for data_class; **quarantined items are never purged**; deleted keys land in the audit row for compensating cleanup in Garage), dry-run with no mutation, **audit_log refused as a target in code** + old audit rows survive, one tenant's failure does not abort the sweep (audited as `retention_failed`) |
| **ALERTING-RULES §3 ACTIVATED — Temporal history** | `infra/otel-collector-config.yaml` (`metrics/history_alert` pipeline: OTTL `filter` + severity `transform` + `debug/history_alert` exporter) | `tests/test_history_alert.py` — real OTLP against the foundation collector: above the threshold it shows up on the alert channel with the correct `dse.alert_severity=warning|critical` (both events AND bytes); below the threshold, and non-history metrics, NEVER leak through |

### P7 decision (requested by the acceptance criteria): Python scheduler in compose, not a CronJob in k3d

Full rationale in the `dse_platform/jobs_scheduler.py` docstring. Short
version: the consumers of the service secrets (adapters/gateway/broker) and
the Postgres targeted by retention live in docker-compose — scheduling in the
cluster would create a cross-runtime dependency with no upside. The SAME module
runs as a CronJob on real K8s (`python -m dse_platform.jobs_scheduler --once`
— tested). Temporal Schedules was rejected because it would couple rotation to
the availability of one of its own indirect consumers.

### Phase 3 consumption contracts (cross-workstream)

```python
# WS-E (previews, WSE-E4-T10): preview secrets via ESO —
#   secretStoreRef: { kind: ClusterSecretStore, name: dse-vault }
#   Vault paths: secret/dse/preview/<...>   (KV v2, mount "secret")
# (live example: kubectl -n dse-preview-example get externalsecret)

# WS-E (artifact store lifecycle): retention policy is single-sourced from here
from dse_platform import get_retention_policies, set_retention_policy, run_retention

# rotation, for any service that needs to swap an internal secret
from dse_platform import rotate_secret
rotate_secret("dse/service/my-secret", actor="system:secret-rotator")
```

### Requests filed for other workstreams (we edited nothing of theirs)

- **WS-E**: (1) `GRANT DELETE ON wse_artifacts TO dse_app` at integration time
  — until then the real artifact purge reports `skipped` with an explicit
  reason (dry-run/counting works; the test proves the purge using a privileged
  connection); (2) consume `purged_store_keys` from the `retention_executed`
  audit row to delete the corresponding objects in Garage.
- **WS-B**: pin the canonical name of the history metric (the filter accepts
  `dse.workflow.history_length`/`history_size_bytes` and the
  `temporal_workflow_event_history_*` variants — see the updated
  ALERTING-RULES §3).
- **Foundation**: promote the metric name into `dse_contracts.constants`
  (`OTEL_METRIC_HISTORY_LENGTH`) in the next contract window.

### Honest gaps (Phase 3)

- **Rotation of external PROVIDER credentials** (Slack bot/GitHub App/Jira):
  the mechanism (KV v2 versioning + verification + audit + schedule) is
  complete and proved; issuing the new credential through the provider's API
  requires real apps (inherited administrative blocker) — it is a matter of
  implementing one `Generator` per integration (interface documented in
  `secret_rotation.py`).
- **Vault is still dev-mode** (foundation, Phases 1-3): hardening the
  deployment (HA/auto-unseal) is production infrastructure — the
  ESO/policies/scoped-tokens path is already the production one.
  `setup-eso.sh --rotate` rotates the ESO token.
- **History alert**: the MVP channel is the collector's stdout (a line with
  `dse.alert=...`); the upgrade to Alertmanager is documented in
  ALERTING-RULES §3. CONTINUOUS emission of the metric is WS-B's job (in
  parallel in this phase); the test proves the pipeline with real OTLP emitted
  by the test itself.
- **k3d/ESO unavailable** ⇒ the ESO tests skip with an explicit reason (the
  same pattern as the egress-proxy in Phase 1) — run
  `infra/k8s-local/setup-k3d-argocd.sh` and `infra/k8s-local/setup-eso.sh`.

### Running (Phase 3)

```bash
source .venv-wsf/bin/activate   # same venv as the previous phases
export DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse \
       DSE_AUDIT_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
       DSE_PLATFORM_DATABASE_URL=postgresql://dse_app:dse_app_dev_only@localhost:5432/dse \
       VAULT_ADDR=http://localhost:8200 VAULT_DEV_ROOT_TOKEN=dse_dev_root
python scripts/migrate.py                      # applies 0018_wsf3.sql
./infra/k8s-local/setup-eso.sh                 # ESO 2.8.0 + SecretStore + example
pytest -q packages/dse_audit services/platform # 121 passed, 2 skipped

# scheduled jobs (compose)
docker compose -f docker-compose.yml -f docker-compose.wsf.yml up -d platform-jobs
docker exec dse_platform_jobs python -m dse_platform.jobs_scheduler --once  # CronJob mode
```

### Structure added (Phase 3)

```
services/platform/
  dse_platform/
    secret_rotation.py       (WSF-E2-T3b(a) — zero-downtime rotation + audit)
    retention.py              (WSF-E8-T2 — per-class policy + purge/anonymization)
    jobs_scheduler.py         (scheduler; compose service OR CronJob --once)
  tests/
    test_secret_rotation.py  test_retention.py
    test_eso_preview_secrets.py  test_history_alert.py

migrations/0018_wsf3.sql      (tenant_config.retention + received_at index)
infra/k8s-local/setup-eso.sh  (ESO 2.8.0 pinned + scoped policy/token)
infra/k8s-local/eso/          (ClusterSecretStore dse-vault + preview example)
infra/otel-collector-config.yaml  (+ metrics/history_alert pipeline — §3 ACTIVE)
docker-compose.wsf.yml        (+ platform-jobs service)
```

## Phase 4 — what was added (loop hardening & learning)

WS-F Phase 4 is the **security pilot gate package** plus the Webex scope decision. No new
platform code in `dse_platform/` — WS-F Phase 4 is **formal security documentation + an
executable red-team suite** that ATTACKS the controls already built (it does not rewrite
them).

### Delivery map (Phase 4)

| Task | Deliverable | State |
|---|---|---|
| **WSF-E8-T1** threat model + data flow | `infra/THREAT-MODEL.md` — a threat→control→test matrix per component + mermaid diagrams for Tier 1 (PrivateLink) / Tier 2 (air-gapped) | Complete; every row cites a real file+test; honest gaps listed (§4) |
| **WSF-E8-T3** red-team program | `infra/RED-TEAM-PROGRAM.md` (owner/cadence/scope/manual items) + `services/platform/tests/test_red_team.py` (21 executable attacks) | Complete; 21/21 passing against real infrastructure |
| **WSF-E5-T3** topology B | `infra/helm/dse/values-topology-b.yaml` + `templates/model-server.yaml` + `infra/helm/dse/TOPOLOGY-B.md` (NFR-08 × N cost) | Complete; `helm lint`+`template` validate both A and B |
| **Webex decision (ADR-25)** | `infra/ADR-25-webex-decision.md` — formal de-scope with sign-off + how to reverse it | Complete (pending architect/stakeholder ratification) |

### The red-team suite (`tests/test_red_team.py`) — what it actually ATTACKS

Attacks against REAL controls (not mocks), with a clear skip when the target control is not
present in the environment (P6/P8 — "could not verify" beats a false positive):

- **`TestForgedWebhook`** → the HMAC in `ingest_gateway.security` (WS-A): forged signature /
  wrong key / missing / out-of-window replay are all refused; a positive control proves it is
  not just "always deny".
- **`TestPromptInjection`** → `ingest_gateway.sanitize` (invisible/bidi unicode + a planted
  secret) AND the real containment: default-deny egress (:8806) refuses exfiltration to
  pastebin/telegram/cloud metadata plus host-confusion bypasses.
- **`TestCrossTenant`** → `dse_platform.tenant_isolation`: A reads B's skill/retrieval/audit/token
  → `CrossTenantViolation` + a `cross_tenant_access_denied` audit row; artifact path traversal
  blocked.
- **`TestMaliciousSkill`** → `sandbox_runtime.skill_promotion` (WS-C, ties into WSC-E4-T3): a
  candidate tries to become `active`/`approved` with no human approver → `ApproverRequired`; a
  candidate is never served to the Planner (`read_approved_skills`).

How to run it (WS-F venv, infrastructure up):
```bash
export DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse
pytest -q services/platform/tests/test_red_team.py     # 21 passed
```

### Validating the Helm topologies (Phase 4)
```bash
helm lint infra/helm/dse
helm lint infra/helm/dse -f infra/helm/dse/values-topology-b.yaml
helm template dse-acme infra/helm/dse                                              # topology A
helm template dse-acme infra/helm/dse -f infra/helm/dse/values.yaml \
    -f infra/helm/dse/values-topology-b.yaml                                       # topology B
```
Topology B enables the air-gapped in-cluster `model-server` (GPU), forces self-hosted
Postgres/Temporal/Vault and an internal-only egress allowlist. Operating cost is documented in
`TOPOLOGY-B.md` (NFR-08 × N — with no amortization, the dedicated per-customer GPU is the
dominant driver).

### Honest gaps (Phase 4)

- **Real credentials** (GitHub App/Slack/Jira/AWS-Bedrock) are still absent — signing and
  PrivateLink have production logic but run against an env/fixture/echo secret. Administrative
  pilot gate (addendum 03 §Part 3).
- **Supply chain**: no SBOM/image signing/CVE scanning in CI — the highest-priority manual item
  in RED-TEAM-PROGRAM (§5).
- **Credential replay against a real upstream** and **the console with no real IdP** remain
  manual items in the program (documented in §5, not hidden).
- **The air-gapped `model-server`** is validated packaging (lint/template); the real serving
  image is P2 (WSD-E5-T2/T3) and does not block the pilot.

### Structure added (Phase 4)

```
infra/THREAT-MODEL.md                    (WSF-E8-T1 — matrix + Tier 1/2 data flow)
infra/RED-TEAM-PROGRAM.md                (WSF-E8-T3 — owner/cadence/scope/manual items)
infra/ADR-25-webex-decision.md           (formal de-scope + reversal)
infra/helm/dse/values-topology-b.yaml    (WSF-E5-T3 — strict overlay)
infra/helm/dse/templates/model-server.yaml  (air-gapped model, gated on modelServer.enabled)
infra/helm/dse/TOPOLOGY-B.md             (NFR-08 × N operating cost)
infra/helm/dse/values.yaml               (+ modelServer block, disabled by default — A intact)
services/platform/tests/test_red_team.py (21 executable attacks)
```

No new migration in WS-F Phase 4: the red-team suite reuses the existing tables
(`skill_registry`/`skill_episode` from 0019, `virtual_keys`, `audit_log`). `0020_wsf4.sql` was
reserved but turned out not to be needed.
