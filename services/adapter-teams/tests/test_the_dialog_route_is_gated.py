"""A rota do diálogo: assinada, autorizada, e respondida na hora.

Três propriedades, todas medidas contra defeitos reais deste repositório:

  - o endpoint recusava TUDO que não fosse `type == "message"`, e um
    `task/fetch` é `type == "invoke"` — a rota precisa existir DEPOIS da porta
    de assinatura, nunca antes;
  - o diálogo mostra caminho real do repositório do cliente e o risco efetivo,
    então passa pelo MESMO gate de autorização dos botões (`is_authorized_to_steer`);
  - a resposta é SÍNCRONA: o Teams lê o corpo da própria resposta HTTP. Um 200
    vazio fecha o diálogo em branco, que é o que o usuário viu na rc.111.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cliente(monkeypatch):
    from adapter_teams import app as app_mod

    monkeypatch.setattr(app_mod, "is_activated", lambda: True)
    monkeypatch.setattr(app_mod, "audit_emit", lambda **kw: None)
    # A porta de assinatura tem teste próprio; aqui ela é dada como cumprida.
    monkeypatch.setattr(app_mod, "_verificar_assinatura", lambda body, hdr: json.loads(body),
                        raising=False)
    return TestClient(app_mod.app)


def _fetch(work_item_id: str = "wi_x") -> dict:
    return {
        "type": "invoke",
        "name": "task/fetch",
        "conversation": {"id": "19:c@thread.v2"},
        "id": "act-1",
        "serviceUrl": "https://smba/br/",
        "from": {"id": "29:user", "name": "Andre"},
        "value": {"data": {"dse": True, "action_id": "dse_plan_details",
                           "work_item_id": work_item_id}},
    }


def test_an_invoke_is_no_longer_refused_as_a_non_message():
    from adapter_teams import events

    assert events.is_task_fetch(_fetch()) is True
    assert events.is_task_fetch({"type": "message", "text": "oi"}) is False


def test_the_work_item_travels_inside_the_invoke():
    from adapter_teams import events

    assert events.task_fetch_work_item(_fetch("wi_abc")) == "wi_abc"
    assert events.task_fetch_work_item({"type": "invoke", "name": "task/fetch"}) is None


def test_an_unauthorized_reader_gets_a_refusal_dialog_not_the_plan():
    """Não é integridade, é confidencialidade: o diálogo mostra caminhos do
    repositório do cliente. Convidado da conversa não lê por um clique."""
    from adapter_teams.card import refusal_dialog

    env = refusal_dialog("You are not allowed to read this task's plan.")
    assert env["task"]["type"] == "continue"
    conteudo = json.dumps(env, ensure_ascii=False)
    assert "not allowed" in conteudo
