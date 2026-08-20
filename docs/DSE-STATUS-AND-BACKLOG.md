# DSE — Current State & Change Backlog

2026-08-12 · Written for Confluence. Everything here is verified against the code (10-agent code sweep, file:line evidence in `docs/REMAINING-WORK.md`) or against the production database — nothing is aspirational. Deployment basis: **rc.88** on the POC VPS.

---

## 1. Summary

The DSE (Development Software Engineer) is an autonomous engineering engine: a request typed in Slack (or a labeled GitHub issue) becomes a work item that is planned, implemented and tested inside an isolated sandbox, validated by automated gates, opened as a GitHub PR with evidence, and served as a live per-PR preview environment. A human always reviews and merges; the DSE never approves its own work.

Where it genuinely stands today:

- **The full pipeline works end to end on Kubernetes** — including, as of this week, the review fix cycle and an autonomous repair loop for failed previews.
- **No work item has ever been merged.** 122 items processed since 2026-07-24; 26 PRs opened; 0 completed the loop through human merge. One item is currently in review.
- **The quality bottleneck is the generated test code** (Tester writing specs against APIs that don't exist), not the infrastructure — the infrastructure failure modes found this week were each fixed the same day.
- **The biggest product gap is the channel experience**: Slack can start work but can't steer it, approvals are effectively invisible, and links/cost/progress don't reach the people who asked.

## 2. What the DSE does today (verified flow)

1. **Trigger** — `@dse <request>` in a bound Slack channel, or a `dse`-labeled GitHub issue on a bound repo. (Jira works by polling; Teams is a stub — see matrix.)
2. **Intake** — deterministic clarification gate; multi-repo requests fan out to sibling items per repo.
3. **Plan** — Planner produces a plan; high-risk items park at a human approval gate (Slack button; 72h timeout).
4. **Implement** — Coder and Tester run as agent turns inside a gVisor-sandboxed pod. Model calls go through the model gateway (per-stage virtual keys, budget enforcement); git operations never execute client repo hooks; provider credentials never enter the sandbox.
5. **Validate** — L1 gates (lint, typecheck, test, build, SAST, secret scan, diff budget, forbidden paths) against the repo's own declared commands; infra failures are separated from code failures and don't burn retries; L2 model review of plan vs diff.
6. **Deliver** — PR opened/updated idempotently with evidence (L1 line, preview line, changed-tests warning). Per-PR preview namespace: clone at the task branch, dev server with build fallback, TLS ingress URL on the PR.
7. **Review loop** — a formal GitHub "Request changes" review dispatches a fix cycle (merge-base first, then Coder on the same branch, revalidation, re-preview). A failed preview additionally triggers an autonomous loop: an agent reads the pod's error plus the repo's manifests and, if the cause is in the code, sends the item back to coding by itself (capped at 2 rounds, no-op and budget brakes).
8. **Terminal** — `review_ready` awaiting human decision; `failed`/`escalated` with audited reasons; merge is always human (`merged_by_human` webhook closes the item).

Everything above is exercised in production; step 7's autonomous loop first fired in production on 2026-08-12.

## 3. Deployment & usage (production numbers, 2026-07-24 → 2026-08-12)

| Metric | Value |
|---|---|
| Platform | Single VPS, k3s, 21 running pods, all services at rc.88 |
| Work items processed | 122 (84 in the last 7 days) |
| Outcomes | 61 failed · 60 escalated · 1 in review · **0 merged** |
| PRs opened | 26 |
| Model spend (all-time) | US$ 505.89 across 630 gateway calls |
| Previews attempted | 32 (14 degraded · 11 reaped by TTL · 1 up · 6 skipped by rule) |

Read these numbers in context: nearly all volume is the platform team's own end-to-end testing, and the failure counts include the platform's development iterations — each infrastructure failure class found this week (5 of them) was fixed and deployed the same day (rc.85→rc.88). The number that matters as a product signal is **0 merged**: the loop has never been closed by a human accepting the work, partly because review-facing UX (section 5) gives reviewers little to work with, and partly because generated test quality keeps killing items before review.

## 4. What was hardened this week (rc.85 → rc.88, all deployed)

- **rc.85** — failed previews explain themselves on the PR (log capture RBAC, begin+end truncation, static-build fallback when the dev server is broken).
- **rc.86** — the review fix cycle works on k8s at all (its git workspace is now provisioned on demand; before this it had never worked in production); the autonomous preview-repair loop (agent triage → back to coding under caps).
- **rc.87** — post-fix validation classifies by the whole PR diff, not the last commit (a metadata-only fix no longer flips an Angular repo to the Java preview recipe).
- **rc.88** — preview activity deadlines got headroom + heartbeats (no more discarded results and doubled waits); the triage agent reads the pod's actual error from the ledger on every path.
- **Gateway** — LiteLLM got its Postgres; real per-stage virtual keys now work (the `/key/generate` endpoint returned 500 before this).

## 5. Known limitations — engineering view

Honest list; none of these are hidden in the code (most carry dated comments):

1. **Generated test quality is the #1 killer.** Today's example: the Tester wrote a spec against a nonexistent component property; the item burned 8 coder retries and $12.77 before failing. Four structural mitigations are already live (typecheck verdicts never deferred, self-repair of zero-verdict specs, diff-proximity examples, reference spec in prompt); the Tester instruction itself is the remaining lever, and its "measure first" precondition has now been met.
2. **One silent death path.** Items that die by coder-retry exhaustion don't post "failed" to the originating Slack thread (every other terminal does). One-line fix, identified.
3. **In-flight items don't receive new features.** Temporal patch semantics (by design): a deploy only affects items started after it; upgrading an in-flight item requires restarting it. Needs an operational rule, not code.
4. **L1's build gate trusts the repo's exit code.** The client testbed manifests chain commands with `;`, which masks broken builds (fix in flight in the client repos).
5. **Preview TTL is a dated curative** (6h, "revert when the demo passes") and the namespace reaper deletes without a ledger event by documented design — the TTL-vs-review-window decision is still open.
6. **Cost accounting doesn't close.** Cache tokens are read nowhere, and 87% of all-time spend sits in anomalous ledger rows that predate current metering.
7. **Durability gaps accepted for POC**: sandbox checkpoints live on emptyDir (die with the node); Temporal/Postgres are single-node; there is no tested Postgres restore ("no tested restore, no go-live" per the runbook).
8. **Access control is POC-grade**: any member of the bound Slack channel can approve a plan; the operator console authenticates but doesn't authorize (any first-seen SSO subject can reach kill switches).

## 6. Product & usability view

The engine outruns its own user experience. The verified channel matrix:

| Capability | Slack | GitHub | Jira | Teams |
|---|---|---|---|---|
| Create work item | ✅ | ✅ label/mention | ✅ poller | ❌ |
| Live status updates (in place) | ✅ | ✅ | ✅ | ❌ |
| Terminal outcome posted | ⚠️ one silent path | ✅ | ✅ | ❌ |
| Preview link persists | ❌ | ✅ PR line | ❌ | ❌ |
| Approve a plan | ✅ button | ❌ | ❌ | ❌ |
| Request changes | ❌ | ✅ formal review only | ❌ | ❌ |
| Cancel / raise budget / see cost | ❌ | ❌ | ❌ | ❌ |

What this means for each person (all verified against the adapters' code):

**The requester (Slack)** sees one status message that edits itself. The PR link and preview URL never persist in the thread; replying "actually, make the button blue" is silently recorded as a clarification and dropped — mid-flight steering from Slack does not exist; unauthorized or unusable replies get no answer; there is no way to ask "status?", cancel, or see cost. When their high-risk item needs approval, nothing pings the approver — the code's own comment admits "THIS DOES NOT NOTIFY ANYBODY" — and GitHub/Jira requesters can't approve at all (their items wait 72h and escalate).

**The reviewer (GitHub)** gets a PR whose body describes the request, not what was built (no plan/acceptance-criteria mapping). Requesting changes works — but the fix lands as a silent push (no "done, see new commits" reply), the reviewer's inline comments never reach the Coder (only the review summary does), an expired preview leaves a dead link (the "expired" sentence exists as dead code), and the demo/trace/visual evidence bundle is implemented but never published to the PR.

**The operator** hand-onboards each repo in ~7 steps across 4 surfaces (SQL, GitHub UI, client manifest, k8s secret); sees a status word instead of a dossier when items escalate; cannot retry a dead workflow; gets no daily digest even though the cost/health rollup is already computed; and has no surface for `raise_budget` (the signal exists, nothing exposes it — nor an 80% warning before the hard stop).

## 7. Change backlog (prioritized)

Full itemized list with code evidence: `docs/REMAINING-WORK.md`. Condensed:

**Now — engine** (validated, cheap): the one-line silent-death fix · client manifests `;`→`&&` · Tester instruction improvement (precondition met) · preview TTL decision · written rule for in-flight items vs deploys.

**Now — product** (where users feel it): persist PR/preview links in the thread · thread replies become change requests (Slack/Jira) · answer refused replies · reply on the PR when a fix cycle lands · feed inline review comments to the Coder · PR body states what was built vs plan · approval pings + approval from GitHub · one-command repo onboarding · escalation dossier page · retry for dead items · daily digest · budget-raise surface + 80% warning · cancel from the channel · "expired" preview line · publish the evidence bundle.

**Next — engine**: stranded-sweep notices · cache-token accounting + spend anomaly cleanup · deny-by-default pass (plan approval principal gate + console authz) · sibling interface contract (C1b, measured 6× cost) + PR group links · repo-declared preview env (G7) · real diff for the L2 reviewer · console event filter + sibling `[repo]` prefix · GitHub webhook bot filter + in-channel refusals · autofix terminal narration on the PR · handle unmerged PR close.

**Later — with named reopening triggers** (build `reopening-triggers.sql` first so deferrals stay honest): per-cause retry budgets · L1 fail-fast · javac parser fix · per-test baselines · `client_spec_obsolete` counter · onboarding-as-stage + learning writers · preview workspace hand-off · k3d e2e in CI · data-test migration as dogfood · LiteLLM failover xfails · Teams activation.

**Go-live gates** (calendar items, start the long ones now): supply-chain (SBOM, image signing, CVE scan) · Postgres restore drill · real alerting backend + Temporal metrics · license decisions (Vault/BUSL, Redis/RSAL) + ADR-25 signatures · checkpoint PVC · third-party pentest.

## 8. Security posture (unchanged fundamentals)

Sandboxed execution (gVisor), egress through a filtering proxy, per-stage short-lived model credentials, client repo hooks never executed by DSE git operations, tokens never written to disk or argv in the preview/merge paths, secrets outside git by construction, full audit ledger for every decision. Accepted POC risks are listed in section 5 (items 7–8) and in the threat model docs; the red-team program's manual items are in the go-live gates.

---

*Method note: current-state claims come from the production database and ledger on 2026-08-12; capability and limitation claims come from a 10-agent adversarially-verified code sweep (533 file reads) — each has file:line evidence in `docs/REMAINING-WORK.md`. Items previously believed open but found already delivered are listed there in section 0 and are excluded here.*
