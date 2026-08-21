"""O connector aceita um card com 202 e corpo VAZIO — e não devolve id.

Medido ao vivo (2026-08-21, rc.111/112): `POST /v3/conversations/{id}/activities`
com `attachments` responde **202 Accepted, corpo vazio**; só a mensagem de texto
puro responde 200 com `{"id": ...}`. O código fazia `resp.json()["id"]` e
estourava — DEPOIS de o card já ter saído.

O efeito era pior que um erro: o card aparecia, a exceção derrubava o endpoint
com 500, e a referência nunca era gravada. Como o `MutableCommentWriter` decide
entre criar e editar pela referência gravada, TODA transição virava um card
novo. O operador viu três cards para uma tarefa, e o Details de cada um sem
item — porque nenhum deles chegou a existir para o writer.

Sem id não há edição possível, e o writer precisa saber disso: um `comment_ref`
inventado faria a próxima transição tentar editar uma activity que não existe.
"""
from __future__ import annotations

import json

import pytest

from adapter_teams.backend import RealTeamsClient


class _Resposta:
    def __init__(self, status: int, corpo: str = ""):
        self.status_code = status
        self.text = corpo
        self._corpo = corpo

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def json(self):
        if not self._corpo:
            raise ValueError("corpo vazio não é JSON")
        return json.loads(self._corpo)


@pytest.fixture()
def cliente(monkeypatch):
    cli = RealTeamsClient.__new__(RealTeamsClient)
    monkeypatch.setattr(cli, "_bearer", lambda: "token", raising=False)
    return cli


def _com_resposta(monkeypatch, resposta):
    import requests
    enviados = []

    def fake_post(url, **kw):
        enviados.append({"url": url, **kw})
        return resposta

    monkeypatch.setattr(requests, "post", fake_post)
    return enviados


def test_a_202_without_a_body_does_not_explode(monkeypatch, cliente):
    _com_resposta(monkeypatch, _Resposta(202, ""))

    ref = cliente.send_activity(service_url="https://smba/br/", conversation_id="19:c",
                                text="oi", attachments=[{"contentType": "x"}])
    assert ref == "", "sem id, a referência é vazia — nunca um id inventado"


def test_a_200_with_an_id_still_returns_it(monkeypatch, cliente):
    """O caminho de texto puro não muda: ele devolve id e continua editável."""
    _com_resposta(monkeypatch, _Resposta(200, '{"id": "1755"}'))

    assert cliente.send_activity(service_url="https://smba/br/", conversation_id="19:c",
                                 text="oi") == "1755"


def test_an_unusable_ref_is_not_stored_so_the_next_post_is_a_new_one():
    """A regra que fecha o defeito: referência sem activity_id não vai para o
    store. Guardá-la faria a próxima transição tentar editar uma activity que
    não existe — e o `PUT` de uma activity inexistente é 404, ou seja, a
    mensagem PARARIA de atualizar."""
    from adapter_teams.backend import is_editable_ref

    assert is_editable_ref(json.dumps({"activity_id": "1755"})) is True
    assert is_editable_ref(json.dumps({"activity_id": ""})) is False
    assert is_editable_ref("") is False
