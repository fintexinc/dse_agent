# Running the DSE on Microsoft Teams — research

Date: 2026-08-14. Written against the DSE at `v0.1.0-rc.93`.

## Bottom line

Teams is **not a greenfield port**. A complete, tested `services/adapter-teams`
already exists in this repo (Phase 4, WS-A) and its image is already built by the
release workflow. It was deliberately left **inactive**, and it was written against
the Slack adapter *as it looked in Phase 4* — a plain text status message. Since
then the Slack surface grew the parts that make the DSE usable: approval buttons,
the plan modal, the repo label, the stage bar.

So the work splits cleanly in two:

- **Track 1 — turn it on (notifications).** Small, mostly wiring: 1 contract line,
  1 migration, 1 routing branch, 1 helm template, secrets. The DSE would *speak*
  in Teams: one status message per work item, edited in place as the item advances.
- **Track 2 — reach parity with Slack today.** Real work: Adaptive Cards instead
  of Block Kit, an invoke endpoint for the buttons, a dialog for "Details", and
  the verdict-idempotency logic the Slack adapter learned the hard way.

There is also one **hard external dependency** and one **finding that invalidates
part of the existing implementation** — both below.

---

## 1. What already exists in the repo

| Piece | State | Where |
|---|---|---|
| Inbound `POST /teams/messages` (normalize → 4 intake defenses → correlate) | implemented, tested | `services/adapter-teams/adapter_teams/app.py` |
| Outbound `POST /internal/status-comment` (one mutable message per item) | implemented, tested | same, + `backend.py` |
| Bot Framework Connector transport (AAD token, `POST`/`PUT` of activities) | implemented | `backend.RealTeamsClient` |
| Teams outgoing-webhook HMAC verification | implemented, fuzz corpus | `ingest_gateway/security.py:89` |
| Test suite | runs in CI today, group `adapters` | `scripts/test_matrix.py:33` |
| Container image | built on every release | `.github/workflows/release.yml:32` |
| Activation DDL (not a numbered migration) | written, **never applied** | `services/adapter-teams/activation.sql` |
| Helm template / deployment | **missing** | — |
| `Platform.teams` in the contract enum | **missing** (`slack`, `github`, `jira` only) | `packages/contracts/dse_contracts/conversation_event.py:15` |
| Orchestrator → Teams outbound routing | **missing** (no `teams` branch) | `local_activities._resolve_comment_target:907` |

Verified on the live VPS: `work_items.source` still checks
`('slack','github','jira')` — activation was never applied anywhere.

## 2. What Teams gives us, measured against what the DSE needs

The DSE's surface contract is narrow and specific. Teams satisfies all of it, but
through different primitives than Slack.

| DSE requirement | Slack today | Teams equivalent | Verdict |
|---|---|---|---|
| One status message per item, **edited in place** for hours | `chat.update` | `PUT /v3/conversations/{conversationId}/activities/{activityId}` | works; already coded |
| Keep siblings in **one thread** | `thread_ts` in `source_ref` | `conversation.id` carries channel + root message id; replying to it stays in the thread | works; same `source_ref` shape |
| **Proactive** posts (the DSE speaks minutes/hours after the trigger) | bot token | requires a **real bot registration** (Azure Bot + Entra app) | see §3 blocker |
| Approve / Reject / Details **buttons** | Block Kit `actions` | Adaptive Card `Action.Execute` → `adaptiveCard/action` invoke on the same messaging endpoint | needs new code |
| Plan **modal** | `views_open` | Teams dialog (task module) via `task/fetch` invoke | needs new code |
| Who clicked (allowlist/steering) | Slack user id | `from.aadObjectId` (already extracted by `events.py:71`) | ready |
| Tenant mapping | channel binding | `channelData.tenant.id` (AAD tenant guid, `events.aad_tenant_id`) | ready |
| Rich text (`code` spans, bullets, bold) | mrkdwn | **subset only**: Adaptive Card `TextBlock` has no tables and no code blocks | rendering must be rewritten, not ported |

Rate limits are comfortable for our traffic: ~7 sends/second per conversation,
60 per 30s, 1800/hour per conversation, and 50 requests/second per app per tenant.
The DSE edits one message per status transition — orders of magnitude below. A
`429` backoff is still required (the Slack adapter has `ratelimit.py`; Teams needs
the equivalent).

## 3. The two findings that change the plan

### 3.1 The implemented inbound auth is a dead end for our model

The adapter implements the **outgoing-webhook HMAC** scheme, which is the direct
analogue of Slack's signing secret — and that is exactly why it was chosen in
Phase 4. But a Teams *outgoing webhook* is a request/response object with no app
identity: it cannot post later, and it cannot edit a message it never owned. The
DSE's entire UX is asynchronous (post at intake, edit for the next 40 minutes),
so the HMAC path can never carry it.

**Consequence:** activation requires the **Bot Framework JWT path** — validating
the `Authorization: Bearer` token against Microsoft's OpenID metadata on inbound,
which the adapter's README already lists as a deferred "channel activation step".
That is a real, security-critical piece of work (token signature, issuer,
audience = our app id, `serviceUrl` claim), not a config toggle.

### 3.2 The SDK the code does *not* use is the one that got archived

The Bot Framework SDK is archived — support ended 2025-12-31 — and Microsoft's
successor is the **Microsoft 365 Agents SDK** (Python packages exist:
`microsoft-agents-hosting-core`, `-activity`, `-hosting-aiohttp`,
`-authentication-msal`, `-hosting-teams`).

Our adapter never took that dependency: `RealTeamsClient` speaks the Connector
**REST API** directly (`login.microsoftonline.com` client-credentials → `POST`/`PUT`
`/v3/conversations/...`). That REST surface is the stable, versioned wire protocol
and is what the SDKs themselves call. **Recommendation: stay on raw REST.** It
keeps the adapter dependency-light (as the Slack one is), avoids adopting an SDK
mid-rewrite, and the only thing we lose is the SDK's JWT validation helper — a few
dozen lines with `PyJWT` + cached JWKS.

## 4. External dependencies (operator side, not code)

These are not ours to merge, and they gate any real test — same class as the Auth0
wildcard callback we hit on previews:

1. **Azure subscription** with an **Azure Bot** resource (Teams is a *standard*
   channel: free, unlimited messages; we keep hosting on our own VPS).
2. **Entra ID app registration** (client id + secret) → stored in Vault as
   `dse/teams/bot` (the path the adapter already expects).
3. **Public HTTPS messaging endpoint** for the bot. We already terminate TLS for
   previews on the VPS, so this is one more ingress host.
4. **Teams app package** (manifest + icons) uploaded to the tenant — and the tenant
   admin must permit it. Custom-app upload/sideloading is controlled by org-wide
   settings and app setup policies; in many corporate tenants (a bank, notably) it
   is **disabled by default** and requires an admin publishing to the org catalog.

Item 4 is the one that can add weeks of calendar time at a customer, and it is
worth starting in parallel with any code work.

## 5. Recommended plan

**Track 1 — activation (notifications only).** Delivers: the DSE posts and keeps
editing a status message in a Teams channel; humans still approve in Slack.

1. `Platform.teams` — one additive line in the contract (+ changelog, MINOR).
2. Numbered migration `0043` from `activation.sql` (widens two CHECKs; additive,
   idempotent). Note `activation.sql` is documentation, not a migration — it must
   be promoted into `scripts/migrate.py`'s numbered set.
3. `_resolve_comment_target`: a `teams` branch returning `DSE_ADAPTER_TEAMS_URL`
   plus the correlation fields (`service_url`, `conversation_id`) — without this
   the orchestrator silently never speaks to Teams.
4. Helm template + service on port 8808, secrets wired from Vault, image already
   published.
5. Inbound JWT validation (§3.1) — required the moment a real bot is registered.

**Track 2 — parity with the Slack surface at rc.93.**

6. A Teams renderer mirroring `status_blocks`: repo as a small header line, body,
   stage bar (`✅/⏳/▫️` render fine), and an actions row — as Adaptive Card JSON.
   Formatting is a rewrite, not a port: no code spans, no tables.
7. Invoke handling on `/teams/messages`: `adaptiveCard/action` (Approve/Reject) and
   `task/fetch` (Details dialog with the plan and sibling plans).
8. The verdict rules the Slack adapter already paid for: one decision per parking,
   re-arm on re-park, Details never falling through to a verdict, and the
   authorization check on the clicker's `aadObjectId`.

**Not recommended:** Power Automate / incoming-webhook style integrations. They can
post, but cannot edit a message they posted nor carry interactive verdicts, so the
DSE's oversight model (one live message, buttons that decide) would degrade to a
notification firehose.

## 6. Effort, honestly

Track 1 is small and boring — the kind of change that is mostly review surface:
one contract line, one migration, one routing branch, one chart template, plus the
JWT validator. Track 2 is the real cost, comparable to what the Slack interactive
surface took across rc.89–rc.93 (cards, invokes, dialog, verdict idempotency, and
their tests). Both are dwarfed, in calendar time, by the tenant-side app approval
if the target is a customer tenant rather than our own.

## Sources

- [Bot Framework SDK overview (archived/maintenance status)](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-overview?view=azure-bot-service-4.0)
- [Bot Framework SDK → Microsoft 365 Agents SDK migration, Python](https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/bf-migration-python)
- [Pick the right SDK for your Teams agent](https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/teams/sdk-comparison)
- [Conversations with an agent (update activity, thread conversation id)](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability)
- [Send proactive messages](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages)
- [Universal Actions for Adaptive Cards (`Action.Execute`, `adaptiveCard/action`)](https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/universal-actions-for-adaptive-cards/work-with-universal-actions-for-adaptive-cards)
- [Format text in cards (markdown subset)](https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-format)
- [Rate limiting for agents](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/rate-limit)
- [Manage custom app policies and settings (sideloading/admin)](https://learn.microsoft.com/en-us/microsoftteams/teams-custom-app-policies-and-settings)
- [Azure AI Bot Service pricing (standard channels free)](https://azure.microsoft.com/en-us/pricing/details/bot-services/)
