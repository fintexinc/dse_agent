"""O card só serve se SAIR e se o clique VOLTAR.

O renderizador (`card.py`) e o leitor (`events.card_verdict`) são puros. Estes
testes ligam os dois ao caminho real: o backend que posta no connector e o
endpoint que recebe o clique assinado.

Regressão que este arquivo existe para impedir: o Teams tem DOIS verbos —
`send_activity` (POST, cria) e `update_activity` (PUT, edita a mesma mensagem).
O writer de comentário mutável usa os dois, e um card que só aparece no POST
some no primeiro edit — que é exatamente quando o gate aparece.
"""
from __future__ import annotations

import json

from adapter_teams.backend import TeamsCommentBackend


class _ClienteFake:
    def __init__(self):
        self.postados: list[dict] = []
        self.editados: list[dict] = []

    def send_activity(self, *, service_url, conversation_id, text, attachments=None):
        self.postados.append({"text": text, "attachments": attachments})
        return "activity-1"

    def update_activity(self, *, service_url, conversation_id, activity_id, text,
                        attachments=None):
        self.editados.append({"text": text, "attachments": attachments})


def _ref(**extra):
    # Simétrico ao Slack: quem CHAMA renderiza e põe em `surface_ref` (lá é
    # `blocks`, aqui é `card`). O backend não sabe montar card, só entregar.
    base = {"conversation_id": "19:c@thread.v2", "service_url": "https://smba/br/"}
    base.update(extra)
    return base


_REF = _ref()


def _card_de(chamada: dict) -> dict:
    anexos = chamada["attachments"]
    assert anexos, "sem attachment o Teams renderiza só o texto"
    return anexos[0]


def test_posting_carries_the_card():
    from adapter_teams.card import status_card

    cli = _ClienteFake()
    TeamsCommentBackend(cli).post(
        _ref(card=status_card("⚙️ implementando", status="implementing")), "⚙️ implementando")

    card = _card_de(cli.postados[0])
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"


def test_editing_carries_the_card_too():
    """O gate NASCE num edit: o card do Approve/Reject nunca passa pelo POST."""
    from adapter_teams.card import status_card

    cli = _ClienteFake()
    backend = TeamsCommentBackend(cli)
    ref = backend.post(_ref(card=status_card("⚙️", status="implementing")), "⚙️")
    backend.edit(
        _ref(card=status_card("📋 Plan ready", status="awaiting_plan_approval")),
        ref, "📋 Plan ready — awaiting human approval (risk: high).")

    card = _card_de(cli.editados[0])
    ids = [a["data"]["action_id"] for a in card["content"]["actions"]]
    assert "dse_plan_approve" in ids and "dse_plan_reject" in ids


def test_the_text_survives_as_fallback():
    """Cliente que não renderiza card (notificação de celular, cliente antigo)
    mostra o `text`. Card sem texto vira notificação vazia."""
    from adapter_teams.card import status_card

    cli = _ClienteFake()
    TeamsCommentBackend(cli).post(
        _ref(card=status_card("⚙️ implementando", status="implementing")), "⚙️ implementando")
    assert "implementando" in cli.postados[0]["text"]


def test_a_backend_without_a_card_still_posts_text():
    """Nenhum caller sem card deve quebrar: `surface_ref` sem `card` continua
    a mensagem de texto de sempre."""
    cli = _ClienteFake()
    TeamsCommentBackend(cli).post(_REF, "só texto")
    assert cli.postados[0]["attachments"] in (None, [])
    assert cli.postados[0]["text"] == "só texto"


def test_the_repo_and_status_reach_the_card():
    from adapter_teams.card import status_card

    cli = _ClienteFake()
    TeamsCommentBackend(cli).post(
        _ref(card=status_card("corpo", status="validating", repo="acme/svc")), "corpo")
    conteudo = json.dumps(_card_de(cli.postados[0]), ensure_ascii=False)
    assert "acme/svc" in conteudo and "⏳ Validate" in conteudo
