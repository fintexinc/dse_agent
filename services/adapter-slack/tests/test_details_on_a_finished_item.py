"""Details continua abrindo depois que o item termina — com o desfecho junto.

Medido 2026-08-18: o item wi_3b152b5c… falhou, o operador clicou em "Details"
na mensagem e recebeu "I could not find the task for this message." A tarefa
existia, com plano gravado; o que aconteceu foi que `_plan_for_message` filtra
`status NOT IN _TERMINAL_STATUSES` e descartou a linha que a correlação tinha
achado — a mensagem de erro é imprecisa: achou e recusou.

A exclusão tem uma razão real, escrita em ed881b5f: numa thread com um item
concluído ao lado de outro no gate, o fallback por `thread_ts` pegaria o mais
recente e mostraria o plano ERRADO na tela de decisão. Mas ela caiu nos DOIS
ramos da busca, e o outro — por `bot_ts` — é exato: um `bot_ts` pertence a uma
mensagem só. É esse que casa aqui.

A regressão nasceu na rc.90, quando o Details virou incondicional em toda
mensagem de status sem revisitar o leitor: o lado que renderiza diz "sempre", o
lado que lê diz "nunca se terminal".

E como o canal trunca o erro ("Show less" cortando no meio), o modal é onde o
diagnóstico inteiro cabe.
"""
from __future__ import annotations

import json

import psycopg2
import pytest

import adapter_slack.app as app_module
from adapter_slack.backend import FakeSlackClient

from .test_plan_details_modal import (
    DSN,
    _CH,
    _PLAN,
    _click_details,
    _post_event,
    client,
)


@pytest.fixture
def fake_slack(monkeypatch):
    fake = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake)
    return fake

_ERRO = (
    "activity_retries_exhausted:checkpoint_sandbox:IsolatedStageExecutionUnavailable: "
    "checkpoint failed in Pod dse-sbx-wi-3b152b5c: [gitops_error] GitScopeViolation: "
    "push refused by the remote (scope): git push origin HEAD:refs/heads/dse/wi_3b152b5c "
    "failed (exit 1) in /workspace: To /checkpoint.git ! [remote rejected] "
    "HEAD -> dse/wi_3b152b5c (shallow update not allowed)"
)


def _item_terminal(fake_slack, *, ts: str, com_plano: bool = True, status: str = "failed"):
    """Um item que JÁ TERMINOU, como o operador encontra na thread."""
    work_item_id = _post_event({
        "type": "app_mention", "channel": _CH, "ts": ts,
        "user": "U_PLAN_REQ", "text": f"terminal scenario {ts}",
    })["work_item_id"]
    # A mensagem de status é postada ANTES do término (é a mesma, editada) —
    # é ela que carrega o bot_ts pelo qual o clique correlaciona.
    resp = client.post("/internal/status-comment", json={
        "work_item_id": work_item_id, "channel": _CH,
        "body": "⚙️ implementing", "actor": "system:orchestrator",
        "status": "implementing",
    })
    assert resp.status_code == 200
    post = fake_slack.post_calls[-1]

    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE work_items SET status=%s, plan=%s::jsonb, last_error=%s, "
                "risk_class='high' WHERE id=%s",
                (status, json.dumps(_PLAN) if com_plano else None, _ERRO, work_item_id),
            )
        conn.commit()
    finally:
        conn.close()
    return work_item_id, post


def test_details_still_opens_after_the_item_failed(fake_slack):
    """O caso medido: item terminal, plano no banco, clique no Details."""
    _, post = _item_terminal(fake_slack, ts="9301.000100")
    before = len(fake_slack.views_open_calls)

    result = _click_details(post)

    assert len(fake_slack.views_open_calls) == before + 1, (
        f"nenhum modal abriu para item terminal — {result}"
    )
    rendered = json.dumps(fake_slack.views_open_calls[-1]["view"], ensure_ascii=False)
    assert "Exclude retired levels" in rendered, "o plano tem que aparecer"


def test_the_modal_carries_the_whole_error_the_channel_truncates(fake_slack):
    """O canal corta o erro com 'Show less'; o modal é onde ele cabe inteiro —
    e é o único lugar onde 'shallow update not allowed' (a palavra que resolve
    o caso) chega ao humano."""
    _, post = _item_terminal(fake_slack, ts="9301.000200")
    _click_details(post)

    rendered = json.dumps(fake_slack.views_open_calls[-1]["view"], ensure_ascii=False)
    assert "shallow update not allowed" in rendered, (
        "o desfecho não veio inteiro — o modal repetiu o truncamento do canal"
    )
    assert "failed" in rendered, "o status final tem que aparecer"


def test_a_finished_item_without_a_plan_still_opens(fake_slack):
    """Item que morreu antes do Planner: sem plano, o modal mostra o desfecho
    em vez de recusar a abrir."""
    _, post = _item_terminal(fake_slack, ts="9301.000300", com_plano=False)
    before = len(fake_slack.views_open_calls)

    _click_details(post)

    assert len(fake_slack.views_open_calls) == before + 1, "modal não abriu sem plano"
    rendered = json.dumps(fake_slack.views_open_calls[-1]["view"], ensure_ascii=False)
    assert "shallow update not allowed" in rendered


def test_the_gate_still_wins_over_a_finished_sibling_in_the_same_thread(fake_slack):
    """A proteção que a exclusão dava, e que NÃO pode ser perdida: numa thread
    com um item terminal e outro no gate, o clique no prompt do gate mostra o
    plano DO GATE. Mostrar o plano errado numa tela de decisão é pior que não
    mostrar nada — e este caso não tinha teste nenhum."""
    from .test_plan_details_modal import _item_at_the_plan_gate

    _item_terminal(fake_slack, ts="9301.000400")
    _, post_gate = _item_at_the_plan_gate(fake_slack, ts="9301.000401")

    _click_details(post_gate)

    rendered = json.dumps(fake_slack.views_open_calls[-1]["view"], ensure_ascii=False)
    assert "shallow update not allowed" not in rendered, (
        "o modal do GATE mostrou o desfecho do item terminal — plano errado na "
        "tela de decisão é exatamente o que a exclusão protegia"
    )
