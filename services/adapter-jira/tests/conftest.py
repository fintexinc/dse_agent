from __future__ import annotations

import os

import psycopg2
import pytest

os.environ.setdefault("JIRA_WEBHOOK_SECRET", "jira_webhook_secret_test")
os.environ.setdefault("JIRA_TRIGGER_LABEL", "dse")
os.environ.setdefault("JIRA_PLAN_APPROVED_STATUS", "Plan approved")
os.environ.setdefault("JIRA_PLAN_REJECTED_STATUS", "Plan rejected")
os.environ["DSE_TENANT_ID"] = "test_tenant_jira_adapter"

SUPERUSER_DSN = "postgresql://dse:dse_dev_only@localhost:5432/dse"


@pytest.fixture
def tenant_id():
    return os.environ["DSE_TENANT_ID"]


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    try:
        conn = psycopg2.connect(SUPERUSER_DSN)
    except psycopg2.OperationalError:
        # Part of this suite runs with no database at all (fakes for the Jira
        # client AND for the connection). On a machine without Postgres those
        # tests passed and then reported an ERROR in teardown, which reads
        # exactly like a real failure — the cleanup was the only thing failing.
        # Nothing was written, so there is nothing to delete.
        return
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
            cur.execute("DELETE FROM jira_transition_queue WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM jira_poll_state WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM audit_log WHERE tenant_id LIKE 'test_tenant_%'")
        conn.commit()
    finally:
        conn.close()
