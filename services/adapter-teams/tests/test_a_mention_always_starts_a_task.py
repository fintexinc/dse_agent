"""Mencionar o bot abre tarefa NOVA; falar sem mencionar dirige a atual.

No Slack a distinção vem de graça: uma mensagem na raiz do canal tem `thread_ts`
próprio, então vira tarefa nova, e uma resposta na thread carrega o `thread_ts`
da tarefa, então vira sinal. No Teams não existe esse endereço — `thread_key` é
a conversa inteira, e chat de reunião e 1:1 nem têm thread.

Consequência medida (2026-08-21, wi_b3ab93): a tarefa anterior terminou em
`review_ready` com a PR aberta — status VIVO, não terminal — e o pedido seguinte
do operador virou `signal_recorded` para ela em vez de tarefa nova. O workflow
já havia encerrado, então o sinal morreu em `dispatch_deduped_already_started`.
Da superfície: "enviei e não aconteceu nada".

A menção é o endereço que faltava. Ela já distingue os dois `EventKind` no
adapter; o que faltava era a correlação saber disso — e ela sabe, porque aceita
um `correlation_ref` próprio. Menção manda um ref que nenhum item existente
casa (o id da activity entra nele); mensagem simples manda a conversa e casa
com a tarefa mais recente.

Nada disso toca Slack, GitHub ou Jira: o `correlate` não muda.
"""
from __future__ import annotations

from adapter_teams import events

_CONV = "19:meeting_abc@thread.v2"


def _mencao() -> dict:
    return {
        "type": "message", "id": "act-99",
        "text": "<at>DSE</at> faça outra coisa",
        "conversation": {"id": _CONV},
        "serviceUrl": "https://smba/br/",
        "from": {"id": "29:u", "name": "Andre"},
        "entities": [{"type": "mention", "mentioned": {"id": "28:bot"}}],
    }


def _simples() -> dict:
    return {
        "type": "message", "id": "act-100", "text": "na verdade use outro nome",
        "conversation": {"id": _CONV},
        "serviceUrl": "https://smba/br/",
        "from": {"id": "29:u", "name": "Andre"},
    }


def test_a_mention_correlates_to_nothing_so_it_becomes_a_new_task():
    ref = events.correlation_ref(_mencao())

    assert ref["conversation_id"] == _CONV
    assert ref["root_activity_id"] == "act-99", (
        "sem o id da activity o ref casa com a tarefa anterior da conversa"
    )


def test_a_plain_message_correlates_to_the_conversation():
    ref = events.correlation_ref(_simples())

    assert ref == {"conversation_id": _CONV}, (
        "dirigir precisa casar com a tarefa mais recente — e o `service_url` "
        "fica de fora porque ele MUDA por região e quebraria o casamento"
    )


def test_the_stored_address_never_carries_the_activity_id():
    """`source_ref` é o ENDEREÇO DE RESPOSTA do item, gravado no banco. Se o id
    da activity entrasse nele, a mensagem simples seguinte não casaria com
    nada e nunca daria para dirigir a tarefa."""
    guardado = events.source_ref(_mencao())

    assert "root_activity_id" not in guardado
    assert guardado["conversation_id"] == _CONV
    assert guardado["service_url"]
