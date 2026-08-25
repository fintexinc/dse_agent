from __future__ import annotations

import os

import psycopg2
import pytest

os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "github_webhook_secret_test")
os.environ.setdefault("GITHUB_BOT_LOGIN", "dse-bot")
os.environ.setdefault("GITHUB_TASK_LABEL", "dse")
os.environ["DSE_TENANT_ID"] = "test_tenant_github_adapter"

SUPERUSER_DSN = "postgresql://dse:dse_dev_only@localhost:5432/dse"


@pytest.fixture
def tenant_id():
    return os.environ["DSE_TENANT_ID"]


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    conn = psycopg2.connect(SUPERUSER_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ingest_events WHERE work_item_id IN "
                "(SELECT id FROM work_items WHERE tenant_id LIKE 'test_tenant_%')"
            )
            cur.execute(
                "DELETE FROM comment_state WHERE work_item_id IN "
                "(SELECT id FROM work_items WHERE tenant_id LIKE 'test_tenant_%')"
            )
            cur.execute("DELETE FROM work_items WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM channel_kill_switches WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM tenant_platform_bindings WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM audit_log WHERE tenant_id LIKE 'test_tenant_%'")
        conn.commit()
    finally:
        conn.close()
