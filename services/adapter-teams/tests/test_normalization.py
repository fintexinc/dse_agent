"""Teams Activity normalization -> ConversationEvent fields + the 4 defenses at
the function level (without depending on the frozen `Platform.teams` enum).

These tests exercise the adapter's REAL extraction/idempotency/sanitization
logic — the part that does NOT need the foundation activation. The typed
construction of the ConversationEvent (which does) is covered in
test_activation.py.
"""
from __future__ import annotations

from dse_contracts import EventKind
from ingest_gateway import sanitize_content

from adapter_teams import events
from .helpers import teams_activity


def test_extracts_conversation_actor_and_content():
    act = teams_activity(text="<at>DSE Bot</at>   fix the login bug  ")
    assert events.conversation_id(act) == "19:channel_abc@thread.tacv2"
    assert events.activity_id(act) == "act-0001"
    assert events.service_url(act) == "https://smba.trafficmanager.net/emea/"
    assert events.aad_tenant_id(act) == "aad-tenant-guid-xyz"
    user_id, display = events.actor_of(act)
    assert user_id == "aad-obj-jane"  # prefers the stable aadObjectId
    assert display == "Jane Doe"
    # TOCTOU defense: content is the payload text, with the <at> mention tag stripped
    assert events.clean_text(act) == "fix the login bug"


def test_actor_falls_back_to_from_id_without_aad_object():
    act = teams_activity(aad_object_id=None, from_id="29:aad-user-x")
    user_id, _ = events.actor_of(act)
    assert user_id == "29:aad-user-x"


def test_mention_is_task_request():
    act = teams_activity(text="<at>DSE Bot</at> do the thing")
    assert events.is_mention(act) is True
    assert events.event_kind(act) is EventKind.task_request


def test_mention_via_entity_without_at_tag():
    act = teams_activity(text="do the thing", with_mention_entity=True)
    assert events.is_mention(act) is True
    assert events.event_kind(act) is EventKind.task_request


def test_plain_message_is_clarification_answer():
    act = teams_activity(text="here is my answer", reply_to_id="act-0001")
    assert events.is_mention(act) is False
    assert events.event_kind(act) is EventKind.clarification_answer


def test_source_ref_is_conversation_scoped():
    """A conversa é o escopo — mas o endereço completo inclui o `service_url`
    regional, sem o qual ninguém responde depois (ver
    test_source_ref_can_be_answered.py, achado na ativação de 2026-08-14)."""
    act = teams_activity()
    assert events.source_ref(act) == {
        "conversation_id": "19:channel_abc@thread.tacv2",
        "service_url": "https://smba.trafficmanager.net/emea/",
    }


def test_event_id_is_deterministic_and_idempotent():
    """Defense #4: redelivery of the same webhook (same Activity) -> same event_id."""
    act = teams_activity(activity_id="act-777")
    a = events.compute_event_id(act)
    b = events.compute_event_id(dict(act))  # copy (redelivery)
    assert a == b
    # differs when the activity id changes (a different message)
    assert events.compute_event_id(teams_activity(activity_id="act-778")) != a


def test_sanitization_defense_applies_to_extracted_content():
    """Defense #3: the gateway sanitizes the extracted content (invisible unicode
    is stripped) before any stage that involves a model."""
    zwsp = "​"  # zero-width space (category Cf)
    act = teams_activity(text=f"<at>DSE Bot</at> hi{zwsp}there")
    cleaned = events.clean_text(act)
    assert zwsp in cleaned
    assert zwsp not in sanitize_content(cleaned)
    assert sanitize_content(cleaned) == "hithere"
