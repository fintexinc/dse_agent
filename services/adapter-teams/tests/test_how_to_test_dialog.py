"""O "How to test" no Teams — paridade com o modal do Slack, pelo task module.

O card final (`pr_ready`) ganha o botão; o clique chega como `invoke`
(task/fetch) e a resposta é o diálogo SÍNCRONO com passos, login de seed e o
link composto. A rota que hoje assume "task/fetch = plano" passa a ramificar
pelo `action_id` que viaja no `data` do card — o mesmo canal que já carrega o
`work_item_id`.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from adapter_teams.card import how_to_test_dialog, status_card

_GUIA = {"steps": ["Abra /planos", "Clique em Nova Simulação"],
         "login": "demo@acme.com / demo123 (supabase/seed.sql)"}


def _acoes(card: dict) -> dict[str, dict]:
    return {a["data"]["action_id"]: a for a in card["content"]["actions"]}


def _texto_todo(no) -> str:
    if isinstance(no, dict):
        return "\n".join(_texto_todo(v) for v in no.values())
    if isinstance(no, list):
        return "\n".join(_texto_todo(i) for i in no)
    return str(no)


# --- o botão no card --------------------------------------------------------

def test_the_pr_ready_card_offers_how_to_test_as_a_dialog():
    acoes = _acoes(status_card("c", status="pr_ready", work_item_id="wi_x"))
    assert "dse_how_to_test" in acoes, "o card final não oferece o How to test"
    acao = acoes["dse_how_to_test"]
    assert acao["data"]["msteams"] == {"type": "task/fetch"}
    assert acao["data"]["work_item_id"] == "wi_x"


def test_other_statuses_do_not_offer_it():
    for status in ("implementing", "awaiting_plan_approval", "done"):
        acoes = _acoes(status_card("c", status=status, work_item_id="wi_x"))
        assert "dse_how_to_test" not in acoes, status


# --- o diálogo --------------------------------------------------------------

def test_the_dialog_renders_steps_login_and_the_composed_link():
    env = how_to_test_dialog("wi_x", _GUIA, url="https://p.example",
                             deep_path="/planos")
    assert env["task"]["type"] == "continue"
    corpo = _texto_todo(env["task"]["value"]["card"])
    assert "Nova Simulação" in corpo
    assert "demo@acme.com" in corpo
    assert "https://p.example/planos" in corpo, "o link composto leva direto à tela"


def test_the_dialog_without_login_omits_the_credentials_line():
    env = how_to_test_dialog("wi_x", {"steps": ["Abra /"], "login": ""},
                             url="https://p.example", deep_path=None)
    corpo = _texto_todo(env["task"]["value"]["card"])
    assert "Login" not in corpo, "linha de login vazia é ruído que confunde"


# --- a rota ramifica pelo action_id ----------------------------------------

@pytest.fixture()
def cliente(monkeypatch):
    from adapter_teams import app as app_mod

    monkeypatch.setattr(app_mod, "is_activated", lambda: True)
    monkeypatch.setattr(app_mod, "audit_emit", lambda **kw: None)
    monkeypatch.setattr(app_mod, "_verificar_assinatura",
                        lambda body, hdr: json.loads(body), raising=False)
    return TestClient(app_mod.app)


def _fetch(action_id: str) -> dict:
    return {
        "type": "invoke", "name": "task/fetch",
        "conversation": {"id": "19:c@thread.v2"}, "id": "act-1",
        "serviceUrl": "https://smba/br/", "from": {"id": "29:user", "name": "A"},
        "value": {"data": {"dse": True, "action_id": action_id,
                           "work_item_id": "wi_route"}},
    }


def test_task_fetch_routes_by_the_action_id_in_the_card(cliente, monkeypatch):
    from adapter_teams import app as app_mod

    monkeypatch.setattr(app_mod, "_plan_dialog_for",
                        lambda activity: {"task": {"type": "continue",
                                                   "value": {"marker": "PLAN"}}})
    monkeypatch.setattr(app_mod, "_how_to_test_dialog_for",
                        lambda activity: {"task": {"type": "continue",
                                                   "value": {"marker": "GUIDE"}}})

    r1 = cliente.post("/teams/messages", json=_fetch("dse_how_to_test"))
    assert r1.json()["task"]["value"]["marker"] == "GUIDE"

    r2 = cliente.post("/teams/messages", json=_fetch("dse_plan_details"))
    assert r2.json()["task"]["value"]["marker"] == "PLAN", (
        "o Details tem que continuar caindo no diálogo do plano"
    )
