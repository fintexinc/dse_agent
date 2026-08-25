from __future__ import annotations

import os

import psycopg2
import pytest

os.environ.setdefault("SLACK_SIGNING_SECRET", "slack_signing_secret_test")
os.environ["DSE_TENANT_ID"] = "test_tenant_slack_adapter"

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
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
            # Item 3 (0040): sem esta limpeza, o consumo one-shot da rodada
            # ANTERIOR sobrevive ao renascimento do item de ts fixo — e o
            # primeiro clique da rodada seguinte vira "already_resolved".
            cur.execute(
                "DELETE FROM verdict_consumptions WHERE work_item_id IN "
                "(SELECT id FROM work_items WHERE tenant_id LIKE 'test_tenant_%')"
            )
            cur.execute("DELETE FROM work_items WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM channel_kill_switches WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM audit_log WHERE tenant_id LIKE 'test_tenant_%'")
        conn.commit()
    finally:
        conn.close()
