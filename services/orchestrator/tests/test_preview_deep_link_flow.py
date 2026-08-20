"""O workflow resolve o caminho fundo ANTES do trigger, e todas as superfícies
compõem — menos o demo evidence, que continua recebendo a URL crua.

Harness db-free de `test_plan_approval_timeout` (fakes compartilhados em
`fakes.py` — a lição do NotFoundError retry storm, paga duas vezes).
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator import policy
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import new_work_item_id, wait_for_status
from fakes import FakeControlPlane

from test_plan_approval_timeout import _Ledger, _gate_input, build_db_free_activities


@pytest.fixture(autouse=True)
def _require_postgres():
    yield


@pytest.fixture(autouse=True)
def _reset_codeowners():
    policy.set_codeowners_reader(None)
    yield
    policy.set_codeowners_reader(None)


async def _run_to_done(time_skipping_env, state: FakeControlPlane, ledger: _Ledger):
    work_item_id = new_work_item_id("deeplink")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    policy.set_codeowners_reader(lambda tenant_id, repo: "* @alice")
    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow],
                      activities=build_db_free_activities(ledger, state)):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _gate_input(work_item_id), id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        return await handle.result()


@pytest.mark.asyncio
async def test_the_slack_link_lands_on_the_change_with_the_note(time_skipping_env):
    state = FakeControlPlane(
        plan_expected_files=["src/app/page.ts"],
        deep_link_result={"path": "/api/v1/metrics",
                          "note": "the new metrics endpoint", "cost_usd": 0.002},
    )
    ledger = _Ledger()
    result = await _run_to_done(time_skipping_env, state, ledger)
    assert result.status == WorkItemStatus.done.value

    corpo = next((b for s, b in ledger.comments if "Preview" in b and "ready" in b), None)
    assert corpo is not None, f"nenhuma mensagem de preview: {ledger.comments}"
    assert "/api/v1/metrics" in corpo, (
        "o link do Slack não caiu na mudança — continua a raiz que devolve SYS-002"
    )
    assert "the new metrics endpoint" in corpo, "a nota de 1 linha não veio"
    assert "resolve_preview_deep_link" in state.calls_log


@pytest.mark.asyncio
async def test_demo_evidence_still_receives_the_bare_url(time_skipping_env):
    """A armadilha que o campo separado existe para evitar: base_url do
    Playwright com path re-rootaria toda navegação do demo."""
    state = FakeControlPlane(
        plan_expected_files=["src/app/page.ts"],
        deep_link_result={"path": "/api/v1/metrics", "note": "x", "cost_usd": 0.0},
    )
    ledger = _Ledger()
    await _run_to_done(time_skipping_env, state, ledger)

    base = (state.last_demo_payload or {}).get("base_url") or ""
    assert base and "/api/v1/metrics" not in base, (
        f"o deep_path vazou para o baseURL do demo: {base!r}"
    )


@pytest.mark.asyncio
async def test_a_failed_resolver_leaves_todays_message_untouched(time_skipping_env):
    state = FakeControlPlane(
        plan_expected_files=["src/app/page.ts"],
        deep_link_raise=True,
    )
    ledger = _Ledger()
    result = await _run_to_done(time_skipping_env, state, ledger)
    assert result.status == WorkItemStatus.done.value

    corpo = next((b for s, b in ledger.comments if "Preview" in b and "ready" in b), None)
    assert corpo is not None, "resolvedor quebrado não pode calar o link do preview"
    assert "→" not in corpo, "sem caminho não há nota — a mensagem é a de hoje"
