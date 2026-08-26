"""Os serviços sobem — e alguém precisa contar isso a quem escreve o código.

O Tema 1 põe um Postgres vivo em `localhost:5432` dentro do sandbox, com senha
gerada por provisão. Medido em wi_a5a395f8: nada disso chega ao Planner, ao
Coder ou ao Tester. O modelo escreve o teste de integração chutando uma
`DATABASE_URL` que ninguém definiu, o teste falha por conexão, e o laço gasta
turno pago tentando consertar código que está certo.

Os FATOS que a plataforma tem o direito de contar são os que o REPO declarou —
nome, porta e as chaves de env do serviço — mais a única variável que a
plataforma dá de si (`DSE_SERVICE_PASSWORD`). Ela não interpreta: não monta
DSN, não diz "Postgres", não escolhe driver. Quem sabe o formato da URL é o
repositório; quem sabe o endereço é a plataforma.

VALOR de senha nunca entra na instrução (ela vive no ambiente do Pod, e a
instrução viaja para o gateway do modelo, para o audit e para a PR).
"""
from __future__ import annotations

from dse_orchestrator.workflows import services_instruction_block

_SERVICES = {
    "postgres": {
        "image": "postgres:15-alpine",
        "port": 5432,
        "env": {"POSTGRES_PASSWORD": "$DSE_SERVICE_PASSWORD",
                "POSTGRES_DB": "app"},
    },
    "redis": {"image": "redis:7-alpine", "port": 6379, "env": {}},
}


def test_the_block_names_address_and_env_keys():
    bloco = services_instruction_block(_SERVICES)

    assert "localhost:5432" in bloco, "sem endereço o modelo chuta a porta"
    assert "localhost:6379" in bloco
    assert "postgres" in bloco and "redis" in bloco
    assert "POSTGRES_DB=app" in bloco, "o nome do banco é fato declarado pelo repo"
    assert "DSE_SERVICE_PASSWORD" in bloco, (
        "sem a variável da senha o teste não tem como autenticar"
    )


def test_the_password_value_never_travels_in_the_instruction():
    """A instrução vai para o gateway do modelo, para o audit e para a PR."""
    bloco = services_instruction_block({
        "postgres": {"port": 5432,
                     "env": {"POSTGRES_PASSWORD": "s3nh4-real-gerada-agora"}},
    })
    assert "s3nh4-real-gerada-agora" not in bloco
    assert "DSE_SERVICE_PASSWORD" in bloco


def test_no_services_no_block():
    """Repo que não declara nada não ganha ruído na instrução."""
    assert services_instruction_block(None) == ""
    assert services_instruction_block({}) == ""


def test_the_block_does_not_pretend_to_know_the_stack():
    """A plataforma não monta DSN nem nomeia tecnologia — ela repete o que o
    repositório declarou. Quem sabe a forma da URL é o repo."""
    bloco = services_instruction_block(_SERVICES).lower()
    assert "postgresql://" not in bloco
    assert "jdbc" not in bloco
