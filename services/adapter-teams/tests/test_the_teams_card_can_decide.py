"""O card do Teams: mesma informação do Slack, e um gesto que decide.

Antes disto o adapter postava `{"type": "message", "text": body}` — texto puro.
O humano do Slack vê o repositório, em que etapa está, um botão Details, e no
gate os botões Approve/Reject. O do Teams via uma frase.

A metade grave é a segunda: sem gesto de aprovação, uma tarefa de risco `high`
pelo Teams para em `awaiting_plan_approval` para sempre. Não é degradação de
experiência, é um beco — e ele fica invisível enquanto todas as tarefas de
teste caem em `low` e são auto-aprovadas (foi o que mascarou até hoje).

O Adaptive Card é o Block Kit do Teams, e `Action.Submit` é o clique: o Teams
entrega o clique como uma activity `message` COM `value` e SEM texto — por isso
ele passa pela mesma porta assinada de sempre, sem rota nova no endpoint.
"""
from __future__ import annotations

import pytest

from adapter_teams.card import status_card
from adapter_teams import events


def _acoes(card: dict) -> list[dict]:
    return card["content"].get("actions") or []


def _texto_todo(card: dict) -> str:
    def anda(no):
        if isinstance(no, dict):
            for k, v in no.items():
                yield from anda(v)
        elif isinstance(no, list):
            for item in no:
                yield from anda(item)
        elif isinstance(no, str):
            yield no
    return "\n".join(anda(card["content"]))


# --- o card ---------------------------------------------------------------

def test_it_is_a_real_adaptive_card_attachment():
    card = status_card("corpo", status="implementing", repo="acme/svc")
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["type"] == "AdaptiveCard"
    assert card["content"]["version"], "sem version o Teams recusa o card inteiro"


def test_the_repo_is_shown_when_known_and_absent_when_not():
    assert "acme/svc" in _texto_todo(status_card("c", status="implementing", repo="acme/svc"))
    assert "acme/svc" not in _texto_todo(status_card("c", status="implementing"))


def test_the_body_survives_verbatim():
    assert "⚙️ implementando agora" in _texto_todo(
        status_card("⚙️ implementando agora", status="implementing"))


def test_an_in_flight_status_shows_the_same_bar_the_slack_card_shows():
    from dse_contracts.surface import progress_line

    texto = _texto_todo(status_card("c", status="validating"))
    assert progress_line("validating") in texto


def test_a_terminal_status_shows_no_bar():
    texto = _texto_todo(status_card("acabou", status="escalated"))
    assert "⏳" not in texto


# --- o gesto que decide ---------------------------------------------------

def test_details_is_always_offered():
    for status in ("implementing", "awaiting_plan_approval", "escalated"):
        ids = [a.get("data", {}).get("action_id") for a in _acoes(status_card("c", status=status))]
        assert "dse_plan_details" in ids, status


def test_approve_and_reject_appear_only_at_the_gate():
    no_gate = [a.get("data", {}).get("action_id")
               for a in _acoes(status_card("c", status="awaiting_plan_approval"))]
    assert "dse_plan_approve" in no_gate and "dse_plan_reject" in no_gate

    fora = [a.get("data", {}).get("action_id")
            for a in _acoes(status_card("c", status="implementing"))]
    assert "dse_plan_approve" not in fora and "dse_plan_reject" not in fora


def test_the_reject_button_carries_its_route():
    """`reject` sem rota deixa o workflow sem para onde ir — o valor viaja no
    botão, exatamente como no Slack."""
    reject = [a for a in _acoes(status_card("c", status="awaiting_plan_approval"))
              if a.get("data", {}).get("action_id") == "dse_plan_reject"][0]
    assert reject["data"]["value"] == "reject:re_plan"


def test_every_action_is_a_submit_the_bot_receives():
    for a in _acoes(status_card("c", status="awaiting_plan_approval")):
        assert a["type"] == "Action.Submit", (
            "Action.OpenUrl não volta para o bot; Action.Execute exige responder "
            "invoke — Submit chega como activity de mensagem, que já é assinada"
        )
        assert a["data"]["dse"] is True, "o marcador é o que separa clique de conversa"


# --- a volta: o clique vira evento ---------------------------------------

def _clique(action_id: str, value: str = "") -> dict:
    return {
        "type": "message",
        "conversation": {"id": "19:conv@thread.v2"},
        "id": "activity-1",
        "serviceUrl": "https://smba.trafficmanager.net/br/",
        "from": {"id": "29:user", "name": "Andre"},
        "value": {"dse": True, "action_id": action_id, "value": value},
    }


def test_a_card_click_is_an_approval_not_a_chat_message():
    from dse_contracts import EventKind

    assert events.event_kind(_clique("dse_plan_approve", "approve")) is EventKind.approval
    assert events.event_kind({"type": "message", "text": "oi"}) is not EventKind.approval


def test_the_click_carries_the_verdict_the_dispatcher_reads():
    assert events.card_verdict(_clique("dse_plan_approve", "approve")) == ("approved", None)
    assert events.card_verdict(_clique("dse_plan_reject", "reject:re_plan")) == ("rejected", "re_plan")


def test_a_details_click_is_not_a_verdict():
    """Details vive em TODA mensagem. Se ele virasse veredito, um clique
    curioso aprovaria o plano — o Slack desvia antes do fallthrough e aqui
    tem de ser igual."""
    assert events.is_details_click(_clique("dse_plan_details", "details")) is True
    assert events.is_details_click(_clique("dse_plan_approve", "approve")) is False


def test_a_plain_message_is_untouched_by_any_of_this():
    from dse_contracts import EventKind

    plain = {"type": "message", "text": "<at>DSE</at> faça algo",
             "entities": [{"type": "mention", "mentioned": {"id": "28:bot"}}]}
    assert events.event_kind(plain) is EventKind.task_request
    assert events.card_verdict(plain) is None
