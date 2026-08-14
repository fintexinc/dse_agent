"""O item do Teams nasce com repositório, e a recusa não vira 500.

Dois buracos medidos na ativação (2026-08-14), ambos por comparação direta com
o Slack:

1. **`resolve_repo` nunca é chamado no fluxo do Teams.** Slack chama em
   `adapter_slack/app.py:408`, Jira em `adapter_jira/ingest.py:78`. Sem isso o
   `repo_bindings` do canal é decorativo: `work_items.repo` nasce NULL,
   `repo_candidates` vazio, e o item cai no roteador LLM sobre o catálogo do
   tenant INTEIRO — um modelo adivinha o repositório que uma linha de banco já
   respondia.

2. **`NonTaskAdmissionRefused` não é capturada.** Toda mensagem sem menção vira
   `clarification_answer` (`events.py:107`); se a correlação não achar item
   ativo, `gateway.py:101` levanta e o FastAPI devolve **500** — um erro de
   servidor para o que é, na verdade, "não achei a tarefa desta conversa". O
   Slack trata em `app.py:427`.

Postgres real, como os vizinhos.
"""
from __future__ import annotations

import uuid

import psycopg2
from fastapi.testclient import TestClient

from adapter_teams.app import app

from .helpers import activity_body, teams_auth_header

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
_CONVERSATION = "19:channel_abc@thread.tacv2"


def _tenant_with_binding(repo: str) -> str:
    """Tenant novo + binding do tenant (AAD guid → tenant) + binding de repo
    do canal, que é exatamente o que um onboarding de Teams faria."""
    tenant = f"t_teams_{uuid.uuid4().hex[:10]}"
    aad = f"aad-{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tenant_platform_bindings (platform, binding_key, tenant_id) "
                    "VALUES ('teams', %s, %s) ON CONFLICT DO NOTHING",
                    (aad, tenant),
                )
                cur.execute(
                    "INSERT INTO repo_bindings "
                    "(tenant_id, platform, binding_type, binding_value, repo, base_branch) "
                    "VALUES (%s, 'teams', 'channel', %s, %s, 'main') "
                    "ON CONFLICT DO NOTHING",
                    (tenant, _CONVERSATION, repo),
                )
    finally:
        conn.close()
    return aad


def _repo_of(work_item_id: str) -> tuple[str | None, list]:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo, COALESCE(repo_candidates, '{}') FROM work_items WHERE id = %s",
                (work_item_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def test_a_mention_lands_on_the_repo_bound_to_the_channel(teams_secret_b64):
    repo = "fintexinc/bmo-fee-calculator-be-dse"
    aad = _tenant_with_binding(repo)
    body = activity_body(text="<at>DSE Bot</at> retire a payout level",
                         tenant_guid=aad, conversation_id=_CONVERSATION)
    resp = client.post("/teams/messages", content=body,
                       headers={"Authorization": teams_auth_header(teams_secret_b64, body)})

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["path"] == "new_task", payload
    got_repo, candidates = _repo_of(payload["work_item_id"])
    assert got_repo == repo, (
        "o binding do canal foi ignorado: o item nasceu sem repo e vai depender "
        f"do roteador LLM sobre o catálogo do tenant inteiro (repo={got_repo!r})"
    )
    # Binding único resolve DETERMINISTICAMENTE: a lista de candidatos existe
    # para o caso ambíguo (canal com FE e BE), e ficar vazia aqui é a prova de
    # que nenhum modelo foi consultado.
    assert list(candidates) == [], (
        f"resolução determinística não deveria deixar candidatos: {candidates}"
    )


def test_a_reply_with_no_task_is_refused_not_a_server_error(teams_secret_b64):
    """Mensagem sem menção, em conversa que a correlação desconhece: é recusa
    orientada, não 500. O 500 é o que faz o Bot Framework reentregar em loop."""
    aad = _tenant_with_binding("fintexinc/bmo-fee-calculator-fe-dse")
    body = activity_body(text="mas e aí, saiu?", tenant_guid=aad,
                         conversation_id=f"19:{uuid.uuid4().hex}@thread.tacv2")
    resp = client.post("/teams/messages", content=body,
                       headers={"Authorization": teams_auth_header(teams_secret_b64, body)})

    assert resp.status_code == 200, (
        f"recusa virou erro de servidor ({resp.status_code}) — o connector "
        "reentrega 500 e a mensagem volta em loop"
    )
    assert resp.json()["path"] == "refused_non_task", resp.json()
