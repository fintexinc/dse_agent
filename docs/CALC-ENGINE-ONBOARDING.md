# Onboarding: `fintexinc/calculation-engine-service`

Date: 2026-08-17. Channel `C0BR4Q7NW20`, tenant `fintex-poc`, base branch
`dse-agent`.

## Done

- **Branch `dse-agent` created** from `main` (`743fc1f`). Every plan, PR and
  merge for this repo now targets it, because the binding carries the base
  branch — not a global default.
- **Channel bound**: `repo_bindings(fintex-poc, slack, channel, C0BR4Q7NW20,
  fintexinc/calculation-engine-service, dse-agent, deploys_preview=true)`.
  Single binding on the channel, so `resolve_repo` resolves it
  **deterministically** — no model picks the repo.
- **Repo profile registered** (role/language/description), which is what the
  router reads when a channel can reach more than one repo.

Remaining, and only you can do it: **invite the DSE app to the channel**
(`/invite @dse` in `#calculation-engine`). A bot only receives mentions in
channels it belongs to. Tenant resolution needs nothing — Slack falls back to
`DSE_TENANT_ID=fintex-poc`, the same path the other two channels already use.

## The repo, as measured

Java 21 / Spring Boot 3.4.6, Maven **multi-module** (domain, api, application,
observability-adapter, web-client-adapter, cache-adapter, rest-adapter,
bootstrap), hexagonal. Port 8181. CI lives in **Azure Pipelines**
(`spotless:check` → `test` → `install -DskipTests`, JDK 21), not GitHub.

What it does **not** have, and each absence is load-bearing for us:

| Missing | Consequence for the DSE |
|---|---|
| `.dse/validation.json` | L1 has no commands to run — and the preview recipe reads `.commands.build[2]` from it (`argocd.py:377`). Without it, **nothing validates and nothing previews**. |
| Maven wrapper (`mvnw`) | The preview image is JDK-only; with no wrapper there is no Maven to run the build. |
| GitHub Actions | Every PR gets "This repository has no CI — you are the only gate". The fix-cycle has no CI signal to react to. |
| `Dockerfile` | Not required (the preview builds from source), but it is why the preview must compile in-pod. |

## Preview: four blockers, all ours except one

The `deployable` recipe clones the branch, runs the repo's own build command,
then `java -jar`. Against this repo it breaks four times:

1. **Wrong JDK.** `DSE_PREVIEW_DEPLOYABLE_IMAGE` defaults to
   `eclipse-temurin:17-jdk` (`services/validation/dse_validation/config.py:839`)
   and the repo is Java 21 — compilation fails on release 21.
2. **No Maven in the image.** Fine when the repo ships `./mvnw`; this one does
   not. Either the image carries Maven (`maven:3.9-eclipse-temurin-21`) or the
   repo gains the wrapper (goal 1 below).
3. **Jar path assumes a single module.** The recipe does
   `ls target/*.jar` (`argocd.py:383`); in a multi-module build the artifact is
   at `bootstrap/target/*.jar`. It will always miss.
4. **The app cannot boot without SMS.** `SM_REST_BASE_URL` has no default
   (`application.yml`), so Spring fails fast at startup, and readiness is gated
   on SMS reachability. The preview probe is `tcpSocket`, so the gate itself
   does not block us — but a process that refuses to start does.

Ours to fix (1-3) is small: make the image and the jar glob repo-aware instead
of assuming one shape. The honest structural fix is the one already in the
backlog as G7 — **the repo declares its preview environment** in
`.dse/validation.json`, the same way it already declares its build, so the
platform stops hardcoding another client's variable names (the `BMO_DB_*` block
at `argocd.py:390` is exactly that debt).

## Three goals to feed the DSE in this channel

Ordered. Each is real value for the repo *and* removes a blocker for us — that
overlap is deliberate: the first tasks should make the engine able to work here.

### Goal 1 — "Add the Maven wrapper and a `.dse/validation.json` mirroring the Azure pipeline"

The repo's quality bar today lives only in Azure Pipelines. This puts the same
three commands (`spotless:check`, `test`, `install -DskipTests`) where any
tool — the DSE included — can read and run them, and pins the Maven version via
the wrapper so a build is reproducible off a bare JDK.

Unblocks: L1 gates and the preview build. Without it, nothing else runs.

### Goal 2 — "Run the same checks on GitHub Actions for pull requests"

A workflow on `pull_request` running spotless, tests and package on JDK 21,
publishing the surefire report. Today a PR here has no automated verdict on
GitHub at all.

Unblocks: the DSE's CI-red fix cycle, which currently has no signal to consume,
and removes the "you are the only gate" warning from every PR.

### Goal 3 — "A preview profile that boots without the Security Master"

A Spring profile (`preview`) where `SM_REST_BASE_URL` has a safe default and the
SMS-backed paths answer from a stub or fixture, keeping the readiness group
honest. The service becomes demonstrable in isolation — Swagger UI and the
metric endpoints — without a live SMS.

Unblocks: preview that actually shows something instead of a pod that will not
start. It is also the only one of the three with real product value beyond
tooling: it makes the engine runnable on any laptop or ephemeral environment.
