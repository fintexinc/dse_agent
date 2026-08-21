"""Quem está no canal pode falar com o DSE. Ponto.

O DSE tinha uma allowlist de DIREÇÃO por tenant (`tenant_steering_allowlist`,
mais papéis do console e aprovadores designados do bundle): quem não estivesse
nela via o próprio comentário virar `steering_rejected_unauthorized` e sumir —
a tarefa seguia como se ninguém tivesse falado.

Decisão do operador (2026-08-21): isso sai, no Slack e no Teams. O convite ao
canal É a autorização. Quem tem acesso ao canal já lê tudo que o DSE escreve
ali — plano, arquivos tocados, veredito dos gates — e a assimetria de poder
ler e não poder responder custava mais do que protegia: toda superfície nova
recriava o problema, porque cada plataforma dá ao mesmo humano uma identidade
diferente, e nenhuma delas nasce na lista.

**O que NÃO muda:** a aprovação de plano. Ela nunca passou por aqui
(`_STEERING_GATED_KINDS` cobre steering/review_comment/clarification_answer, e
`approval` tem cascata própria: CODEOWNERS → aprovadores designados). Quem pode
aprovar continua sendo decidido lá, e este arquivo pina isso — porque a
diferença entre "responder" e "aprovar" é a única que ainda importa.
"""
from __future__ import annotations

import json
import uuid

import psycopg2
import pytest
from dse_contracts import Actor, ConversationEvent, EventKind, Platform

from ingest_gateway.correlate import correlate

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _semeia(tenant_id: str, source_ref: dict) -> str:
    work_item_id = f"wi_test_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, "
            "status, idempotency_key) VALUES (%s,%s,'slack',%s::jsonb,"
            "'usr_original_requester','implementing',%s)",
            (work_item_id, tenant_id, json.dumps(source_ref), f"idem_{work_item_id}"),
        )
    conn.commit()
    conn.close()
    return work_item_id


def _evento(kind: EventKind, source_ref: dict, actor_id: str) -> ConversationEvent:
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=f"{source_ref['channel']}:{source_ref['thread_ts']}",
        message_id=f"m-{actor_id}-{kind.value}-{uuid.uuid4().hex[:6]}",
        kind=kind,
        source_ref=source_ref,
        actor=Actor(platform_user_id=actor_id, display_name=actor_id),
        content_snapshot="mude o escopo",
        signature_verified=True,
    )


@pytest.mark.parametrize("kind", [
    EventKind.steering,
    EventKind.review_comment,
    EventKind.clarification_answer,
])
def test_a_stranger_in_the_channel_now_steers(db_conn, tenant_id, kind):
    """Antes: `unauthorized`, comentário engolido em silêncio para quem não
    fosse o requester nem estivesse na lista."""
    ref = {"channel": "C1", "thread_ts": f"{uuid.uuid4().int % 10**9}.1"}
    work_item_id = _semeia(tenant_id, ref)

    resultado = correlate(db_conn, tenant_id=tenant_id,
                          event=_evento(kind, ref, "U_ESTRANHO"),
                          requester_principal="usr_estranho")

    assert resultado.kind == "signal"
    assert resultado.work_item_id == work_item_id


def test_the_refusal_verb_is_gone_from_the_vocabulary():
    """`unauthorized` era um dos três resultados possíveis da correlação, e
    todo adapter tinha um ramo para ele. Deixar o verbo vivo sem quem o
    produza é o tipo de resto que volta a ser lido como regra."""
    from ingest_gateway import correlate as mod

    assert not hasattr(mod, "_STEERING_GATED_KINDS")
    assert "unauthorized" not in getattr(mod, "CorrelationKind").__args__


def test_plan_approval_still_has_its_own_gate():
    """A cascata de aprovadores (CODEOWNERS → designated approvers) não é a
    allowlist de direção e não sai com ela: responder é de todos, aprovar não."""
    from dse_orchestrator import policy

    # A cascata vive aqui (CODEOWNERS → designated approvers do access bundle),
    # numa activity própria — nunca no gate de direção.
    assert hasattr(policy, "parse_codeowners_owners")
    assert hasattr(policy, "requires_plan_approval")
