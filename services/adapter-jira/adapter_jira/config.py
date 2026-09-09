"""Jira adapter configuration — service account credentials + trigger/approval
parameters. Same convention as the other WS-A adapters (WSA-E2-T1): the WS-F
secrets backend (`dse_secrets`) FIRST if available/responding, otherwise it
falls back to a local env var.

With no real Jira site registered in this session, the Vault read always falls
back (via `VaultUnavailableError`) to the env vars below. The service account
token is scoped (project-level) and is swapped in by Vault in production
(`dse/jira/service_account`).
"""
from __future__ import annotations

import os


def get_tenant_id() -> str:
    """Single-tenant fallback (same env var as the other adapters). The real
    Jira site -> tenant resolution is done by
    `ingest_gateway.resolve_tenant` (WSA-E1-T5); this is only the default."""
    return os.environ.get("DSE_TENANT_ID", "tenant_dev")


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


def get_webhook_secret() -> str:
    return _from_vault_or_env("dse/jira/service_account", "webhook_secret", "JIRA_WEBHOOK_SECRET")


def get_base_url() -> str:
    """Jira Cloud site URL, e.g.: `https://acme.atlassian.net`. Doubles as the
    tenant-resolution `binding_key` (site -> tenant)."""
    return _from_vault_or_env("dse/jira/service_account", "base_url", "JIRA_BASE_URL")


def get_service_account_email() -> str:
    return _from_vault_or_env("dse/jira/service_account", "email", "JIRA_ACCOUNT_EMAIL")


def get_api_token() -> str:
    return _from_vault_or_env("dse/jira/service_account", "api_token", "JIRA_API_TOKEN")


def get_trigger_label() -> str:
    """Label that marks an issue as a task for the DSE (default `dse`)."""
    return os.environ.get("JIRA_TRIGGER_LABEL", "dse")


def get_plan_approved_status() -> str:
    """Name of the column/status whose TRANSITION is read as a plan approval
    (UC5, WSA-E5-T1). E.g.: 'Plan approved'."""
    return os.environ.get("JIRA_PLAN_APPROVED_STATUS", "Plan approved")


def get_plan_rejected_status() -> str:
    """Name of the column/status whose transition is read as a plan REJECTION
    (optional). E.g.: 'Plan rejected'."""
    return os.environ.get("JIRA_PLAN_REJECTED_STATUS", "Plan rejected")


def get_poll_projects() -> list[str]:
    """Jira projects the fallback poller sweeps (CSV in
    `JIRA_POLL_PROJECTS`, e.g.: 'DSE,OPS')."""
    raw = os.environ.get("JIRA_POLL_PROJECTS", "")
    return [p.strip() for p in raw.split(",") if p.strip()]
