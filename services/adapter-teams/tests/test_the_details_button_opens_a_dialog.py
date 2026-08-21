"""Details no Teams abre um DIÁLOGO, como o modal do Slack.

A rc.111 desviou o clique de Details antes do fallthrough de veredito — sem
isso um clique curioso aprovaria o plano — mas parou aí: o clique era
consumido, o Teams dizia "Your response was sent to the app", e nada abria.

O equivalente do modal no Teams é o TASK MODULE, e o caminho é outro:
`Action.Submit` com `msteams: {"type": "task/fetch"}` faz o Teams mandar uma
activity `invoke` (não `message`), e o bot responde o diálogo SÍNCRONO, no
corpo da própria resposta HTTP. Por isso o endpoint precisa deixar de recusar
tudo que não é `message`.

Duas coisas que não são detalhe:

  - **Autorização.** O diálogo mostra o que a mensagem do canal não mostra:
    caminhos reais do repositório do cliente e o risco efetivo. O Slack gateia
    com `is_authorized_to_steer` pelo mesmo motivo — não é integridade, é
    confidencialidade: entregar isso a um convidado da conversa é
    reconhecimento por um clique.
  - **De onde vem o work item.** O card carrega o `work_item_id` no `data` da
    ação, posto quando ele foi renderizado. Sem isso, o bot teria de descobrir
    o item pela activity clicada — e uma conversa do Teams sem thread não dá
    esse endereço de volta.
"""
from __future__ import annotations

from adapter_teams.card import plan_details_dialog, status_card


def _acao_details(card: dict) -> dict:
    return [a for a in card["content"]["actions"]
            if a["data"]["action_id"] == "dse_plan_details"][0]


def _texto_todo(no) -> str:
    if isinstance(no, dict):
        return "\n".join(_texto_todo(v) for v in no.values())
    if isinstance(no, list):
        return "\n".join(_texto_todo(i) for i in no)
    return str(no)


# --- o botão pede o diálogo ------------------------------------------------

def test_details_asks_teams_for_a_dialog():
    acao = _acao_details(status_card("c", status="implementing", work_item_id="wi_x"))
    assert acao["data"]["msteams"] == {"type": "task/fetch"}, (
        "sem este marcador o Teams entrega o clique como mensagem e o bot não "
        "tem como responder um diálogo"
    )


def test_the_card_carries_the_item_so_the_dialog_can_find_the_plan():
    acao = _acao_details(status_card("c", status="implementing", work_item_id="wi_abc"))
    assert acao["data"]["work_item_id"] == "wi_abc"


def test_the_decision_buttons_do_not_ask_for_a_dialog():
    """Approve/Reject são veredito, não leitura: eles seguem chegando como
    mensagem, pelo caminho de sinal que já existe."""
    card = status_card("c", status="awaiting_plan_approval", work_item_id="wi_x")
    for a in card["content"]["actions"]:
        if a["data"]["action_id"] != "dse_plan_details":
            assert "msteams" not in a["data"], a["data"]["action_id"]


# --- o diálogo -------------------------------------------------------------

_PLANO = {
    "steps": ["Criar MetricInfo", "Adicionar o endpoint"],
    "expected_files": ["rest-adapter/src/main/java/.../MetricInfo.java"],
    "test_plan": "Um teste que afirma a presença de todo valor do enum",
    "estimated_lines": 120,
}


def test_the_dialog_is_a_task_module_teams_understands():
    env = plan_details_dialog("wi_x", _PLANO, risk="low", repo="acme/svc")
    assert env["task"]["type"] == "continue"
    card = env["task"]["value"]["card"]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert env["task"]["value"]["title"]


def test_the_dialog_shows_the_effective_risk_not_the_declared_one():
    """O risco efetivo é o que decide se aprovação é exigida; mostrar o
    declarado pelo Planner seria a tela que o humano abre PARA DECIDIR
    argumentando na direção de aprovar."""
    env = plan_details_dialog("wi_x", {**_PLANO, "risk_class": "low"},
                              risk="high", repo="acme/svc")
    texto = _texto_todo(env["task"]["value"]["card"])
    assert "high" in texto


def test_the_dialog_carries_the_plan():
    texto = _texto_todo(plan_details_dialog("wi_x", _PLANO, risk="low", repo="acme/svc"))
    assert "Criar MetricInfo" in texto
    assert "MetricInfo.java" in texto
    assert "120" in texto, "a estimativa do Planner é o tamanho previsto"


def test_an_item_without_a_plan_says_so_instead_of_opening_empty():
    texto = _texto_todo(plan_details_dialog("wi_x", None, risk=None, repo=None))
    assert "not available" in texto.lower()


def test_long_plans_are_cut_and_say_they_were_cut():
    """Diálogo que rola sem fim não é mais legível que a mensagem que ele veio
    substituir — e corte silencioso é a versão discreta do mesmo problema."""
    enorme = {"steps": [f"passo {i}" for i in range(200)]}
    texto = _texto_todo(plan_details_dialog("wi_x", enorme, risk="low", repo=None))
    assert "more" in texto.lower() or "…" in texto
