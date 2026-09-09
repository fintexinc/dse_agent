"""The escalation comment must not ask for a gesture the surface cannot honour.

For as long as this text has existed it said "re-apply the `dse` label to try
again", on every surface. On GitHub, Slack and Teams there is no such label. On
Jira there was one, and re-applying it did nothing at all — the event id derived
from the card is a lifetime constant, so the sweep recognised the attempt that
had just escalated and moved on. The adapter's own source said so in as many
words: *which does nothing at all*.

Now the Jira gesture is real (take the label off, put it back — see
`adapter_jira.trigger_state`), and it is real ONLY on Jira. So the text has to
know which surface it is going to.
"""
from __future__ import annotations

from dse_orchestrator.local_activities import _STATUS_BODIES, status_body_for


def test_jira_names_the_gesture_that_exists_there():
    body = status_body_for("jira", "escalated", detail="lint failed")
    assert "label" in body.lower()
    assert "remove" in body.lower() or "take" in body.lower(), (
        f"the Jira text does not describe taking the label off: {body}"
    )


def test_the_other_surfaces_never_mention_a_label():
    for source in ("github", "slack", "teams"):
        body = status_body_for(source, "escalated", detail="lint failed")
        assert "label" not in body.lower(), (
            f"{source} was told to use a label it does not have: {body}"
        )


def test_the_reason_survives_on_every_surface():
    for source in ("jira", "github", "slack", "teams"):
        assert "lint failed" in status_body_for(source, "escalated", detail="lint failed")


def test_an_unknown_surface_falls_back_to_the_generic_text():
    assert status_body_for("carrier-pigeon", "escalated", detail="x") == _STATUS_BODIES[
        "escalated"
    ].format(detail="x", status="escalated")
