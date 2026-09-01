"""O que TODA superfície de conversa renderiza, e como um clique vira veredito.

Duas superfícies (Slack, Teams) e, quando houver, uma terceira: as três têm de
apontar a MESMA etapa para o mesmo status, e derivar o MESMO veredito do mesmo
clique. Nenhum dos dois fatos é sobre Block Kit ou Adaptive Card — são sobre o
ciclo de vida do work item, que mora aqui.

Por que aqui e não numa cópia por adapter: a regra do `core.hooksPath` viveu em
três call sites e foi corrigida três vezes; `_l1_infra_gates` teve três listas
divergentes de status terminal. Uma cópia do parser de veredito é pior que
essas: ele é decisão de segurança, e a que envelhecer vira aprovação silenciosa
numa superfície só.
"""
from __future__ import annotations

#: As etapas da barra, na ordem do fluxo.
STAGES = ("Plan", "Build", "Validate", "PR", "Review")

#: status → etapa. DELIBERADAMENTE incompleto: status terminal (done/failed/
#: escalated/blocked) e status desconhecido não têm etapa — uma barra "em
#: andamento" num item morto mentiria, e etapa chutada é o mesmo defeito do
#: "Diff budget 400" que era constante morta. O corpo humanizado conta o
#: desfecho; a barra só existe enquanto há próximo passo.
STAGE_FOR_STATUS = {
    "needs_clarification": "Plan",
    "awaiting_plan_approval": "Plan",
    "awaiting_repo_selection": "Plan",
    # rc.93: o membro parado na barreira de grupo espera em `queued` — a barra
    # existe e aponta Plan (`ready` idem, pelo mesmo trecho do fluxo).
    "ready": "Plan",
    "queued": "Plan",
    "implementing": "Build",
    "validating": "Validate",
    "pr_open": "PR",
    "pr_updated": "PR",
    "pr_ready": "Review",
    "review_feedback": "Review",
    # rc.130: os parques pós-PR EXISTEM na superfície. Nenhum call site postava
    # card em review_ready/ci_pending/merge_pending e o mapa não os conhecia — o
    # card congelava em "PR opened — CI is running" enquanto o item esperava um
    # humano que nunca foi chamado.
    "ci_pending": "PR",
    "review_ready": "Review",
    "merge_pending": "Review",
}

#: Onde cada superfície oferece o gesto de APROVAR. Uma fonte, dois adapters —
#: o Slack decidia por literal, o Teams por outro (`STATUS_GATE`), e nenhum
#: dos dois conhecia o parque de review: o clique de Approve era descartado
#: pelo dispatcher em `review_ready` (medido: zero itens `done` na vida).
#: Recusa no review NÃO é botão: é texto (Request changes no GitHub ou
#: `@dse fix ci` / `@dse fix preview`) — um clique de reject num card de
#: review seria lido como aprovação pelo padrão do dispatcher.
APPROVAL_STATUSES = ("awaiting_plan_approval", "review_ready")
#: Onde "How to test" faz sentido: a mensagem que carrega o preview e os
#: parques em que ele ainda está de pé.
HOW_TO_TEST_STATUSES = ("pr_ready", "review_ready", "merge_pending")

#: Toda forma de recusa que uma superfície pode emitir. Um token novo que
#: escape desta lista aprova em silêncio — o padrão do dispatcher é `approved`
#: quando o adapter não manda marcador.
REJECT_TOKENS = ("reject", "rejected", "deny", "denied", "changes", "re_plan", "replan")

#: Rota padrão de uma recusa sem rota explícita: recusa que não diz para onde
#: ir deixaria o workflow parado no mesmo lugar de onde saiu.
DEFAULT_REJECT_ROUTE = "re_plan"

#: Os identificadores dos gestos. São contrato entre o que a superfície DESENHA
#: e o que ela LÊ de volta — por isso nomeados aqui, não em cada adapter.
ACTION_APPROVE = "dse_plan_approve"
ACTION_REJECT = "dse_plan_reject"
ACTION_DETAILS = "dse_plan_details"
#: "How to test" — leitura, como o Details: as superfícies o DESVIAM antes de
#: qualquer máquina de veredito. O nome não pode conter token de recusa
#: (cadeado em test_the_surfaces_cannot_drift).
ACTION_HOW_TO_TEST = "dse_how_to_test"


def progress_stages(status: str) -> list[tuple[str, str]] | None:
    """(marcador, nome) por etapa, ou None quando não há barra a mostrar."""
    stage = STAGE_FOR_STATUS.get(status)
    if stage is None:
        return None
    idx = STAGES.index(stage)
    return [
        ("✅" if i < idx else ("⏳" if i == idx else "▫️"), nome)
        for i, nome in enumerate(STAGES)
    ]


def progress_line(status: str) -> str | None:
    """A barra como uma linha de texto — a forma que Slack e Teams usam."""
    stages = progress_stages(status)
    if stages is None:
        return None
    return "   ".join(f"{marcador} {nome}" for marcador, nome in stages)


def parse_approval_click(action_id: str, value: str) -> tuple[str, str | None]:
    """Veredito DETERMINÍSTICO de um clique, igual em toda superfície.

    O veredito não pode viver no texto: ele vira marcador
    (`approval_verdict`/`approval_route`) que o dispatcher lê. Sem marcador o
    padrão é `approved` — então um "reject" mal lido aprovaria o plano, que é
    um defeito de segurança do gate, não um bug de apresentação.

    Convenção: `action_id` ou `value` contendo qualquer `REJECT_TOKENS` =>
    recusado, com a rota depois do ':' no value (senão `re_plan`). Qualquer
    outra coisa => aprovado — fail-safe no sentido certo: recusa nunca é lida
    como aprovação, e clique ambíguo não aprova nada destrutivo sozinho, só
    segue o fluxo normal, que ainda passa pelo gate."""
    haystack = f"{action_id}|{value}".lower()
    if not any(tok in haystack for tok in REJECT_TOKENS):
        return "approved", None
    route = DEFAULT_REJECT_ROUTE
    if ":" in value:
        candidate = value.split(":", 1)[1].strip()
        if candidate:
            route = candidate
    return "rejected", route
