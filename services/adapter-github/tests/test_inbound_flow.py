"""WSA-E4-T1 — issue labeled/assigned creates a task_request; a comment with a
mention on a plain issue creates a task_request; a comment on a PR
(issue_comment on a PR or pull_request_review_comment) NEVER creates a new
WorkItem — it only correlates (signal) by PR/issue number."""
from __future__ import annotations

import json

import psycopg2
from dse_identity import resolve_principal
from fastapi.testclient import TestClient

from adapter_github.app import app
from .helpers import sign

TENANT_ID = "test_tenant_github_adapter"


def _principal_for(login: str) -> str:
    """Resolves the principal for `login`. A allowlist de direção saiu
    (2026-08-21): estar no canal — aqui, no repo — é a autorização."""
    return resolve_principal("github", login, login)

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _post(payload: dict, event_type: str, delivery_id: str) -> dict:
    body = json.dumps(payload).encode()
    sig = sign(body)
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={"X-GitHub-Event": event_type, "X-GitHub-Delivery": delivery_id, "X-Hub-Signature-256": sig},
    )
    assert resp.status_code == 200
    return resp.json()


def test_issue_labeled_dse_creates_new_task():
    data = _post(
        {
            "action": "labeled",
            "issue": {"number": 100, "title": "Add rate limiting", "body": "please add it"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-100",
    )
    assert data["path"] == "new_task"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT status, source, repo FROM work_items WHERE id = %s", (data["work_item_id"],))
        assert cur.fetchone() == ("new", "github", "acme/widgets")
    conn.close()


def test_issue_labeled_other_label_is_ignored():
    data = _post(
        {
            "action": "labeled",
            "issue": {"number": 101, "title": "t", "body": "b"},
            "label": {"name": "bug"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-101",
    )
    assert data["path"] == "ignored_label"


def test_issue_assigned_creates_new_task():
    data = _post(
        {
            "action": "assigned",
            "issue": {"number": 102, "title": "t", "body": "b"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-102",
    )
    assert data["path"] == "new_task"


def test_issue_comment_with_mention_creates_new_task():
    data = _post(
        {
            "action": "created",
            "issue": {"number": 200, "title": "t", "body": "b"},
            "comment": {"id": 5001, "body": "@dse-bot please handle this", "user": {"login": "bob"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-200",
    )
    assert data["path"] == "new_task"


def test_issue_comment_without_mention_and_no_active_work_item_is_ignored():
    data = _post(
        {
            "action": "created",
            "issue": {"number": 201, "title": "t", "body": "b"},
            "comment": {"id": 5002, "body": "just a random comment", "user": {"login": "bob"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-201",
    )
    assert data["path"] == "ignored_no_mention"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM work_items WHERE tenant_id='test_tenant_github_adapter' "
            "AND source_ref @> %s::jsonb",
            (json.dumps({"repo": "acme/widgets", "number": 201}),),
        )
        assert cur.fetchone()[0] == 0
    conn.close()


def test_pr_issue_comment_never_creates_work_item_even_without_match():
    """A comment on a PR with NO matching active WorkItem -> ZERO WorkItems
    created (WSA-E4-T1), unlike the plain-issue behavior."""
    data = _post(
        {
            "action": "created",
            "issue": {"number": 300, "pull_request": {"url": "https://api.github.com/x"}},
            "comment": {"id": 5003, "body": "looks good to me", "user": {"login": "carol"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-300",
    )
    assert data["path"] == "ignored_no_active_work_item"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM work_items WHERE tenant_id='test_tenant_github_adapter' "
            "AND source_ref @> %s::jsonb",
            (json.dumps({"repo": "acme/widgets", "number": 300}),),
        )
        assert cur.fetchone()[0] == 0
    conn.close()


def test_pr_comment_on_active_work_item_correlates_as_signal_zero_new_work_items():
    created = _post(
        {
            "action": "labeled",
            "issue": {"number": 400, "title": "t", "body": "b"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-400",
    )
    work_item_id = created["work_item_id"]
    _principal_for("carol")

    review = _post(
        {
            "action": "created",
            "issue": {"number": 400, "pull_request": {"url": "https://api.github.com/x"}},
            "comment": {"id": 5004, "body": "please rename this variable", "user": {"login": "carol"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-401",
    )
    assert review["path"] == "signal"
    assert review["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        # still only 1 work_item for number 400 — the review comment created no other.
        cur.execute(
            "SELECT count(*) FROM work_items WHERE tenant_id='test_tenant_github_adapter' "
            "AND source_ref @> %s::jsonb",
            (json.dumps({"repo": "acme/widgets", "number": 400}),),
        )
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT kind FROM ingest_events WHERE work_item_id=%s ORDER BY id", (work_item_id,))
        kinds = [r[0] for r in cur.fetchall()]
        assert kinds == ["task_request", "review_comment"]
    conn.close()


def test_pull_request_review_comment_event_correlates_as_signal():
    created = _post(
        {
            "action": "labeled",
            "issue": {"number": 500, "title": "t", "body": "b"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-500",
    )
    work_item_id = created["work_item_id"]
    _principal_for("dave")

    review = _post(
        {
            "action": "created",
            "pull_request": {"number": 500},
            "comment": {"id": 5005, "body": "nit: naming", "user": {"login": "dave"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "pull_request_review_comment",
        "delivery-501",
    )
    assert review["path"] == "signal"
    assert review["work_item_id"] == work_item_id


def test_toctou_snapshot_not_refetched_on_redelivery_with_edited_body():
    original = _post(
        {
            "action": "labeled",
            "issue": {"number": 600, "title": "Original title", "body": "Original body"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-600",
    )
    work_item_id = original["work_item_id"]

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ingest_events WHERE work_item_id = %s", (work_item_id,))
        payload = cur.fetchone()[0]
    conn.close()
    assert "Original title" in payload["content_snapshot"]

    # Redelivery (SAME X-GitHub-Delivery -> same message_id -> same event_id)
    # with the issue "edited" -> dedup, snapshot not overwritten.
    edited = _post(
        {
            "action": "labeled",
            "issue": {"number": 600, "title": "EDITED title", "body": "EDITED body"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-600",  # same delivery id = same redelivery
    )
    assert edited["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ingest_events WHERE work_item_id = %s ORDER BY id", (work_item_id,))
        rows = cur.fetchall()
    conn.close()
    assert len(rows) == 1
    assert "Original title" in rows[0][0]["content_snapshot"]
    assert "EDITED" not in rows[0][0]["content_snapshot"]


def test_secret_pattern_in_comment_is_redacted_in_sanitized_content():
    data = _post(
        {
            "action": "created",
            "issue": {"number": 700, "title": "t", "body": "b"},
            "comment": {
                "id": 5006,
                "body": "@dse-bot use this token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 to deploy",
                "user": {"login": "bob"},
            },
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-700",
    )
    assert data["path"] == "new_task"
    work_item_id = data["work_item_id"]

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ingest_events WHERE work_item_id = %s", (work_item_id,))
        payload = cur.fetchone()[0]
    conn.close()

    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" in payload["content_snapshot"]
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in payload["sanitized_content"]


def test_pull_request_review_submitted_carries_review_state_for_verdict():
    """Post-S7 audit: the `pull_request_review` webhook (Request changes /
    Approve in the UI) is the ONLY one carrying `review.state`; without handling
    it, the dispatcher never saw a verdict and the changes_requested loop was
    unreachable. Validates: correlates as a signal + `review_state` in the
    event's source_ref (that is where `_review_signal_payload` derives the
    verdict from)."""
    created = _post(
        {
            "action": "labeled",
            "issue": {"number": 700, "title": "t", "body": "b"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-700",
    )
    work_item_id = created["work_item_id"]
    _principal_for("erin")

    review = _post(
        {
            "action": "submitted",
            "pull_request": {"number": 700},
            "review": {
                "id": 7007,
                "state": "changes_requested",
                "body": "Remove the .dse-task-branch file from the PR",
                "user": {"login": "erin"},
            },
            "repository": {"full_name": "acme/widgets"},
        },
        "pull_request_review",
        "delivery-701",
    )
    assert review["path"] == "signal"
    assert review["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM ingest_events WHERE work_item_id = %s AND event_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (work_item_id,),
        )
        payload = cur.fetchone()[0]
    conn.close()
    assert payload["source_ref"]["review_state"] == "changes_requested"
    assert ".dse-task-branch" in payload["content_snapshot"]
