"""Teams adapter configuration — credentials and parameters.

Same convention as the other WS-A adapters (WSA-E2-T1): the WS-F secrets backend
(`dse_secrets`) FIRST if available/responsive, otherwise fall back to a local env
var. With no real Teams tenant in this session, the Vault paths
(`dse/teams/webhook`, `dse/teams/bot`) have no version written and the read falls
back (via `VaultUnavailableError`) to the env vars below — filled with local test
values so the fixtures can run.

Two credential modes (the adapter supports both inbound transports):
  - Outgoing webhook: `TEAMS_OUTGOING_SECRET` (Base64) — HMAC of the body.
  - Bot Framework (Azure Bot): `TEAMS_APP_ID` + `TEAMS_APP_PASSWORD` — used to
    obtain the connector REST bearer token (outbound) and, once activated, to
    validate the inbound JWT Bearer.
"""
from __future__ import annotations

import os


def _from_vault_or_env(vault_path: str, vault_key: str, env_var: str, default: str = "") -> str:
    try:
        from dse_secrets import VaultUnavailableError, get_secret

        try:
            return get_secret(vault_path)[vault_key]
        except VaultUnavailableError:
            pass
    except ImportError:
        pass
    return os.environ.get(env_var, default)


def get_tenant_id() -> str:
    """Single-tenant fallback (same env var as the other adapters). The real
    resolution from AAD tenant guid -> DSE tenant is done by
    `ingest_gateway.resolve_tenant` (WSA-E1-T5) once activated."""
    return os.environ.get("DSE_TENANT_ID", "tenant_dev")


def get_teams_shared_secret() -> str:
    """Base64 secret of the outgoing webhook (used by the signature HMAC)."""
    return _from_vault_or_env("dse/teams/webhook", "shared_secret", "TEAMS_OUTGOING_SECRET")


def get_teams_app_id() -> str:
    return _from_vault_or_env("dse/teams/bot", "app_id", "TEAMS_APP_ID")


def get_teams_app_password() -> str:
    return _from_vault_or_env("dse/teams/bot", "app_password", "TEAMS_APP_PASSWORD")


def get_teams_bot_tenant_id() -> str:
    """Tenant do REGISTRO do bot (AAD) — não confundir com `get_tenant_id()`,
    que é o tenant do DSE. Bot single-tenant só recebe token do emissor do
    próprio tenant; vazio mantém o fluxo multi-tenant antigo."""
    return _from_vault_or_env("dse/teams/bot", "tenant_id", "TEAMS_APP_TENANT_ID")


def get_default_service_url() -> str:
    """Bot Framework connector `serviceUrl` (per region; e.g.
    `https://smba.trafficmanager.net/emea/`). In production it arrives on every
    received Activity; this default is only for outbound initiated internally."""
    return _from_vault_or_env("dse/teams/bot", "service_url", "TEAMS_SERVICE_URL")
