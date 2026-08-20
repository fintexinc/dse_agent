# DSE scaling audit — N repos, N languages, backing services

**Date:** 2026-08-20 · **Method:** 5 parallel auditors (one scaling axis each),
every non-trivial finding adversarially verified against the cited code before
inclusion. **45 findings confirmed, 0 refuted** (16 high, 29 medium). Findings
already covered by the shipped rc.100–103 work or the planned Phase B/C/D
programme were excluded unless they carried new depth.

**The headline:** the operator's stated target — *"multiple repos in N languages
whose apps depend on a database, Redis, whatever"* — is blocked by two things
that **no planned phase addresses**: (1) the DSE has no concept of a *backing
service* anywhere (tests can't reach a DB, previews get one hardcoded Postgres),
and (2) the multi-language gate logic **silently inverts** for every dialect
beyond pytest/jest/surefire — green suites fail, and red lint/typecheck ships as
green. A third tranche (isolation/credentials) is latent today but becomes a real
breach the day tenant #2 or a second GitHub org onboards.

Recommended sequencing is at the end; it reorders the existing Phase B/C/D plan.

---

## Theme 1 — Backing services: the DSE has no concept of one *(the operator's ask)*

Today "the app needs a database" is expressible in exactly one place — the
preview — and only as *one hardcoded Postgres*. Tests that need a live service
have no path at all.

### 1.1 [HIGH] No execution path for tests that need a live backing service
The sandbox is a single hardened container (`k8s_driver.py:239-304`,
`readOnlyRootFilesystem`, gVisor, PSA `restricted` — no Docker, so testcontainers
can't run), behind a **default-deny egress** that allows only the egress-proxy,
model-gateway and DNS (`deploy/k8s/sandbox-isolation.yaml:35-81`). And the proxy
is an **HTTP** proxy — Postgres/Redis/AMQP wire protocols can't traverse it
(`egress_proxy/allowlist.py:82-111`), so even an *external* DB is unreachable.
`_run_suite` provisions nothing (`activities.py:3278-3316`). Worse, `connection
refused` scores as `GateStatus.FAIL` — a verdict on the code
(`quality_checks.py:115-139`) — and a NetworkPolicy DROP turns into a hang →
`rc=124 suite_hung`. **L1 runs the same suite in the same pod.**
→ *Any repo with integration tests (testcontainers, `@SpringBootTest`+datasource,
pytest+redis) can never go green: it defers at the Tester, reddens at L1, burns
paid Coder turns on an unfixable cause, and escalates.* The only escapes are
mocking everything or `disabled_stages:['test']` — i.e. shipping changes no suite
looked at.

### 1.2 [HIGH] Preview: one hardcoded Postgres, global DB name, closed schema
`kind=deployable` **unconditionally** deploys `postgres:16-alpine` with literal
`preview/preview` creds (`argocd.py:825-882`); the DB name is a *deployment-global*
env `DSE_PREVIEW_DB_NAME` default `"fee"` — one testbed's Hibernate catalog baked
into platform config (`config.py:1061-1065`), so a second Java repo with a
different catalog can't coexist. `_PREVIEW_FIELDS` is a closed set
(`config.py:398`) — `services` is a manifest error. A repo needing **Redis** →
CrashLoop → the rc.86 autofix loop spends Coder turns on an infra cause. A repo
needing **no DB** still pays for a Postgres pod. Extensions/version pins
(pgvector): unrepresentable. And invalid preview blocks **fail silently** —
`read_repo_preview` swallows the exception and drops the whole declaration
(`argocd.py:700-725`) while L1 never validates the block (see 5.1).

### 1.3 [MEDIUM] Two services can't talk in a preview
Sibling wiring is ui-kind only, one live sibling (`LIMIT 1`), `/api` prefix
hardcoded (`argocd.py:1499-1505,539-543`, `db.py:597-627`). A backend preview
gets no route to any sibling. `preview.env` values are static strings — no way to
inject a sibling's URL.

**The fix is one primitive, and the postgres block at `argocd.py:826-882` is the
template for it:** a manifest `services:` block (image, env, readiness, optional
persistence) consumed by **both** the sandbox pod (as sidecar containers, with
`DATABASE_URL`/`REDIS_URL` injected — the only shape that works under gVisor with
no Docker) **and** the preview namespace. Skip Postgres when the repo declares no
DB. This is bigger than the Phase D `preview.services` bullet, which targets only
the post-PR preview pod, not the test path.

---

## Theme 2 — Multi-language gates silently invert *(correctness/safety)*

Phase B fixes the tester *command* ladder and the `_test_counts` footer parser.
It does **not** fix these two, which are gate *inversions* — the most dangerous
kind of bug because the ledger lies.

### 2.1 [HIGH] L1 test gate hard-fails GREEN suites in cargo/dotnet/rspec/phpunit
`_COUNT_RE` and `_FOOTER_LINE_RE` only recognise pytest/jest/surefire shapes
(`quality_checks.py:380,433-437`). Cargo (`test result: ok. 5 passed`), .NET
(`Passed: 5` — word before digit), RSpec (`5 examples, 0 failures`), PHPUnit
(`OK (5 tests)`) all yield `counts=None`; the evidence rule then requires
`executed>0` and scores a fully green suite as **FAIL** (`:673-674,734-737`).
→ *A correct, green Rust/.NET/Ruby/PHP repo can never pass L1 test; the fix loop
is told the diff is at fault.*

### 2.2 [HIGH] Lint & typecheck publish PASS on RED runs when the dialect is unknown
`passed = (result.ok or changed_files is not None) and len(issue_lines) == 0`
(`quality_checks.py:248`, same at `:344`) drops the exit code whenever a diff
exists (the normal path). Only ruff/eslint-unix and mypy/tsc shapes are parsed.
Clippy (`--> src/x.rs`), checkstyle (`[ERROR] /abs:[12,5]`), **eslint's default
`stylish` formatter** (indented `12:5 error`) → zero lines parsed → `passed=True`,
`status=PASS` on a **red** run. → *A red gate is published as green in the ledger
and the PR proceeds — a silent inversion per ecosystem, including common Node
configs.*

The right fix for both is **structured evidence over stdout regexes** — have the
manifest command emit JUnit XML / SARIF and parse that; interim, `exit≠0 + no
parser hit` must be `ERROR ("dialect not recognised")`, **never** PASS/FAIL.
Supporting gaps in the same family (all MEDIUM): inherited-red suite-id regexes
know only jest+surefire so pytest/go never get the baseline comparison
(`:519-535`); the secret scanner's entropy rule needs *quoted* values, blind to
`.properties`/`.env`/unquoted YAML (`secret_scan.py:41-44`); Tester self-repair
(porta 5) recognises only jest+Maven failure dialects (`activities.py:2594-2616`);
the test-path write gate can't express Ruby `spec/`, .NET `*.Tests/`, Rust inline
(`paths.py:17-25`).

---

## Theme 3 — Isolation & credentials: latent now, a breach at tenant #2 *(security)*

None of this is in Phase B/C/D. It must land **before** a second tenant or a
second GitHub org.

### 3.1 [HIGH] Preview namespaces have no NetworkPolicy — lateral movement as root
`build_manifests` emits no NetworkPolicy, no pod-security labels, no
`securityContext` on a pod that runs `apt-get` (root) then executes PR-branch code
(`argocd.py:728-923`). Every helm policy is `Egress`-only (`networkpolicy.yaml`),
so **nothing stops a preview pod reaching `dse` `postgres:5432`, `temporal:7233`
(unauth), `vault:8200`, or another preview's Postgres** with the hardcoded
`preview/preview` creds. → *With tenant #2, a PR whose generated code scans the
cluster reads another tenant's DB or probes the control plane.*

### 3.2 [HIGH] Manifest strings f-string'd into pod YAML → injection with platform kube creds
`parse_repo_preview` only strips ends; interior newlines survive
(`config.py:511-536`). `argocd.py` f-strings the env **name** and **image** raw
into the Deployment (`:487,602,658`), read from the **PR branch** and applied with
the platform's cluster credentials. → *A prompt-injected Coder turn (or any
contributor editing the manifest on the `dse/*` branch) can inject extra
containers / hostPath volumes / objects in other namespaces.* Fix: validate env
names `^[A-Za-z_][A-Za-z0-9_]*$`, image against an OCI-ref regex, reject control
chars; long-term serialise YAML instead of f-strings.

### 3.3 [HIGH] One customer's private-feed PAT is injected into *every* sandbox and preview
`provision()` writes `MAVEN_FEED_TOKEN` into `/tmp/.m2/settings.xml` of **every**
sandbox unconditionally (`k8s_driver.py:420-431`); previews seed it into any
deployable namespace (`argocd.py:1523,1544`). The PAT also sits in the shared
`extraEnvSecret` mounted by 6 components. → *The day tenant #2 onboards, tenant
#1's PAT runs inside pods executing tenant #2's arbitrary build code.* (Same
shape for `DSE_MAVEN_FEED_ID` globally — this is one bug reported from two axes.)
Fix: deliver build creds **per work item, keyed by repo**, out of the shared
secret.

### 3.4 [HIGH] Single GitHub App installation hardwired at every mint site
The one `GITHUB_APP_INSTALLATION_ID` is read in three independent mint sites
(`egress-proxy/credentials.py:104,187`, `config.py:911-918`,
`sandbox_runtime/repo_clone.py:45`). → *Any repo under a second org/installation:
clone degrades to anonymous → 401 on private repos, PR finalize fails, preview
token fails — the whole loop dead, silently.* The repo→installation map **already
exists inbound** (`tenant_platform_bindings`, `migrations/0008_wsa2.sql:19-23`);
it just isn't threaded to the outbound side.

### 3.5 [HIGH] Egress allowlist: no per-repo path, and adding a host silently no-ops
The list is hand-edited helm values read **once at process start**
(`server.py:29-41`); the mounted `allowlist.yaml` is read by nothing; there's **no
config-checksum annotation**, so an allowlist-only `helm upgrade` changes the
ConfigMap but never rolls the pod. → *Every new repo repeats the
`download.eclipse.org` loop: build dies, operator edits values, upgrades, and the
fix doesn't take effect.* One-line interim fix: checksum annotation on the
egress-proxy pod template.

Related MEDIUMs: outbound Slack/Jira/Teams creds are one global Vault path each
(second workspace breaks outbound while inbound resolves — `adapter_slack/config.py:32-55`);
Vault has no tenant dimension and the most sensitive per-customer secrets bypass
Vault entirely; **no private npm/NuGet/PyPI support at all** — only a dangling
`npmrc` destination row (`build_credentials.py:81-98`).

---

## Theme 4 — Capacity & durability: breaks at ~2-3 concurrent, not N *(reliability)*

### 4.1 [HIGH] Nothing bounds live sandbox Pods — fairness cap 8 vs honest node capacity ~2
Fairness caps *activity concurrency* per tenant at 8 (`fairness.py:44`); the worker
has no `max_concurrent_activities` (SDK default 100); there's **no ResourceQuota**
and no live-pod count before provisioning. Pods outlive the working phase (TTL 72h,
teardown only on terminal). → *A 10-item day piles ~10 sandboxes against a ~2-pod
disk budget; kubelet disk-pressure eviction destroys the emptyDir workspace, and
an evicted sandbox is never transparently rebuilt.* Fix: admission control keyed
on live pods + a ResourceQuota backstop + decouple pod lifetime from review dwell.

### 4.2 [HIGH] `checkpointPvc` is a dead flag — turning it on bricks provisioning fleet-wide
`configmap.yaml:85` references a PVC **no template creates**; enabling it leaves
every sandbox `Pending` until the 120s wait fails. Even hand-created, all
sandboxes would mount the *same* claim at `/checkpoint.git`. → *The documented
mitigation for "work dies with the node" can't be turned on; the first attempt
takes down provisioning.* Fix: PVC template + per-work-item isolation + a render
conformance test.

### 4.3 [HIGH] Preview pods have no ephemeral-storage bounds
Preview resources declare only cpu/memory (`argocd.py:505-508,618-621`); the recipe
git-clones and `npm install`s into the container writable layer with no `emptyDir`
sizeLimit. → *2-3 live UI previews cross the nodefs eviction threshold; kubelet
evicts request-less pods first (recreate → re-clone churn loop), and the local-path
Postgres PVC shares the same filesystem.* The sandbox side already learned this
(`k8s_driver.py:106-128`); previews didn't get the lesson.

### 4.4 [HIGH] Aggregate spend cap is dormant
The monthly cap runs only if a `tenant_config` row exists, and **nothing inserts
one** (`dse_budget_hook.py:110-126`; `upsert_tenant_config` has zero callers). The
only brake is per-item (`$50 × coderRetryCap 8`); measured burn ~US$108/h. → *A hot
day across N repos can burn US$500-1000 with no aggregate brake.* Fix: seed
`tenant_config` at install; add a per-day ceiling.

Related MEDIUMs: single 2-CPU Postgres backs control-plane + Temporal + LiteLLM
keys with no tested restore; worker event loop blocked by sync `kubectl` in
provision/checkpoint/teardown (~5 min held); model gateway has no per-key rate
limits and one shared upstream API key, one replica; preview cap counts tenants
not the node and LRU-evicts previews still under human review; `resource_class` is
audited but ignored on k8s.

---

## Theme 5 — Manifest & runtime.image: the Phase-D naïveté *(design debt)*

### 5.1 [MEDIUM, but load-bearing] L1 never validates the `preview`/`forbidden_paths` blocks it whitelists
`_from_manifest_payload` whitelists the keys but never calls
`parse_repo_preview`/`parse_repo_forbidden_paths` (`config.py:788-906`); the only
reader swallows all exceptions → the whole block is dropped silently. → *A repo's
preview/protection config can be quietly wrong with no gate telling anyone.* Fix:
call the parsers inside `_from_manifest_payload` (the bootstrap gate reuses it, so
this hardens onboarding too); degrade per-field, not whole-block.

### 5.2 [HIGH] `runtime.image` (Phase D) can't be a stock language image
Every lifecycle op is `kubectl exec … python -m agent_runner` (`k8s_driver.py:386`);
the image vendors `agent_runner` + contracts, and `sast`/`secret_scan` exec
`bandit`/`python3` in-pod. → *`runtime.image: golang:1.22` ships broken.* Each new
language actually needs a **DSE-derived base image** built, published and kept in
lockstep with the runner — a release train Phase D doesn't name. Fix: define the
contract (`runtime.image` must extend a published `dse-runner-base`), or split
toolchain into a second container sharing the workspace.

### 5.3 [MEDIUM] Manifest evolution is a flag-day
`version==1` plus any unknown field/command/timeout key bricks **all** gates at
once (`config.py:792-822,600-610`), and manifests live in customer repos we don't
control. Adding Phase B's `commands.install` means old deployed parsers reject new
manifests. Fix: unknown *optional* fields degrade per-field with a note; or an
N/N+1 dual-accept window. Decide **before** shipping the next field.

Related MEDIUMs: Tester reads the manifest from the *mutable workspace* and ignores
`disabled_stages` (`activities.py:2997-3013`) while L1 reads the base SHA — Phase B
inherits both traps; monorepo-of-deployables has no manifest shape (one commands
set, one preview kind/port/glob); the timeout-budget arithmetic is hardwired to the
closed 4-command set; per-repo operational config has **no storage home** today
(`repo_profiles`/`tenant_config` carry none of it); onboarding a repo spans four
systems with four change mechanisms and no checklist.

---

## Recommended sequencing (reorders the current plan)

The current plan is B (tester manifest) → C (identity) → D (trigger-gated). This
audit says three things must jump the queue:

**Now / next rc — the two silent inversions (Theme 2.1, 2.2).** A false-green lint
gate ships broken code; a false-red test gate blocks correct code. Cheap, pure,
high-value, and every new language makes them worse. Do the "unknown dialect →
ERROR, never PASS/FAIL" guard first, then structured-evidence parsing.

**Before tenant #2 or a second GitHub org — the isolation tranche (Theme 3).**
Preview NetworkPolicies (3.1), manifest injection hardening (3.2), per-repo build
creds (3.3), repo→installation resolver (3.4), egress checksum annotation (3.5).
These are latent with one tenant and become real breaches with two. 3.2 and the
checksum are small; 3.1/3.3 are the substantial ones.

**The backing-services primitive (Theme 1) — the operator's actual ask.** One
design — a manifest `services:` block consumed by both the sandbox (sidecars for
tests) and the preview — closes 1.1, 1.2, and the Phase-D preview.services bullet
together. This is the feature that lets a DB/Redis app work at all; it's larger
than a bugfix and deserves its own rc, with the postgres block as the template and
the per-namespace Secret plumbing already in place.

**Then capacity (Theme 4) before actually running N repos in parallel** — admission
control on live pods (4.1), preview ephemeral bounds (4.3), the spend-cap seed
(4.4); fix or remove the `checkpointPvc` flag (4.2) so nobody bricks the fleet.

**Fold into whichever phase ships first:** the manifest self-validation (5.1) and
the evolution contract (5.3) — both are prerequisites for adding *any* new manifest
field safely.

Phase B/C/D still stand; this audit adds Theme 1 as a new phase, pulls Theme 2 and
Theme 3 ahead of them, and flags Theme 4 as the gate before real parallelism.
