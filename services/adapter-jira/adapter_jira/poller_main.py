"""Entrypoint of the fallback poller (`python -m adapter_jira.poller_main`).
Separate process — sweeps the configured projects and reconciles through the
same idempotent path as the webhook (WSA-E5-T2)."""
from __future__ import annotations

import os
import time

from ingest_gateway import get_connection, resolve_tenant

from . import config
from .backend import build_real_jira_client
from .poller import JiraPoller


def main() -> None:  # pragma: no cover - production loop
    interval = float(os.environ.get("JIRA_POLL_INTERVAL_SECONDS", "60"))
    conn = get_connection()
    try:
        tenant = resolve_tenant(conn, platform="jira", binding_key=config.get_base_url() or None)
        conn.commit()
    finally:
        conn.close()

    poller = JiraPoller(
        build_real_jira_client(),
        tenant_id=tenant.tenant_id,
        projects=config.get_poll_projects(),
        trigger_label=config.get_trigger_label(),
        approved_status=config.get_plan_approved_status(),
        rejected_status=config.get_plan_rejected_status(),
    )
    # `projects` here is only the configured fallback: the sweep set is resolved
    # per round from the panel's bindings as well, so printing this list as if
    # it were the answer is how "I bound the board on the site" and "the poller
    # reads that board" came apart in the first place.
    print(
        f"[adapter-jira] poller running (configured={config.get_poll_projects()}, "
        f"plus jira project bindings, interval={interval}s)",
        flush=True,
    )
    while True:
        poller.poll_once()
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    main()
