"""Um binding pode dizer "estes repositórios", não só "este repositório".

Até aqui `repo_bindings` respondia uma pergunta só — *qual* repo — e a chave
primária `(tenant, platform, binding_type, binding_value)` tornava isso
estrutural: um canal do Slack, um repo. Quem tem frontend e backend no mesmo
canal ficava sem saída — ou amarrava um e digitava `repo:` para o outro toda
vez (e o esquecimento manda a tarefa para o repo errado em silêncio), ou não
amarrava nada e caía no roteador por LLM, que enxerga **todos** os repos do
tenant, de todos os canais.

Foi o que aconteceu ao plugar a trilha de teste: o canal antigo, que decidia
entre 2 repositórios, passou a decidir entre 4 — e os dois novos são também
"um frontend" e "um backend". A precisão do roteador caiu sem ninguém mexer
nele.

O que estes testes fixam é uma regra só, generalizada para QUALQUER binding
(canal do Slack, projeto ou componente do Jira, workspace):

  - **uma** linha → resolve determinístico, sem modelo nenhum. É o
    comportamento de hoje e ele não pode mudar;
  - **duas ou mais** → não resolve sozinho, e o conjunto daquele binding vira
    o universo do roteador. O modelo escolhe DENTRO do canal, nunca fora dele;
  - **nenhuma** → cascata segue como sempre (tipo mais amplo, repo único do
    tenant, e por fim perguntar).

A terceira asserção é a que dá valor à mudança: o conjunto devolvido é o
recorte, então mesmo um modelo alucinando não alcança repositório de outro
canal — o clamp passa a ser por origem, não por tenant.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from ingest_gateway.repo_resolver import resolve_repo

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
SUPER_DSN = DSN.replace("dse_app:dse_app_dev_only", "dse:dse_dev_only")


@pytest.fixture()
def tenant():
    tid = f"scope-{uuid.uuid4().hex[:8]}"
    yield tid
    conn = psycopg2.connect(SUPER_DSN)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM repo_bindings WHERE tenant_id = %s", (tid,))
        cur.execute("DELETE FROM repo_profiles WHERE tenant_id = %s", (tid,))
    conn.commit()
    conn.close()


@pytest.fixture()
def db():
    conn = psycopg2.connect(DSN)
    yield conn
    conn.close()


def _bind(tenant, platform, btype, value, repo, branch="main"):
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repo_bindings "
            "(tenant_id, platform, binding_type, binding_value, repo, base_branch) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (tenant, platform, btype, value, repo, branch),
        )
    conn.commit()
    conn.close()


def test_two_repos_can_share_one_binding_value(tenant):
    """A migração. Antes disto a PK proibia a segunda linha, e o recurso era
    impossível de expressar no banco — não é detalhe de esquema, é a feature."""
    _bind(tenant, "slack", "channel", "C_TEST", "org/test-fe")
    _bind(tenant, "slack", "channel", "C_TEST", "org/test-be")

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM repo_bindings WHERE tenant_id=%s AND binding_value='C_TEST'",
            (tenant,),
        )
        n = cur.fetchone()[0]
    conn.close()
    assert n == 2, (
        "o segundo binding do mesmo canal não entrou: a chave primária ainda "
        "é (tenant, platform, binding_type, binding_value) e precisa incluir o repo"
    )


def test_a_single_binding_still_resolves_without_any_model(tenant, db):
    """PIN do comportamento de hoje. Se esta asserção cair, a mudança
    transformou toda resolução determinística numa chamada de modelo — que é
    exatamente o que a cascata existe para evitar."""
    _bind(tenant, "slack", "channel", "C_SOLO", "org/only", branch="develop")

    repo, branch, candidates = resolve_repo(
        db, tenant_id=tenant, platform="slack",
        signals={"text": "faça algo", "channel": "C_SOLO"},
    )
    assert (repo, branch) == ("org/only", "develop")
    assert candidates == [], "com uma linha só não há o que rotear"


def test_two_bindings_do_not_resolve_alone_and_hand_over_the_pair(tenant, db):
    """O coração da mudança: o canal não escolhe, mas DELIMITA."""
    _bind(tenant, "slack", "channel", "C_PAIR", "org/pair-fe")
    _bind(tenant, "slack", "channel", "C_PAIR", "org/pair-be")

    repo, branch, candidates = resolve_repo(
        db, tenant_id=tenant, platform="slack",
        signals={"text": "adicione um campo na tela", "channel": "C_PAIR"},
    )
    assert repo is None, (
        f"resolveu para {repo} sozinho — com dois candidatos a escolha é do "
        "roteador, e escolher aqui seria adivinhar"
    )
    assert sorted(candidates) == ["org/pair-be", "org/pair-fe"]


def test_the_scope_is_not_slack_specific(tenant, db):
    """Generalização: a mesma regra para o Jira, cujo binding é `project`.
    Se isto falhar, o recurso virou uma gambiarra de canal do Slack."""
    _bind(tenant, "jira", "project", "BD", "org/jira-fe")
    _bind(tenant, "jira", "project", "BD", "org/jira-be")

    repo, _, candidates = resolve_repo(
        db, tenant_id=tenant, platform="jira",
        signals={"text": "ajuste o cálculo", "project": "BD"},
    )
    assert repo is None
    assert sorted(candidates) == ["org/jira-be", "org/jira-fe"]


def test_the_most_specific_binding_wins_and_stops_the_cascade(tenant, db):
    """Precedência preservada: `component` é mais específico que `project`, e
    um component com dois repos não pode “vazar” para o conjunto do project."""
    _bind(tenant, "jira", "component", "checkout", "org/co-fe")
    _bind(tenant, "jira", "component", "checkout", "org/co-be")
    _bind(tenant, "jira", "project", "BD", "org/outro")

    repo, _, candidates = resolve_repo(
        db, tenant_id=tenant, platform="jira",
        signals={"text": "x", "component": "checkout", "project": "BD"},
    )
    assert repo is None
    assert sorted(candidates) == ["org/co-be", "org/co-fe"], (
        "o conjunto veio do project: um binding mais específico com múltiplos "
        "repos tem de encerrar a cascata, não continuar para o mais amplo"
    )


def test_no_binding_at_all_behaves_exactly_as_before(tenant, db):
    """PIN de não-regressão: sem binding, a cascata continua até perguntar."""
    from ingest_gateway.repo_resolver import resolve_repo as rr

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repo_profiles (tenant_id, repo, role) VALUES (%s,%s,''),(%s,%s,'')",
            (tenant, "org/a", tenant, "org/b"),
        )
    conn.commit()
    conn.close()

    repo, branch, candidates = rr(
        db, tenant_id=tenant, platform="slack",
        signals={"text": "algo", "channel": "C_SEM_BINDING"},
    )
    assert (repo, branch) == (None, None)
    assert candidates == [], (
        "sem binding não há recorte — o roteador segue vendo o tenant inteiro, "
        "como sempre viu"
    )
