"""Duas superfícies, uma verdade: a etapa e o veredito.

O Slack ganhou barra de etapas e botões de aprovação na rc.90; o Teams nasceu
depois e ficou com texto puro. As consequências não são simétricas:

  - a barra ausente é feia, e
  - a APROVAÇÃO ausente é um beco: uma tarefa de risco `high` pelo Teams para
    em `awaiting_plan_approval` e não existe gesto na superfície que responda.
    Medido em 2026-08-21: `event_kind` do Teams só produz `task_request` e
    `clarification_answer`; nenhuma rota de `Action.Submit`.

O conserto óbvio — copiar as duas funções do Slack para o adapter do Teams —
é o defeito que este repositório já pagou três vezes (a regra do
`core.hooksPath` viveu em três call sites; `_l1_infra_gates` teve três listas
divergentes de status terminal). Copiar o parser de VEREDITO é pior ainda:
ele é uma decisão de segurança (`reject` jamais pode ser lido como `approve`),
e uma cópia que envelhece vira aprovação silenciosa numa superfície só.

Então os dois fatos moram em `dse_contracts.surface`, e este arquivo é o
cadeado: para CADA status conhecido, as duas superfícies apontam a mesma etapa.
"""
from __future__ import annotations

import pytest

from dse_contracts.surface import (
    STAGES,
    STAGE_FOR_STATUS,
    parse_approval_click,
    progress_line,
)


def test_the_stage_map_only_names_stages_that_exist():
    for status, stage in STAGE_FOR_STATUS.items():
        assert stage in STAGES, f"{status} aponta uma etapa inexistente: {stage}"


def test_an_in_flight_status_renders_the_bar_with_exactly_one_hourglass():
    linha = progress_line("implementing")
    assert linha is not None
    assert linha.count("⏳") == 1, "a etapa ATUAL é uma só"
    assert all(nome in linha for nome in STAGES)


def test_a_terminal_status_has_no_bar():
    """Barra 'em andamento' num item morto mentiria — e etapa chutada é o mesmo
    defeito que o rodapé de 400 linhas fixas."""
    for terminal in ("done", "failed", "escalated", "blocked", "merged"):
        assert progress_line(terminal) is None, terminal


def test_an_unknown_status_never_invents_a_stage():
    assert progress_line("um_status_que_nao_existe") is None


# --- o parser de veredito: uma decisão de segurança, uma implementação ------

@pytest.mark.parametrize("action_id,value,esperado", [
    ("dse_plan_approve", "approve", ("approved", None)),
    ("dse_plan_reject", "reject:re_plan", ("rejected", "re_plan")),
    ("dse_plan_reject", "reject:re_scope", ("rejected", "re_scope")),
    ("dse_plan_reject", "reject", ("rejected", "re_plan")),
    ("qualquer_coisa", "deny", ("rejected", "re_plan")),
    ("botao_de_changes", "", ("rejected", "re_plan")),
    ("dse_plan_details", "details", ("approved", None)),
])
def test_the_verdict_is_derived_the_same_way_everywhere(action_id, value, esperado):
    assert parse_approval_click(action_id, value) == esperado


def test_a_rejection_is_never_read_as_an_approval():
    """O padrão do dispatcher é `approved` quando o adapter não manda marcador.
    Toda forma de recusa que a superfície pode emitir tem de produzir
    `rejected` — um token novo que escape daqui aprova em silêncio."""
    for forma in ("reject", "rejected", "deny", "denied", "changes", "re_plan", "replan"):
        verdict, route = parse_approval_click(f"dse_plan_{forma}", forma)
        assert verdict == "rejected", forma
        assert route, "recusa sem rota deixa o workflow sem para onde ir"


def test_how_to_test_is_one_shared_action_and_can_never_read_as_a_rejection():
    """O id do How to test vive AQUI, como os outros três — e não pode conter
    token de recusa: um desvio perdido cairia no fallthrough, e "leu o guia"
    virando "rejeitou o plano" seria pior que o bug que o Details já matou."""
    from dse_contracts.surface import ACTION_HOW_TO_TEST, parse_approval_click

    assert ACTION_HOW_TO_TEST == "dse_how_to_test"
    verdict, _ = parse_approval_click(ACTION_HOW_TO_TEST, "how_to_test")
    assert verdict == "approved", (
        "o id contém um token de recusa — um clique de leitura rejeitaria o plano"
    )


def test_the_slack_adapter_reads_from_the_shared_source():
    """Se o Slack voltar a ter cópia própria, esta asserção cai — e é ela que
    impede a divergência de reaparecer pela porta de trás."""
    from adapter_slack.backend import _progress_line
    from adapter_slack.events import parse_slack_approval

    for status in list(STAGE_FOR_STATUS) + ["done", "inexistente"]:
        assert _progress_line(status) == progress_line(status), status
    assert parse_slack_approval("dse_plan_reject", "reject:re_scope") == ("rejected", "re_scope")


# ---------------------------------------------------------------------------
# rc.130 — o parque pós-PR existe na superfície.
#
# Medido: nenhum call site postava card em `review_ready`/`ci_pending`/
# `merge_pending`, e `STAGE_FOR_STATUS` não os conhecia — o card congelava em
# "PR opened — CI is running" enquanto o item esperava um humano que nunca foi
# chamado. Os dois adapters decidiam por literal próprio onde mostrar Approve
# e How to test; agora a fonte é uma.
# ---------------------------------------------------------------------------

def test_the_post_pr_parks_have_a_stage():
    assert STAGE_FOR_STATUS["ci_pending"] == "PR"
    assert STAGE_FOR_STATUS["review_ready"] == "Review"
    assert STAGE_FOR_STATUS["merge_pending"] == "Review"


def test_the_gestures_have_one_shared_source():
    from dse_contracts.surface import APPROVAL_STATUSES, HOW_TO_TEST_STATUSES

    assert set(APPROVAL_STATUSES) == {"awaiting_plan_approval", "review_ready"}
    assert set(HOW_TO_TEST_STATUSES) == {"pr_ready", "review_ready", "merge_pending"}


def test_the_slack_review_card_offers_approve_and_how_to_test():
    from adapter_slack.backend import status_blocks

    blocks = status_blocks("👀 Ready for your review", status="review_ready")
    acoes = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert "dse_plan_approve" in acoes and "dse_how_to_test" in acoes
    assert "dse_plan_reject" not in acoes, "recusa no review é texto, não botão"
