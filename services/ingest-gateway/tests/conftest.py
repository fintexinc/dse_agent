from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

DSN = os.environ.get(
    "DSE_TEST_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
)


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(DSN)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def tenant_id():
    return f"test_tenant_{uuid.uuid4().hex[:8]}"


_SUPERUSER_DSN = os.environ.get(
    "DSE_TEST_SUPERUSER_DATABASE_URL", "postgresql://dse:dse_dev_only@localhost:5432/dse"
)


@pytest.fixture(autouse=True)
def _cleanup_test_rows():
    """Cleans up rows created by the tests (identified by the test_tenant_/wi_
    prefix + the tests' deterministic event_id pattern) so garbage does not
    accumulate across repeated runs against the real Postgres.

    Uses the `dse` superuser (not `dse_app`) because DELETE on work_items/
    ingest_events is deliberately NOT granted to `dse_app` in production (see
    migrations/0001_foundation.sql) — test cleanup is a dev-only concern and
    must not widen the production grant."""
    yield
    conn = psycopg2.connect(_SUPERUSER_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ingest_events WHERE work_item_id IN (SELECT id FROM work_items WHERE tenant_id LIKE 'test_tenant_%')")
            cur.execute("DELETE FROM comment_state WHERE work_item_id IN (SELECT id FROM work_items WHERE tenant_id LIKE 'test_tenant_%')")
            cur.execute("DELETE FROM work_items WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM channel_kill_switches WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM tenant_platform_bindings WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM jira_transition_queue WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM jira_poll_state WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM audit_log WHERE tenant_id LIKE 'test_tenant_%'")
            # WSA-E6-T2b: steering now resolves the role via dse_console_identity /
            # dse_access_bundle (WS-F). The steering tests create test principals
            # (prefix `usr_test_`) + rows in those tables; clean them up here
            # (superuser) without widening production grants. Order:
            # console_identity before principals (FK), access_bundle by tenant
            # prefix.
            cur.execute("DELETE FROM dse_access_bundle WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute(
                "DELETE FROM dse_console_identity "
                "WHERE tenant_id LIKE 'test_tenant_%' OR principal_id LIKE 'usr_test_%'"
            )
            cur.execute("DELETE FROM principals WHERE id LIKE 'usr_test_%'")
        conn.commit()
    finally:
        conn.close()
