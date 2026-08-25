"""Repo selector (ambiguous-repo clarification — resolve_repo Rung 5): the
static_select + Confirm button pair.

Slack fires `block_actions` as soon as the select is PICKED. Treating that pick
as the decision made the first click irreversible — getting the repo wrong fires
an entire agent turn against the wrong repo. The contract proven here is the
pair's: the select STAGES (nothing is recorded), the button DECIDES, and a
confirmation with no choice warns the human instead of failing mute."""
from __future__ import annotations

import json
import uuid

import psycopg2
import pytest
from fastapi.testclient import TestClient

import adapter_slack.app as app_module
from adapter_slack.app import app
from adapter_slack.backend import FakeSlackClient, repo_select_blocks

from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
SUPERUSER_DSN = DSN.replace("dse_app:dse_app_dev_only", "dse:dse_dev_only")

REPO_A = "andre2654/fintex-wallet"
REPO_B = "andre2654/fintex-demo"


# ---------------------------------------------------------------------------
# Message shape (pure — no database)
# ---------------------------------------------------------------------------
def test_repo_select_blocks_pairs_the_select_with_a_confirm_button():
    """Both elements have to live in the SAME actions block: `state.values` is
    indexed by block_id -> action_id, so it is the shared block_id that lets the
    button click find the select's choice."""
    blocks = repo_select_blocks("wi_abc", [REPO_A, REPO_B], "Which repository?")
    actions = next(b for b in blocks if b["type"] == "actions")

    assert actions["block_id"] == "dse_repo_select:wi_abc"  # carries the work_item_id
    by_id = {e["action_id"]: e for e in actions["elements"]}
    assert set(by_id) == {"dse_repo_select", "dse_repo_confirm"}
    assert by_id["dse_repo_select"]["type"] == "static_select"
    assert by_id["dse_repo_confirm"]["type"] == "button"
    # work_item_id fallback for a click that arrives without a block_id
    assert by_id["dse_repo_confirm"]["value"] == "wi_abc"
    assert [o["value"] for o in by_id["dse_repo_select"]["options"]] == [REPO_A, REPO_B]


def _state_values(work_item_id: str, selected: dict | None) -> dict:
    return {
        "state": {
            "values": {
                f"dse_repo_select:{work_item_id}": {
                    "dse_repo_select": {"type": "static_select", "selected_option": selected}
                }
            }
        }
    }


def test_selected_repo_is_read_from_the_message_state():
    """On the button click the `action` describes the BUTTON — the choice only
    exists in `state.values`, which Slack sends on every message
    block_actions."""
    payload = _state_values("wi_abc", {"value": REPO_A})
    assert app_module._selected_repo_from_state(payload, "dse_repo_select:wi_abc", "wi_abc") == REPO_A


def test_selected_repo_tolerates_every_shape_of_no_choice():
    """`state` is opportunistic, not durable: it may arrive absent, without the
    block, or with `selected_option: null` (deselection, or a re-render that
    wiped the choice). None of the three may turn into a guessed repo."""
    bid, wid = "dse_repo_select:wi_abc", "wi_abc"
    assert app_module._selected_repo_from_state({}, bid, wid) is None
    assert app_module._selected_repo_from_state({"state": {"values": {}}}, bid, wid) is None
    assert app_module._selected_repo_from_state(_state_values(wid, None), bid, wid) is None


def test_selected_repo_never_pairs_the_choice_of_another_work_item():
    """Safety net for the click with no block_id: matches on the work_item_id
    SUFFIX. Scanning the state blindly would pick the first block iterated —
    which may be the choice of ANOTHER task in the same channel."""
    payload = {
        "state": {
            "values": {
                "dse_repo_select:wi_other": {"dse_repo_select": {"selected_option": {"value": REPO_B}}},
                "dse_repo_select:wi_mine": {"dse_repo_select": {"selected_option": {"value": REPO_A}}},
            }
        }
    }
    assert app_module._selected_repo_from_state(payload, "", "wi_mine") == REPO_A
    assert app_module._selected_repo_from_state(payload, "", "wi_unknown") is None


# ---------------------------------------------------------------------------
# /slack/interactions endpoint (requires Postgres — like the rest of the suite)
# ---------------------------------------------------------------------------
def _post_interaction(payload: dict) -> dict:
    body = f"payload={json.dumps(payload)}".encode()
    ts, sig = sign(body)
    resp = client.post(
        "/slack/interactions",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert resp.status_code == 200
    return resp.json()


def _interaction(
    work_item_id: str,
    *,
    action_id: str,
    user: str = "U_STEER",
    selected: dict | None = None,
    block_id: str | None = None,
) -> dict:
    """block_actions template for the select+confirm pair. `state.values` always
    rides along — that is how Slack delivers the message's current state."""
    bid = f"dse_repo_select:{work_item_id}" if block_id is None else block_id
    action: dict = {"action_id": action_id, "block_id": bid}
    if action_id == "dse_repo_confirm":
        action["value"] = work_item_id  # fallback when the block_id does not arrive
    else:
        action["selected_option"] = selected
    return {
        "type": "block_actions",
        "channel": {"id": "C_REPO_SELECT"},
        "message": {"ts": "9500.500"},
        "user": {"id": user},
        "action_ts": f"9500.{uuid.uuid4().int % 1_000_000}",
        "actions": [action],
        **_state_values(work_item_id, selected),
    }


def _make_work_item(tenant_id: str) -> str:
    work_item_id = f"wi_sel_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key) "
            "VALUES (%s,%s,'slack','{}'::jsonb,'usr_test',%s)",
            (work_item_id, tenant_id, f"idem_{work_item_id}"),
        )
    conn.commit()
    conn.close()
    return work_item_id


def _seed_repos(tenant_id: str) -> None:
    """Two repos = the ambiguous case that makes the dropdown exist. It is also
    the allowlist the Confirm checks the choice against."""
    conn = psycopg2.connect(SUPERUSER_DSN)
    with conn.cursor() as cur:
        for repo in (REPO_A, REPO_B):
            cur.execute(
                "INSERT INTO repo_bindings (tenant_id, platform, binding_type, binding_value, repo, base_branch) "
                "VALUES (%s,'slack','workspace',%s,%s,'main') ON CONFLICT DO NOTHING",
                (tenant_id, f"ws_{repo}", repo),
            )
    conn.commit()
    conn.close()


def _clarification_snapshots(work_item_id: str) -> list[str]:
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM ingest_events WHERE work_item_id = %s "
            "AND kind = 'clarification_answer' ORDER BY id",
            (work_item_id,),
        )
        rows = [r[0]["content_snapshot"] for r in cur.fetchall()]
    conn.close()
    return rows


@pytest.fixture
def fake_slack(monkeypatch):
    """Only the Slack transport is faked — signature, database and authorization
    stay real."""
    fake = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake)
    return fake


def test_choosing_in_the_dropdown_decides_nothing(tenant_id, fake_slack):
    """The step that makes the choice reversible. Before the button, touching the
    select RECORDED the signal right away: the first option clicked already
    fired the turn."""
    _seed_repos(tenant_id)
    work_item_id = _make_work_item(tenant_id)

    data = _post_interaction(
        _interaction(work_item_id, action_id="dse_repo_select", selected={"value": REPO_A})
    )

    assert data["path"] == "repo_select_staged"
    assert _clarification_snapshots(work_item_id) == []


def test_confirm_promotes_the_staged_choice_to_a_signal(tenant_id, fake_slack):
    _seed_repos(tenant_id)
    work_item_id = _make_work_item(tenant_id)

    data = _post_interaction(
        _interaction(work_item_id, action_id="dse_repo_confirm", selected={"value": REPO_A})
    )

    assert data == {
        "ok": True, "path": "repo_selected", "work_item_id": work_item_id, "repo": REPO_A,
    }
    # the marker the dispatcher extracts (C4 regex) -> refills input.repo in the
    # workflow; identical to typing `repo=... branch=...` in the thread.
    assert _clarification_snapshots(work_item_id) == [f"repo={REPO_A} branch=main"]
    assert fake_slack.ephemeral_calls[-1]["user"] == "U_STEER"


def test_confirm_without_a_choice_warns_instead_of_failing_silently(tenant_id, fake_slack):
    """If the Confirm finds no choice (nothing selected, or the message state was
    lost), the human needs to know — otherwise they keep clicking a mute button."""
    _seed_repos(tenant_id)
    work_item_id = _make_work_item(tenant_id)

    data = _post_interaction(
        _interaction(work_item_id, action_id="dse_repo_confirm", selected=None)
    )

    assert data["path"] == "repo_select_noop"
    assert _clarification_snapshots(work_item_id) == []
    assert len(fake_slack.ephemeral_calls) == 1


def test_confirm_refuses_a_repo_the_tenant_never_offered(tenant_id, fake_slack):
    """The repo arrives via the message `state`; confining it to repo_bindings
    guarantees that only a repo WE offered can become the target of an agent turn."""
    _seed_repos(tenant_id)
    work_item_id = _make_work_item(tenant_id)

    data = _post_interaction(
        _interaction(work_item_id, action_id="dse_repo_confirm",
                     selected={"value": "outsider/ghost-repo"})
    )

    assert data["path"] == "unknown_repo"
    assert _clarification_snapshots(work_item_id) == []
    assert len(fake_slack.ephemeral_calls) == 1  # refusal explained, not silent


def test_confirm_falls_back_to_the_button_value_when_block_id_is_missing(tenant_id, fake_slack):
    """The button's `value` duplicates the work_item_id precisely for this case."""
    _seed_repos(tenant_id)
    work_item_id = _make_work_item(tenant_id)

    data = _post_interaction(
        _interaction(work_item_id, action_id="dse_repo_confirm",
                     selected={"value": REPO_A}, block_id="")
    )

    assert data["path"] == "repo_selected"
    assert _clarification_snapshots(work_item_id) == [f"repo={REPO_A} branch=main"]
