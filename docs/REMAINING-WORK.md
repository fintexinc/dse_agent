# DSE — Remaining Work (validated) + Product Gaps

2026-08-12 · Every line below was verified against the code by a 10-agent sweep (533 file reads), not against docs of intent. Basis: rc.88 deployed. Successor to the lost `BACKLOG-DSE.md`; supersedes the stale tiers of `BACKLOG-REVIEW.md`.

---

## 0. Corrections first — things the old backlog still lists but that are ALREADY DONE

Do not plan these again:

- **Approve button / bot_ts (M1=A1+A4)** — done; fan-out already strips `bot_ts` (`local_activities.py:1343`).
- **L1 infra-vs-code (E2/H5)** — done 08-11; gate `ERROR` escalates as `l1_infra_error` before burning coder retries (`workflows.py:311-336, 2661-2675`).
- **Repo notes read inside the Pod (H3)** — done 08-06 for Coder and Tester turns.
- **Example selection by diff proximity (D1/H4)** — done 08-10 (`_example_candidates`), both paths.
- **Real app registrations** — GitHub App + Slack app run in production today; residuals are only Jira webhook secret (polling is deliberate) and Teams.
- **Private-repo token injection at the egress proxy (`/git-relay`)** — implemented, contrary to the fast-follow doc.
- **`run_demo_evidence` deadline** — not a bug; 720s of headroom exists.
- **E12** — obsolete (its mechanism was removed 08-10), not regressed.
- **Tester metering** — tester turns ARE metered now; the cost gap that remains is cache tokens (below).
- Shipped this week (rc.85–88): previews explain themselves on the PR; fix cycle works on k8s; autonomous triage loop; whole-PR-diff classification; preview deadline headroom; triage reads pod words; real virtual keys (LiteLLM DB).

---

## 1. NOW — engine (validated, still open)

1. **Silent death on ONE path** — only the `coder_retry_cap_exhausted` finisher skips the "failed" post to the origin thread; every other terminal posts. **One-line fix** (`workflows.py:2691-2712`).
2. **Testbed manifests `;` → `&&`** — L1 build gate is returncode-only by design, so the client manifests' `;` chains mask broken builds. Fix lives in the two client repos (parallel session — confirm it landed).
3. **Tester prompt improvement** — the deliberate hold ("measure 2 rounds first") is now satisfied; 4 structural mitigations already shipped, the prompt text itself (`_TEST_AUTHOR_PROMPT`) is the open remainder. Today's kill: spec against a nonexistent API, 8 retries, $12.77.
4. **Preview TTL (G4′)** — 6h curative still in values with a dated revert note; decide the review-window TTL once. Note: the CronJob reaper's silent delete is *documented design* (DB catches up lazily on next trigger) — decide keep-or-audit explicitly.
5. **`workflow.patched()` on in-flight items** — confirmed as expected Temporal semantics (52 patch ids in the file). Not a code fix: write the operational rule — *new features only reach items started after the deploy; restart an item to upgrade it.*

## 2. NOW — product (new, verified against code)

6. **PR + preview links never persist in the Slack thread** — status bodies carry no URL at all, and on Slack/Jira the preview link is written into the same single message the next transition overwrites. (S)
7. **A Slack/Jira thread reply cannot request changes** — every non-mention reply is hardcoded `clarification_answer`; mid-flight "actually, make it blue" is recorded and silently dropped. Request-changes steering exists only as a formal GitHub review. (M)
8. **Refused replies get total silence** — unauthorized steering and no-verdict comments are audited but nothing answers the human who wrote them. (S)
9. **Fix cycle ends with a silent push** — the DSE never replies on the PR "done — see new commits" after a reviewer's request-changes round. (S)
10. **Inline review comments never reach the Coder** — only the review's summary body becomes the fix instruction; the file-anchored comments (where reviewers write the real asks) are dropped. (M)
11. **PR body never says what was built** — only request summary + gate evidence; no plan/acceptance-criteria mapping for the reviewer to judge against. (S)
12. **Approvals are blind and unannounced** — plan-gate "notification" is an in-place edit (the code's own comment admits "THIS DOES NOT NOTIFY ANYBODY"); GitHub/Jira requesters cannot approve at all (their high-risk items wait 72h and escalate); the gate body shows a risk word, no plan, no cost estimate. (M)
13. **Onboarding a repo is ~7 hand steps across 4 surfaces** (SQL inserts — one seeded inside a migration —, GitHub UI, client-repo manifest, k8s secret). Product shape: one command/flow. (M)
14. **Escalation gives a status word, not a dossier** — console work-item page shows state + raw audit rows; no terminal_detail, cost, PR link, or triage story in one place. (M)
15. **A dead workflow cannot be retried** — every operator control is a signal to a live workflow; Failed/terminated items have no "retry as fresh run on the same work item". (M)
16. **No daily fleet digest** — `cost_rollup` by day×repo×model already computed; nobody delivers it to Slack. (S)
17. **`raise_budget` has no surface** — signal exists, absent from console VALID_SIGNALS and every channel; plus no 80% budget warning before the hard deny. (S)
18. **Requester cannot cancel from the channel** — cancel signal exists, no route maps a thread/issue action to it. (M)
19. **Expired preview leaves a dead link** — the "expired (TTL)" sentence exists as dead code; no caller writes it when the reaper deletes the namespace. (S)
20. **Evidence bundle never reaches the PR** — demo video/trace/visual-diff consolidated comment is implemented and registered, but nothing calls the publisher. (S)

## 3. NEXT — engine (confirmed open)

- **B7 remainder** — stranded-sweep converges items silently; rich error dossier still discarded at the channel boundary.
- **E11 remainder** — cache tokens never read anywhere (write paths read only prompt/completion tokens); the 87%-of-spend anomaly rows still unexplained.
- **Access control pair** — approval principal gate (any Slack channel member can approve) + console authz (any first-seen SSO subject reaches kill switches). One deny-by-default pass covers both.
- **C1(b) + C2** — sibling waits for the primary's interface (measured 6× cost); PR group cross-links.
- **G7** — repo declares its preview env in `.dse/validation.json` (BMO_* bridge still hard-coded).
- **L2 real diff on k8s** — reviewer still judges a placeholder `diff_summary`.
- **B5/B6** — console `last_event` unfiltered; sibling threads lack a `[repo]` prefix.
- **A2/A3** — GitHub webhook bot-comment filter at ingest; in-channel refusal for non-task GitHub asks.
- **Autofix terminal narration** — "declined (cap/no-op/infra)" never reaches the PR line. (from product sweep)
- **Unmerged PR close is ignored** — item waits forever; closing should end it with a confirmation. (from product sweep)

## 4. LATER — deferred with named triggers

- **`reopening-triggers.sql` (M6) first** — ~30 lines; makes every deferral below real instead of decorative.
- E6 per-cause retry budgets · E7 L1 fail-fast · E8 javac parser ride-along · E9 per-test baseline · C5 `client_spec_obsolete` counter · D2/D3 onboarding-as-stage + learning writers · G5 workspace hand-off to preview · G6 k3d e2e in CI (local G6-lite joins the preview DoD now) · F2 data-test migration as a dogfood work item · LiteLLM router xfail duo (serves a downed primary; red since 07-24) · Teams activation (today a wired stub with zero capability — don't sell parity).

## 5. GO-LIVE gates (operational / admin / security — all confirmed)

- Supply chain: SBOM + image signing + CVE scan in CI (red-team's top manual item).
- Postgres restore drill ("no tested restore, no go-live").
- Real alerting backend + Temporal Prometheus metrics (explicit TODO).
- License decisions: Vault (BUSL) / Redis (RSAL); ADR-25 needs its two signatures.
- Checkpoint on PVC (emptyDir dies with the node).
- Third-party pentest; credential-replay and sandbox-escape manual reviews.
- VPS knobs: validation image in release, otel on/off decision, adapter-teams in chart, `skillsSync` flip.

---

## Channel capability matrix (verified)

| Capability | Slack | GitHub | Jira | Teams |
|---|---|---|---|---|
| Create work item | ✅ | ✅ (label/mention) | ✅ (poller) | ❌ 501 |
| In-place status updates | ✅ | ✅ | ✅ | ❌ |
| Terminal outcome posted | ⚠️ (one silent path, item 1) | ✅ | ✅ | ❌ |
| Preview link persists | ❌ (item 6) | ✅ PR line | ❌ | ❌ |
| Approve plan | ✅ button | ❌ (item 12) | ❌ | ❌ |
| Request changes | ❌ (item 7) | ✅ formal review only | ❌ | ❌ |
| Cancel | ❌ (item 18) | ❌ | ❌ | ❌ |
| Raise budget | ❌ (item 17) | ❌ | ❌ | ❌ |
| See cost | ❌ | ❌ | ❌ | ❌ |

*Suggested reading order for prioritization: section 2 is where the product feels broken to users today; section 1 items 1–3 are the cheapest engine wins; section 5 runs on the calendar, start the long-lead ones now.*
