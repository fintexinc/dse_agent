#!/usr/bin/env bash
# =============================================================================
#  Fintex DSE — start/stop/status for the WHOLE local system
#
#  Usage:
#    ./dse.sh start            brings everything up (infra, migrations,
#                              services, preview cluster, console) — idempotent
#    ./dse.sh start --no-preview   skips the k3d cluster (previews disabled)
#    ./dse.sh start --no-console   skips the console (API+UI)
#    ./dse.sh stop             stops the console + services (state preserved)
#    ./dse.sh stop --all       same + tears down containers (compose down) and k3d
#    ./dse.sh status           health of each piece
#    ./dse.sh logs [svc]       logs (compose) or console (console-api|console-ui)
#    ./dse.sh secrets          creates/shows the secrets.env template
#    ./dse.sh tunnel           public tunnel → adapter-github (GitHub App
#                              webhook). Quick tunnel: the URL CHANGES on every
#                              start — update it in the GitHub App (Settings →
#                              Developer settings → GitHub Apps → Webhook URL)
#
#  Secrets: the ./secrets.env file (gitignored, chmod 600) is the local source.
#  Without it the system comes up in "local mode" (echo model, no real
#  GitHub/Slack/Jira) — start reports exactly what is left out. The dev Vault
#  is re-seeded from it on every start (dev-mode loses everything on restart).
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
CONSOLE_DIR="${DSE_CONSOLE_DIR:-$ROOT/../../GitHub/Fintex/dse_console_pane}"
CONSOLE_DIR="$(cd "$CONSOLE_DIR" 2>/dev/null && pwd || echo "$CONSOLE_DIR")"
CONSOLE_API_PORT="${DSE_CONSOLE_API_PORT:-4100}"
CONSOLE_UI_PORT="${DSE_CONSOLE_UI_PORT:-3000}"
SECRETS_FILE="$ROOT/secrets.env"
LOGDIR="$CONSOLE_DIR/.logs"

COMPOSE_FILES=$(ls docker-compose.yml docker-compose.ws*.yml 2>/dev/null)
COMPOSE_ARGS=""; for f in $COMPOSE_FILES; do COMPOSE_ARGS="$COMPOSE_ARGS -f $f"; done

G="\033[32m✓\033[0m"; R="\033[31m✗\033[0m"; Y="\033[33m•\033[0m"
say()  { printf "%b %s\n" "$1" "$2"; }
step() { printf "\n\033[1m==> %s\033[0m\n" "$1"; }

PY="python3"; [ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"

# ----------------------------------------------------------------- secrets --
secrets_template() {
  cat > "$SECRETS_FILE" <<'EOF'
# Fintex DSE — local secrets (NEVER commit; chmod 600).
# Fill in what you use; anything left empty just disables that integration.

# --- GitHub (real flow: issue -> PR -> merge) ---
GITHUB_APP_ID=
GITHUB_APP_INSTALLATION_ID=
# the .pem private key on ONE line: awk 'BEGIN{ORS="\\n"}1' app.pem
GITHUB_APP_PRIVATE_KEY=
GITHUB_WEBHOOK_SECRET=

# --- Real model (Coder/Planner). Empty = the local echo model only ---
ANTHROPIC_API_KEY=
# Gemini 3.8 Flash (gemini/flash) — the chosen model for Planner/Tester/Router.
# Empty = those stages fall back to whatever DSE_CODER_MODEL holds.
GEMINI_API_KEY=

# --- Slack (optional) ---
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=

# --- Jira (optional) ---
JIRA_API_TOKEN=
JIRA_WEBHOOK_SECRET=

# --- Teams (optional) ---
TEAMS_APP_ID=
TEAMS_APP_PASSWORD=
TEAMS_OUTGOING_SECRET=
EOF
  chmod 600 "$SECRETS_FILE"
}

load_secrets() {
  SECRETS_LOADED=no
  if [ -f "$SECRETS_FILE" ]; then
    set -a; # shellcheck disable=SC1090
    source "$SECRETS_FILE"; set +a
    SECRETS_LOADED=yes
  fi
}

seed_vault() {
  # The dev Vault is in-memory: re-seed on every start (source: secrets.env).
  docker exec dse_vault sh -c 'exit 0' 2>/dev/null || return 0
  local v="docker exec -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=${VAULT_DEV_ROOT_TOKEN:-dse_dev_root} dse_vault vault"
  if [ -n "${GITHUB_APP_ID:-}" ]; then
    $v kv put secret/dse/github-app app_id="$GITHUB_APP_ID" \
      installation_id="${GITHUB_APP_INSTALLATION_ID:-}" \
      private_key="${GITHUB_APP_PRIVATE_KEY:-}" \
      webhook_secret="${GITHUB_WEBHOOK_SECRET:-}" >/dev/null && say "$G" "Vault: secret/dse/github-app seeded"
  fi
  if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${GEMINI_API_KEY:-}" ]; then
    $v kv put secret/dse/model-gateway/providers \
      anthropic_api_key="${ANTHROPIC_API_KEY:-}" \
      gemini_api_key="${GEMINI_API_KEY:-}" >/dev/null \
      && say "$G" "Vault: secret/dse/model-gateway/providers seeded"
  fi
}

# ------------------------------------------------------------------- start --
cmd_start() {
  local WITH_PREVIEW=yes WITH_CONSOLE=yes
  for a in "$@"; do
    [ "$a" = "--no-preview" ] && WITH_PREVIEW=no
    [ "$a" = "--no-console" ] && WITH_CONSOLE=no
  done

  step "0/8 prerequisites"
  docker info >/dev/null 2>&1 || { say "$R" "Docker is not running — open Docker Desktop and re-run."; exit 1; }
  say "$G" "docker ok"
  command -v k3d >/dev/null || { [ "$WITH_PREVIEW" = yes ] && { say "$Y" "k3d missing — previews disabled"; WITH_PREVIEW=no; }; }
  command -v kubectl >/dev/null || { [ "$WITH_PREVIEW" = yes ] && { say "$Y" "kubectl missing — previews disabled"; WITH_PREVIEW=no; }; }

  step "1/8 secrets"
  load_secrets
  if [ "$SECRETS_LOADED" = yes ]; then
    say "$G" "secrets.env loaded"
    [ -z "${GITHUB_APP_ID:-}" ]     && say "$Y" "GITHUB_APP_* empty — real PRs/comments disabled"
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
      # with the key, Planner/Coder run against the REAL Claude (via gateway +
      # virtual key). Without the export, compose falls back to the 'fake'
      # default — which yields an empty plan and the task escalates at the gate
      # (found on the first real run).
      export DSE_CODER_SUBSTRATE="${DSE_CODER_SUBSTRATE:-claude-agent}"
      say "$G" "Coder substrate: $DSE_CODER_SUBSTRATE (real Claude via gateway)"
    else
      say "$Y" "ANTHROPIC_API_KEY empty — fake substrate (no real Coder)"
    fi
  else
    secrets_template
    say "$Y" "secrets.env did not exist — TEMPLATE created at $SECRETS_FILE"
    say "$Y" "fill it in and re-run ./dse.sh start for the real GitHub/model flow"
  fi

  step "2/8 infra base (postgres, temporal, vault, redis)"
  docker network inspect dse_net >/dev/null 2>&1 || docker network create dse_net >/dev/null
  # shellcheck disable=SC2086
  docker compose $COMPOSE_ARGS up -d --quiet-pull 2>&1 | grep -vE "Running|Started|Created" || true
  until docker exec dse_postgres pg_isready -U dse -d dse >/dev/null 2>&1; do sleep 1; done
  say "$G" "postgres ready"
  until curl -sf http://localhost:8200/v1/sys/health >/dev/null 2>&1; do sleep 1; done
  say "$G" "vault ready"
  seed_vault

  step "3/8 migrations"
  if $PY -c "import psycopg2" 2>/dev/null; then
    $PY scripts/migrate.py | tail -1
  else
    say "$Y" "psycopg2 unavailable on the host — applying via the container's psql"
    for f in migrations/*.sql; do docker exec -i dse_postgres psql -U dse -d dse -q -f - < "$f" >/dev/null 2>&1 || true; done
    say "$G" "migrations applied (idempotent)"
  fi

  step "4/8 auxiliary images"
  docker image inspect dse-sandbox-base:wsc3 >/dev/null 2>&1 \
    || { say "$Y" "building dse-sandbox-base:wsc3…"; docker build -q -t dse-sandbox-base:wsc3 -f services/sandbox-runtime/docker/Dockerfile.sandbox-base services/sandbox-runtime >/dev/null; }
  say "$G" "sandbox base image ok"

  step "5/8 preview cluster (k3d + Argo CD + Traefik + registry)"
  if [ "$WITH_PREVIEW" = yes ]; then
    if ! k3d cluster list 2>/dev/null | grep -q "^dse-preview\b"; then
      bash infra/k8s-local/setup-k3d-argocd.sh
    else
      # a Docker restart changes the API server port — always reconcile
      k3d kubeconfig merge dse-preview --kubeconfig-merge-default --kubeconfig-switch-context >/dev/null 2>&1 || true
      say "$G" "cluster dse-preview already exists"
    fi
    bash infra/k8s-local/gen-kubeconfig-internal.sh >/dev/null
    say "$G" "internal kubeconfig generated (worker → cluster)"
  else
    say "$Y" "previews skipped (--no-preview / k3d missing)"
  fi

  step "6/8 services (adapters, orchestrator, gateway, projector…)"
  # shellcheck disable=SC2086
  { docker compose $COMPOSE_ARGS up -d 2>&1 | grep -cE "Started|Recreated" || true; } | xargs -I{} echo "   {} containers (re)started"
  until curl -sf http://localhost:8900/health >/dev/null 2>&1; do sleep 1; done
  say "$G" "orchestrator worker up"
  until curl -sf http://localhost:4000/health/liveliness >/dev/null 2>&1; do sleep 1; done
  say "$G" "model-gateway up"

  step "7/8 console (console API + admin-ui)"
  if [ "$WITH_CONSOLE" = yes ] && [ -d "$CONSOLE_DIR" ]; then
    mkdir -p "$LOGDIR"
    if ! curl -sf "http://localhost:${CONSOLE_API_PORT}/health/integrations" >/dev/null 2>&1; then
      ( cd "$CONSOLE_DIR/control-plane/api" \
        && DSE_DATA_SOURCE=controlplane PORT="$CONSOLE_API_PORT" \
           nohup npx tsx src/server.ts > "$LOGDIR/api.log" 2>&1 & echo $! > "$LOGDIR/api.pid" )
      until curl -sf "http://localhost:${CONSOLE_API_PORT}/health/integrations" >/dev/null 2>&1; do sleep 1; done
    fi
    say "$G" "console API :$CONSOLE_API_PORT"
    if ! lsof -nP -iTCP:"$CONSOLE_UI_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      ( cd "$CONSOLE_DIR/control-plane/admin-ui" \
        && API_PROXY_TARGET="http://localhost:${CONSOLE_API_PORT}" \
           nohup npm run dev > "$LOGDIR/ui.log" 2>&1 & echo $! > "$LOGDIR/ui.pid" )
      until curl -so /dev/null "http://localhost:${CONSOLE_UI_PORT}/" 2>/dev/null; do sleep 1; done
    fi
    say "$G" "admin-ui :$CONSOLE_UI_PORT"
  else
    [ "$WITH_CONSOLE" = yes ] && say "$Y" "console not found at $CONSOLE_DIR (set DSE_CONSOLE_DIR)" \
                              || say "$Y" "console skipped (--no-console)"
  fi

  step "8/8 done"
  cmd_status
  cat <<EOF

  Console ......... http://localhost:${CONSOLE_UI_PORT}   (Basic auth: see admin-ui/.env.local)
  Temporal UI ..... http://localhost:8088
  Previews ........ http://<ns>.preview.localhost:8081 (link shows up in the PR)
  Console logs .... $LOGDIR/{api,ui}.log
EOF
  if [ "$SECRETS_LOADED" = no ] || [ -z "${GITHUB_APP_ID:-}" ]; then
    printf "\n  \033[33mReminder:\033[0m fill in %s and re-run ./dse.sh start\n" "$SECRETS_FILE"
    printf "  (without it: no real PRs on GitHub — everything else works)\n"
  fi
}

# ------------------------------------------------------------------- status --
check() { # check <name> <command...>
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then say "$G" "$name"; else say "$R" "$name"; fi
}
cmd_status() {
  echo ""
  check "postgres"            docker exec dse_postgres pg_isready -U dse -d dse
  check "temporal"            sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse_temporal Up"'
  check "vault"               curl -sf http://localhost:8200/v1/sys/health
  check "model-gateway :4000" curl -sf http://localhost:4000/health/liveliness
  check "orchestrator :8900"  curl -sf http://localhost:8900/health
  check "adapter-github"      sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse_adapter_github Up"'
  check "adapter-slack"       sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse_adapter_slack Up"'
  check "adapter-jira"        sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse_adapter_jira Up"'
  check "ingest (gw+disp)"    sh -c 'docker ps --format "{{.Names}}" | grep -q dse_ingest_gateway && docker ps --format "{{.Names}}" | grep -q dse_ingest_dispatcher'
  check "console-projector"   sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse_console_projector Up"'
  check "gitserver (GitOps)"  sh -c 'docker ps --format "{{.Names}} {{.Status}}" | grep -q "dse-wse-gitserver Up"'
  check "cluster k3d"         sh -c 'kubectl --context k3d-dse-preview get nodes 2>/dev/null | grep -q Ready'
  check "argo cd"             sh -c 'kubectl --context k3d-dse-preview -n argocd get deploy argocd-server -o jsonpath="{.status.availableReplicas}" 2>/dev/null | grep -qE "^[1-9]"'
  check "console API :${CONSOLE_API_PORT}" curl -sf "http://localhost:${CONSOLE_API_PORT}/health/integrations"
  check "admin-ui :${CONSOLE_UI_PORT}"     sh -c "curl -so /dev/null http://localhost:${CONSOLE_UI_PORT}/"
  # secrets in the containers (indicator only, no values)
  if docker exec dse_orchestrator sh -c '[ -n "$GITHUB_APP_ID" ]' 2>/dev/null; then
    say "$G" "GitHub App creds injected"
  else
    say "$Y" "GitHub App creds MISSING (real PR flow disabled — fill in secrets.env)"
  fi
  if [ -f "$TUNNEL_DIR/webhook.pid" ] && kill -0 "$(cat "$TUNNEL_DIR/webhook.pid")" 2>/dev/null; then
    say "$G" "webhook tunnel: $(tunnel_url)"
  else
    say "$Y" "webhook tunnel stopped (./dse.sh tunnel so GitHub can reach the adapter)"
  fi
}

# --------------------------------------------------------------------- stop --
cmd_stop() {
  local ALL=no; [ "${1:-}" = "--all" ] && ALL=yes
  step "stopping console"
  for p in api ui; do
    [ -f "$LOGDIR/$p.pid" ] && kill "$(cat "$LOGDIR/$p.pid")" 2>/dev/null && rm -f "$LOGDIR/$p.pid" || true
  done
  pkill -f "tsx src/server.ts" 2>/dev/null || true
  pkill -f "next dev -p ${CONSOLE_UI_PORT}" 2>/dev/null || true
  say "$G" "console stopped"
  [ -f "$TUNNEL_DIR/webhook.pid" ] && kill "$(cat "$TUNNEL_DIR/webhook.pid")" 2>/dev/null && rm -f "$TUNNEL_DIR/webhook.pid" && say "$G" "tunnel stopped" || true
  step "stopping services"
  if [ "$ALL" = yes ]; then
    # shellcheck disable=SC2086
    docker compose $COMPOSE_ARGS down
    command -v k3d >/dev/null && k3d cluster delete dse-preview 2>/dev/null || true
    say "$G" "everything torn down (compose down + cluster)"
  else
    # shellcheck disable=SC2086
    docker compose $COMPOSE_ARGS stop
    say "$G" "containers stopped (state preserved — ./dse.sh start brings them back)"
  fi
}

# ------------------------------------------------------------------- tunnel --
TUNNEL_DIR="$ROOT/.tunnel"
tunnel_url() { grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$TUNNEL_DIR/webhook.log" 2>/dev/null | head -1; }

cmd_tunnel() {
  command -v cloudflared >/dev/null || { say "$R" "cloudflared not installed (brew install cloudflared)"; exit 1; }
  mkdir -p "$TUNNEL_DIR"
  if [ -f "$TUNNEL_DIR/webhook.pid" ] && kill -0 "$(cat "$TUNNEL_DIR/webhook.pid")" 2>/dev/null; then
    say "$G" "tunnel already running: $(tunnel_url)"
  else
    : > "$TUNNEL_DIR/webhook.log"
    nohup cloudflared tunnel --url http://localhost:8802 > "$TUNNEL_DIR/webhook.log" 2>&1 &
    echo $! > "$TUNNEL_DIR/webhook.pid"
    until tunnel_url >/dev/null 2>&1 && [ -n "$(tunnel_url)" ]; do sleep 1; done
    say "$G" "tunnel up: $(tunnel_url)"
  fi
  cat <<EOF

  Webhook URL for the GitHub App (update it if it changed):
    $(tunnel_url)/github/webhook

  GitHub → Settings → Developer settings → GitHub Apps → DSE Fintex Demo
  → Webhook URL. (Quick tunnel: the URL changes on every tunnel start. For a
  fixed URL: named tunnel with your own domain — see
  infra/preview-exposure.md, same mechanism.)
EOF
}

# --------------------------------------------------------------------- logs --
cmd_logs() {
  case "${1:-}" in
    console-api) tail -f "$LOGDIR/api.log" ;;
    console-ui)  tail -f "$LOGDIR/ui.log" ;;
    # shellcheck disable=SC2086
    "")          docker compose $COMPOSE_ARGS logs -f --tail 50 ;;
    # shellcheck disable=SC2086
    *)           docker compose $COMPOSE_ARGS logs -f --tail 100 "$1" ;;
  esac
}

case "${1:-}" in
  start)   shift; cmd_start "$@" ;;
  stop)    shift; cmd_stop "${1:-}" ;;
  status)  load_secrets; cmd_status ;;
  logs)    shift; cmd_logs "${1:-}" ;;
  tunnel)  cmd_tunnel ;;
  secrets) [ -f "$SECRETS_FILE" ] && { echo "already exists: $SECRETS_FILE (keys:)"; grep -oE "^[A-Z_]+" "$SECRETS_FILE"; } || { secrets_template; echo "template created: $SECRETS_FILE"; } ;;
  *) sed -n '3,20p' "$0" ;;
esac
