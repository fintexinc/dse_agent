# Production change log — dse-vm-01 (172.172.235.228)

Append-only. One entry per change made directly on the VPS that is NOT reproducible
by `helm upgrade` alone — i.e. anything touching the hand-made `dse-poc-secrets`
secret, which no chart template owns (`infra/helm/dse/` has no `DSE_*_MODEL`; the
orchestrator picks it up through `extraEnvSecret`). A redeploy will NOT restore or
re-apply any of this. Newest last.

---

## 2026-09-11 — Coder default: Haiku 4.5 -> Sonnet 5

**Why:** PR #66 ("Default to Sonnet 5, and register four candidates for the next
A/B", merged as `7e5ce3f`) moves the `DSE_CODER_MODEL` default to
`anthropic/claude`. The default lives in code and compose only; production reads
the secret, so the merge alone was a no-op in prod.

**Decision:** apply half (1) — the secret — now. Skip half (2), the gateway image
rebuild, for now: it is not required for the switch (see evidence below) and only
corrects ledger pricing.

**Changed:** `dse-poc-secrets` key `DSE_CODER_MODEL`, namespace `dse`.

| | value |
|---|---|
| before | `anthropic/claude-haiku` |
| after | `anthropic/claude` (= `claude-sonnet-5`) |

Planner, Tester and L2 inherit this key (`activities.py:1579,2673`,
`l2/session.py:132`), so they move to Sonnet 5 too. Coder spend goes up roughly
10x per token; the spend-cap brake is unchanged.

**State before the change, for rollback:**

- secret `resourceVersion`: `1625221`
- orchestrator generation: `152`, image digest `sha256:ec813a3d5912…`
- `DSE_ROUTER_MODEL`: **unset** — left unset deliberately, see below
- gateway image: `model-gateway:v0.1.0-rc.138`
- in flight at the time: sandbox `dse-sbx-wi-902f5e35…`, Running since 2026-09-09
  12:50Z (46h), interrupted by the restart and left to Temporal's retry

**Rollback (two commands, no helm, no rebuild):**

```
kubectl -n dse patch secret dse-poc-secrets --type=merge \
  -p '{"stringData":{"DSE_CODER_MODEL":"anthropic/claude-haiku"}}'
kubectl -n dse rollout restart deploy/dse-dse-orchestrator
```

The old alias is not going anywhere: `anthropic/claude-haiku` ->
`claude-haiku-4-5-20251001` stays registered in the gateway config
(`litellm_config.yaml:106`), so the rollback needs no image change either.

### Decisions taken, and the evidence for them

1. **`DSE_ROUTER_MODEL` was NOT added**, though PR #66 pins it in compose and
   `deploy/vps/README.md:107` lists it. It is dead weight in prod:
   `local_activities.py:1359` reads
   `os.environ.get("DSE_ROUTER_MODEL", "anthropic/claude-haiku")` — the fallback is
   already Haiku and does not derive from `DSE_CODER_MODEL`. Routing stays cheap
   with the key unset. Add it only if that hardcoded default ever changes.

2. **No gateway rebuild was needed for the switch.** Read from the *running*
   rc.138 image, `/app/config.yaml:94`: `anthropic/claude` -> `claude-sonnet-5` is
   already baked in. The alias resolves today.
   Still open (half 2): that image bundles LiteLLM 1.93.0's July price table, and
   Anthropic moved Sonnet 5 to $2/$10 on 2026-08-11, after the pin. **Cost rows in
   the ledger read high until a release build ships the new
   `litellm_config.yaml`** (`Dockerfile.proxy` COPYs it in — "K8s (no volume
   mount)"; the compose bind-mount is dev-only). That needs a `v*` tag, then
   `_ghcrTag` bumped in `deploy/vps/values-vps-poc.yaml`, then the upgrade.

3. **Only the orchestrator was restarted.** Ten deployments mount
   `dse-poc-secrets`, but the sandbox pods carry no `*_MODEL` env at all — the
   model is chosen orchestrator-side and passed in the minted virtual key.

4. `docs/_build_metrics_pdf.py:213` claims the Coder is already on
   `anthropic/claude`. That was **false for production** as of this change.
