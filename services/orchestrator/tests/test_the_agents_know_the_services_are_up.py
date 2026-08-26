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

import uuid

import pytest
from temporalio.worker import Worker

from dse_orchestrator import policy
from dse_orchestrator.workflows import (
    WorkItemLifecycleWorkflow,
    services_instruction_block,
)

from conftest import new_work_item_id, wait_for_status
from fakes import FakeControlPlane

from test_plan_approval_timeout import _Ledger, _gate_input, build_db_free_activities

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


@pytest.mark.asyncio
async def test_the_coder_really_receives_it_end_to_end(time_skipping_env):
    """A função pura não basta: o que importa é o que o Coder LÊ."""
    state = FakeControlPlane(
        plan_expected_files=["apps/api/src/health.ts"],
        repo_manifest_services={
            "postgres": {"image": "postgres:15-alpine", "port": 5432,
                         "env": {"POSTGRES_DB": "app",
                                 "POSTGRES_PASSWORD": "$DSE_SERVICE_PASSWORD"}},
        },
    )
    ledger = _Ledger()
    work_item_id = new_work_item_id("svcinstr")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id), id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.terminate()

    assert state.coder_instructions, "o Coder nunca rodou"
    instrucao = state.coder_instructions[0]
    assert "localhost:5432" in instrucao, (
        "o Coder escreveu código sem saber que existe um banco vivo ao lado"
    )
    assert "DSE_SERVICE_PASSWORD" in instrucao
    assert "POSTGRES_DB=app" in instrucao


@pytest.mark.asyncio
async def test_a_repo_without_services_gets_no_extra_line(time_skipping_env):
    state = FakeControlPlane(plan_expected_files=["apps/api/src/health.ts"])
    ledger = _Ledger()
    work_item_id = new_work_item_id("nosvc")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id), id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.terminate()

    assert state.coder_instructions
    assert "localhost:" not in state.coder_instructions[0]


def test_the_block_does_not_pretend_to_know_the_stack():
    """A plataforma não monta DSN nem nomeia tecnologia — ela repete o que o
    repositório declarou. Quem sabe a forma da URL é o repo."""
    bloco = services_instruction_block(_SERVICES).lower()
    assert "postgresql://" not in bloco
    assert "jdbc" not in bloco
