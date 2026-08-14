# Creating the DSE app in Microsoft Teams — operator guide

Companion to [TEAMS-ADAPTER-RESEARCH.md](TEAMS-ADAPTER-RESEARCH.md). This is the
**operator track**: everything here happens in Microsoft's consoles with your
credentials, not in this repo. It can run in parallel with the code work, and the
tenant approval (step 0) is the one that can take weeks, so start there.

> The app package alone does nothing. A Teams app is a *pointer*: the manifest
> says "there is a bot with client id X, talk to it". The bot registration and
> the public endpoint are what make it real.

## Step 0 — the blocker you already hit

If "Upload an app" only offers **"Submit an app to your org"** and there is no
**"Upload a custom app"**, custom-app upload (sideloading) is **disabled by policy**
in the tenant. Two ways forward:

- **You have Teams admin**: [Teams admin center](https://admin.teams.microsoft.com)
  → *Teams apps* → *Setup policies* → **Global (Org-wide default)** → turn
  **Upload custom apps** ON. Also check *Teams apps* → *Manage apps* → **Org-wide
  app settings** → allow custom apps. Policy changes can take a few hours to
  propagate to clients.
- **You don't**: use the "Submit an app to your org" flow you already found — it
  puts the package in the admin's approval queue (*Manage apps* → *Pending
  approval*). Same end state, someone else's click.

For the POC, sideloading in a tenant you control is far faster than a customer
tenant's approval queue.

## Step 1 — register the bot identity (Entra ID + Azure Bot)

Teams never calls your service directly: it calls the Bot Framework service,
which forwards to your endpoint using a registration. You need both halves.

1. [Azure portal](https://portal.azure.com) → **☰ menu (top left)** → **+ Create a
   resource** → search `bot` → the **Azure Bot** card → **Create**. (The blue
   global search bar at the top searches existing resources, services and docs —
   it does *not* open the create blade; that is why "create resource" typed there
   returns nothing useful.)
   - *Type of App*: **Single-tenant** — see the note below before choosing.
   - *Creation type*: create a new Microsoft App ID (this creates the Entra ID app
     registration for you).
   - Pricing: the **Teams channel is a standard channel — free, unlimited
     messages**. You are only paying for our own hosting, which is already the VPS.
2. In the new resource → **Configuration** → set **Messaging endpoint** to
   `https://<host>/teams/messages` (see step 2), then *Apply*.
3. → **Channels** → add **Microsoft Teams** → *Save*.
4. → **Configuration** → *Manage* (next to Microsoft App ID) → **Certificates &
   secrets** → **New client secret**. Copy the value now; it is shown once.

You end with two values: **client id** (the `botId` for the manifest) and
**client secret**.

> **App type: single-tenant, and it forces a code change.** Microsoft **stopped
> allowing new multi-tenant bots after 2025-07-31** (existing ones keep working),
> so the remaining options are *single-tenant* and *user-assigned managed
> identity*. Our adapter, however, fetches its outbound token from the shared
> multi-tenant endpoint
> `login.microsoftonline.com/botframework.com/oauth2/v2.0/token`
> (`adapter_teams/backend.py: RealTeamsClient._TOKEN_URL`) — written in Phase 4,
> when multi-tenant was still the default. Single-tenant requires pointing that
> URL at `login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token` and carrying
> the tenant id in config: a small change, but a **mandatory** one, not optional.
> Add it to Track 1 and store `tenant_id` alongside the credentials in
> `dse/teams/bot`. The app type cannot be changed after the resource is created.

## Step 2 — expose the endpoint and store the secret

- **Public HTTPS host** for the adapter (port 8808), e.g.
  `teams.notas.api.br` → ingress + TLS on the VPS, the same pattern the console
  hosts already use. Microsoft requires a publicly reachable HTTPS endpoint with a
  valid certificate; there is no tunnel exception for production.
- **Vault**: store the credentials at `dse/teams/bot` (`client_id`,
  `client_secret`, and `tenant_id` for the single-tenant token endpoint above) —
  the path the adapter already expects. Never put them in values files or the
  manifest.

## Step 3 — build the app package

```bash
python scripts/make_teams_app_package.py --bot-id <client-id-from-step-1>
```

Writes `build/dse-teams-app.zip` (manifest.json + color.png 192x192 + outline.png
32x32, all at the zip root — Teams rejects packages with a nested folder). It
prints a generated **app id**: keep it. Re-running with `--app-id <that value>`
updates the installed app; a new id installs a second copy instead.

Useful flags: `--host` (goes into `validDomains`), `--name`, `--out`.

## Step 4 — upload and install

- Sideload enabled: Teams → **Apps** → *Manage your apps* → **Upload an app** →
  *Upload a custom app* → pick the zip → **Add to a team** and choose the channel
  the DSE should live in.
- Otherwise: same dialog → **Submit an app to your org** → wait for the admin.

Sanity check that the plumbing is alive before anything else: mention the bot in
the channel. Teams should deliver a POST to `https://<host>/teams/messages`. Until
activation (below) the adapter answers **501 `teams_not_activated`** *after*
verifying the request — so a 501 in our logs is the success signal at this stage:
it proves registration, endpoint, TLS and routing all work.

## Step 5 — what still has to ship on our side

Creating the app does **not** make the DSE answer in Teams. From
[the research](TEAMS-ADAPTER-RESEARCH.md), Track 1 is still required:

1. `Platform.teams` in the contracts enum;
2. numbered migration widening the `work_items.source` / `identity_links.platform`
   checks (`services/adapter-teams/activation.sql` is the draft, not a migration);
3. a `teams` branch in `local_activities._resolve_comment_target` — without it the
   orchestrator never sends status updates to Teams;
4. Helm template + service for the adapter on 8808, secrets wired from Vault;
5. **inbound JWT validation** — the implemented HMAC scheme belongs to Teams
   *outgoing webhooks*, which cannot post or edit later, so it cannot carry the
   DSE's asynchronous status message. A registered bot authenticates with a
   `Bearer` token that we must validate (issuer, audience = our client id, signing
   key from Microsoft's OpenID metadata).

And Track 2 (Adaptive Cards, buttons, plan dialog) for parity with what Slack
does today.

## Order of operations, condensed

```
step 0 (tenant policy)  ─┐
step 1 (Azure Bot)       ├─ operator, in parallel with the code
step 2 (host + Vault)    │
step 3-4 (package)      ─┘
                          ↓
                  Track 1 activation (this repo)  →  DSE speaks in Teams
                  Track 2 cards/buttons           →  approvals in Teams
```
