# Teams integration — adversarial audit of what shipped in rc.94

Date: 2026-08-17. Method: 6 parallel audit fronts (inbound auth, outbound wire,
cluster/deploy, data plane, app package, test coverage), each finding then
handed to a skeptic prompted to **refute** it, plus a completeness critic asking
what nobody looked at. 15 agents, ~1.5M tokens. 40 raised, 8 survived
refutation, 0 knocked down, 5 completeness gaps found.

Out of scope by design: Track 2 (adaptive cards, approve/reject buttons, plan
dialog, connector rate limit) — planned, not built, not audited.

---

## What is verifiably correct

Measured against the live cluster and the live database, not inferred from
templates:

- **Inbound auth holds.** Fail-closed with no credential (the observed 401
  `missing_app_id`); an empty HMAC secret does **not** open the door; there is
  no third scheme — anything that is not `Bearer` falls to the HMAC verifier and
  is denied; `algorithms=["RS256"]` is a closed list so `alg:none` and
  HS256-with-public-key die; missing claims are not treated as ok
  (`require: [exp, iss, aud]`); and nothing is read from or written to the
  database before the signature verdict.
- **The outbound wire matches field by field.** What the orchestrator sends is
  exactly what the adapter's model accepts; `service_url` is required to address
  and is used end to end; the bot's tenant id really reaches the client and
  changes the token issuer; `MutableCommentWriter` works for `surface="teams"`.
- **The deployment matches the chart, object by object.** Pod healthy on rc.94,
  port 8808 consistent in all five places it appears, NetworkPolicy verified on
  the live object, and egress proved **from inside the pod** through the proxy:
  `login.botframework.com` metadata returns 200. TLS is a real Let's Encrypt
  certificate valid to Nov 15.
- **Migration 0043 is applied in production** and the sweep found no platform
  enumeration left outside it. Redelivery of the same Activity does not create a
  duplicate item (deterministic `event_id`).
- **The app package validates clean** against the official 1.30 schema (zero
  errors), and the icons meet the hard requirements at pixel level — the outline
  is genuinely 2-colour (transparent + white), the colour icon fully opaque.
- **The tests bite.** Three central changes were reverted mentally and each
  breaks a named test; the JWT corpus is 16 real-RSA cases asserting the reason
  by name, not just "denied".

## Findings that survived refutation

### 1. The channel binding can never match — `resolve_repo` gets the thread id (high)

`conversation.id` carries **channel + root message id** (`19:…@thread.tacv2;messageid=…`),
and it is passed whole as the `channel` signal (`adapter_teams/app.py:129`).
`repo_bindings` matches by exact equality, so a row the operator registers for
the channel never matches a real payload: every Teams item is born with
`repo NULL` and falls to the LLM router over the tenant's whole catalogue —
the exact failure `repo_resolver.py:20-26` was written to narrate.

The feature I shipped this morning is a **no-op against real traffic**, and the
test that certifies it passes only because every fixture in
`adapter-teams/tests/` uses an id **without** the `;messageid=` suffix — while
the orchestrator's own fixture (`test_teams_outbound_routing.py:23`) uses the
real form. Contradiction inside our own repo.

Fix: keep the full id as thread key / `source_ref` (it is the reply address),
but derive the `channel` signal from `channelData.channel.id`, falling back to
`conv_id.split(";", 1)[0]`. Red first with a realistic id.

### 2. Unknown `kid` triggers a synchronous JWKS refetch per request (high)

Once the Secret is populated, any POST with a random `kid` makes the verifier
refetch the JWKS with **blocking urllib inside an async endpoint** — freezing
the event loop of a single-worker pod. A loop of such requests is a denial of
service on a public endpoint, and it also hammers Microsoft.

Fix: throttle the refetch (remember the last attempt; at most one per N
seconds) and move the verification off the event loop.

### 3. The clarification loop is unreachable in Teams (high)

Every follow-up message in the conversation is classified as a new
`task_request`, so the human's answer to "which base branch?" dies as
`deduped_already_started`. The core "the DSE asks, the human answers" loop —
which works in Slack — does not exist here.

### 4. The manifest promises what the adapter cannot serve (high)

The package declares `personal` scope and the commands `status` / `help`. After
install, a DM gets HTTP 200 and silence, and typing `help` opens a **real work
item**. Either implement them or stop advertising them.

### 5-8. Mediums

- **No time budget on the outbound path**, and the token is refetched per
  status: the first message of an item can exceed the caller's 8s and break the
  one-message-per-item invariant.
- **`service_url` is part of the correlation key**: if the regional address
  varies between messages, the human's reply stops correlating.
- **The documented Vault route delivers nothing** — and, if it did, the key
  names in the doc differ from the code and would raise a 500. (The doc is mine;
  the cluster has no `VAULT_TOKEN`, so only the k8s Secret works.)
- **`TEAMS_APP_TENANT_ID` is missing from every provisioning path** in the repo
  (`dse.sh`, the deploy README), so an operator following them produces a
  single-tenant bot asking the multi-tenant issuer for a token → 401.

### Completeness gaps

- **Fan-out**: with siblings sharing one `conversation_id`, a human reply always
  lands on the newest sibling — the primary can never be answered. Slack solved
  this with `bot_ts`; Teams has no equivalent yet.
- **No recovery sweep**: a delivery lost during a rollout (502 from Traefik) is
  lost forever. Both sibling adapters were considered incomplete without one.
- **Operational**: populating the Secret does **not** unblock the adapter —
  env vars are read at container start and the verifier is a process singleton.
  The pod must be restarted.

## Recommended order

1. Findings 1 and 2 — both are defects in code that shipped today, and #1 makes
   a delivered feature inert.
2. Finding 4 (trim the manifest) before the app is approved: it changes the
   package the admin uploads.
3. Findings 3 and the fan-out gap — they decide whether the Teams loop is
   actually usable end to end.
4. The mediums, then Track 2.
