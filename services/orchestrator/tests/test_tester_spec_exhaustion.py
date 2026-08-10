"""Beco 1 do mapa de alcançabilidade — `(tester, spec_tester, asserção)`,
medido DUAS vezes em produção antes disto existir:

  - wi_5eecf486 (run 6): a spec própria montou o MockStore sem
    `pagination.pageSize`; TypeError no detectChanges, três rodadas idênticas,
    morte no teto. Um humano resolveria em trinta segundos.
  - wi_32eb136f (rc.45): a spec própria afirma `severity === 'warning'`, que a
    union do p-tag proíbe — satisfazer a spec quebra o build e vice-versa.
    Morto à mão a caminho do teto.

A regra NÃO é um classificador (decidir se a asserção está errada é a
indecidibilidade da porta 2). É reconhecimento de EXAUSTÃO: fingerprint de
não-convergência apontando exclusivamente para specs do próprio Tester com
veredito presente = nenhum ator autorizado pode agir (Coder revertido, porta 5
não re-autora com veredito, porta 1 exclui por posse) — então parqueia para
humano com dossiê, na primitiva da porta 1, em vez de morrer mudo. Vermelho
antes do fix.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest
from temporalio.worker import Worker

from dse_contracts.activities import L1Finding
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import DSN, insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

_BADGE_SPEC = "src/app/components/homepage/components/report-status-badge/report-status-badge.component.spec.ts"
_DSE_SPEC = "src/app/components/homepage/components/dashboard-list/dashboard-list.component-dse.spec.ts"
_CLIENT_SPEC = "src/app/components/homepage/homepage.component.spec.ts"

#: wi_32eb136f, verbatim (abreviado): asserção com veredito, esperado vs recebido.
_WARNING_DETAIL = f"""summary: 12 errors
--- the 2 line(s) this gate counted ---
FAIL {_BADGE_SPEC}
FAIL {_DSE_SPEC}
--- raw output (tail) ---
  ● ReportStatusBadgeComponent › severity › should return "warning" severity when status is in-progress

    expect(received).toBe(expected) // Object.is equality

    Expected: "warning"
    Received: "warn"

      at src/app/components/homepage/components/report-status-badge/report-status-badge.component.spec.ts:66:34
"""

#: wi_5eecf486, verbatim (abreviado): a spec executa e morre no render.
_PAGESIZE_DETAIL = f"""summary: 403 errors
--- the 2 line(s) this gate counted ---
FAIL {_DSE_SPEC} (7.1 s)
FAIL {_DSE_SPEC} (7.1 s)
--- raw output (tail) ---
    TypeError: Cannot read properties of undefined (reading 'pageSize')

      at DashboardListComponent_Conditional_4_Template (ng:/DashboardListComponent.js:319:25)
      at executeTemplate (node_modules/@angular/core/fesm2022/core.mjs:12429:9)
"""

_MIXED_DETAIL = f"""summary: 5 errors
--- the 2 line(s) this gate counted ---
FAIL {_DSE_SPEC}
FAIL {_CLIENT_SPEC}
--- raw output (tail) ---
    expect(received).toBe(expected)
"""


def _audit_details(work_item_id: str, action: str) -> list[dict]:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT details FROM audit_log WHERE work_item_id = %s AND action = %s ORDER BY id",
                (work_item_id, action),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# O reconhecedor puro.
# ---------------------------------------------------------------------------


def test_recognizer_accepts_both_measured_scenarios():
    from dse_orchestrator.workflows import exclusively_tester_spec_failures

    warning = exclusively_tester_spec_failures(
        ["test"], test_detail=_WARNING_DETAIL, tester_owned=[_BADGE_SPEC, _DSE_SPEC],
    )
    assert warning == [_BADGE_SPEC, _DSE_SPEC]

    pagesize = exclusively_tester_spec_failures(
        ["test"], test_detail=_PAGESIZE_DETAIL, tester_owned=[_DSE_SPEC],
    )
    assert pagesize == [_DSE_SPEC]


def test_recognizer_refuses_anything_not_exclusively_tester_owned():
    from dse_orchestrator.workflows import exclusively_tester_spec_failures

    # spec do cliente na lista FAIL -> não é o beco 1
    assert exclusively_tester_spec_failures(
        ["test"], test_detail=_MIXED_DETAIL, tester_owned=[_DSE_SPEC],
    ) == []
    # outro gate reprovando junto -> o Coder ainda tem o que fazer
    assert exclusively_tester_spec_failures(
        ["test", "build"], test_detail=_WARNING_DETAIL, tester_owned=[_BADGE_SPEC, _DSE_SPEC],
    ) == []
    # sem veredito (carga) -> território da porta 5, nunca deste parque
    zero = f"FAIL {_DSE_SPEC}\n  ● Test suite failed to run\n    Cannot find module 'x'\n"
    assert exclusively_tester_spec_failures(
        ["test"], test_detail=zero, tester_owned=[_DSE_SPEC],
    ) == []


# ---------------------------------------------------------------------------
# Control-plane: parquear, não morrer no teto.
# ---------------------------------------------------------------------------


async def _start(state: FakeControlPlane, work_item_id: str, env):
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)
    worker = Worker(
        env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    )
    wf_input = WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit",
    )
    handle = await env.client.start_workflow(
        WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
    return worker, handle


async def _drive_to_park_and_finish(env, state, work_item_id, *, expect_expected_received):
    worker, handle = await _start(state, work_item_id, env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        assert state.coder_turn_calls == 2, "parqueia na 2ª falha idêntica, sem comprar a 3ª"

        detected = _audit_details(work_item_id, "spec_conflict_detected")
        assert detected, "o dossiê é auditável"
        d = detected[0]
        assert d.get("reason") == "tester_spec_exhaustion"
        assert d["specs"], "quais specs"
        assert "expect(" in d["assertions"] or "TypeError" in d["assertions"], "quais asserções"
        if expect_expected_received:
            joined = " ".join(d.get("expected_vs_received") or [])
            assert "warning" in joined and "warn" in joined, "esperado vs recebido"
        assert d["diff_files"], "o diff do Coder"

        await handle.signal(
            "spec_conflict_resolution", {"verdict": "retry", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "coder_retry_cap_exhausted" not in actions, "parquear, não morrer no teto"


@pytest.mark.asyncio
async def test_the_warning_scenario_parks_instead_of_dying(time_skipping_env):
    work_item_id = new_work_item_id("exh-warn")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_WARNING_DETAIL,
        coder_files_changed=["src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"],
        tester_test_files=[_BADGE_SPEC, _DSE_SPEC],
    )
    await _drive_to_park_and_finish(
        time_skipping_env, state, work_item_id, expect_expected_received=True,
    )


@pytest.mark.asyncio
async def test_the_pagesize_scenario_parks_instead_of_dying(time_skipping_env):
    work_item_id = new_work_item_id("exh-page")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_PAGESIZE_DETAIL,
        coder_files_changed=["src/app/components/homepage/components/dashboard-list/dashboard-list.component.ts"],
        tester_test_files=[_DSE_SPEC],
    )
    await _drive_to_park_and_finish(
        time_skipping_env, state, work_item_id, expect_expected_received=False,
    )


#: wi_c9c7b200, verbatim (abreviado): a mesma spec própria, rodadas 2 e 4 —
#: com lint+build no meio. A linha FAIL duplicada é como o gate a emitiu.
_ALTERNATING_BADGE_DETAIL = f"""summary: 403 errors
--- the 2 line(s) this gate counted ---
FAIL {_BADGE_SPEC}
FAIL {_BADGE_SPEC}
--- raw output (tail) ---
  ● ReportStatusBadgeComponent › should show in-progress for various non-finished pages

    expect(received).toBe(expected) // Object.is equality

      at src/app/components/homepage/components/report-status-badge/report-status-badge.component.spec.ts:76:57
"""


@pytest.mark.asyncio
async def test_the_alternating_sequence_parks_on_the_second_spec_failure(time_skipping_env):
    """A sequência exata que matou o wi_c9c7b200 no teto (2026-08-07): a spec
    própria reprova, o Coder a persegue e quebra lint+build, conserta ambos, e
    a spec reprova DE NOVO — idêntica. O fingerprint consecutivo resetou na
    rodada do meio e o beco 1 nunca disparou. O gatilho certo é memória POR
    SPEC: a mesma spec própria reprovando com veredito em QUALQUER rodada
    posterior parqueia, independente do que falhou entre elas."""
    work_item_id = new_work_item_id("exh-alt")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_findings_by_call=[
            [L1Finding(check="test", passed=False, detail=_ALTERNATING_BADGE_DETAIL)],
            [L1Finding(check="lint", passed=False, detail="ESLint: 2 problems (2 errors)"),
             L1Finding(check="build", passed=False, detail="NG8001: 'p-tag' is not a known element")],
            [L1Finding(check="test", passed=False, detail=_ALTERNATING_BADGE_DETAIL)],
        ],
        coder_files_changed=["src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"],
        tester_test_files=[_BADGE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        assert state.coder_turn_calls == 3, (
            "parqueia na SEGUNDA reprovação da spec — a rodada de lint+build no "
            "meio não apaga a memória"
        )
        d = _audit_details(work_item_id, "spec_conflict_detected")[0]
        assert d.get("reason") == "tester_spec_exhaustion"
        assert d["specs"] == [_BADGE_SPEC]
        await handle.signal(
            "spec_conflict_resolution", {"verdict": "reauthor", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value
    assert "coder_retry_cap_exhausted" not in read_audit_actions(work_item_id)


#: wi_0d95384f, rodada 2 (verbatim, abreviado): a reprovação que armou a pinça
#: — com o par Expected/Received que o dossiê tem que carregar.
_NOOP_PINCER_DETAIL = f"""summary: 403 errors
--- the 1 line(s) this gate counted ---
FAIL {_BADGE_SPEC}
--- raw output (tail) ---
  ● ReportStatusBadgeComponent › should show in-progress for various non-finished pages

    expect(received).toBe(expected) // Object.is equality

    Expected: "in-progress"
    Received: "in_progress"

      at src/app/components/homepage/components/report-status-badge/report-status-badge.component.spec.ts:76:57
"""


@pytest.mark.asyncio
async def test_the_honest_coder_noop_parks_with_the_full_dossier(time_skipping_env):
    """A sequência exata do wi_0d95384f (2026-08-08, escalado sem dossiê):
    build → test(badge, spec própria, veredito) → no-op → no-op. O Coder
    honesto declara "não tenho jogada" duas vezes; isso é evidência de
    exaustão MAIS forte que uma segunda rodada vermelha de L1 — e já paga.
    O ramo do no-op parqueia com o MESMO dossiê do parque via L1 (specs,
    asserções, Expected/Received, diff acumulado): dossiê pobre viraria um
    reauthor às cegas. A escalada genérica fica para no-op sem pinça armada."""
    work_item_id = new_work_item_id("exh-noop")
    insert_work_item(work_item_id)
    component = "src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_findings_by_call=[
            [L1Finding(check="build", passed=False, detail="NG8001: 'p-tag' is not a known element")],
            [L1Finding(check="test", passed=False, detail=_NOOP_PINCER_DETAIL)],
        ],
        coder_files_changed_by_turn=[
            [component],  # implementação
            [component],  # conserto do build
            [],           # no-op 1: nada a mudar — o código está certo
            [],           # no-op 2: o ator declara exaustão
        ],
        tester_test_files=[_BADGE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        assert state.coder_turn_calls == 4, "parqueia no segundo no-op, sem rodada extra de L1"
        actions = read_audit_actions(work_item_id)
        assert "escalated" not in actions, "parque com dossiê, não escalada muda"

        d = _audit_details(work_item_id, "spec_conflict_detected")[0]
        assert d.get("reason") == "tester_spec_exhaustion"
        assert d["specs"] == [_BADGE_SPEC], "quais specs"
        assert "expect(received).toBe(expected)" in d["assertions"], "quais asserções"
        joined = " ".join(d.get("expected_vs_received") or [])
        assert "in-progress" in joined and "in_progress" in joined, "esperado vs recebido"
        assert component in d["diff_files"], "o diff acumulado do Coder"

        await handle.signal(
            "spec_conflict_resolution", {"verdict": "reauthor", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        assert state.tester_reauthor_orders[-1] == [_BADGE_SPEC]
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_a_noop_without_an_armed_pincer_still_escalates(time_skipping_env):
    """O guard: dois no-ops contra uma falha que NÃO é pinça (lint — o Coder
    tinha jogada e não jogou) seguem escalando como hoje. O parque lateral
    existe só quando a última falha de L1 foi exclusivamente spec própria com
    veredito; evidência velha não parqueia ninguém."""
    work_item_id = new_work_item_id("exh-noop-guard")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_findings_by_call=[
            [L1Finding(check="lint", passed=False, detail="ESLint: 2 problems (2 errors)")],
        ],
        coder_files_changed_by_turn=[
            ["src/app/app.component.ts"],
            [],
            [],
        ],
        tester_test_files=[_BADGE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"escalated"})
        result = await handle.result()
    assert result.status == WorkItemStatus.escalated.value
    assert "spec_conflict_detected" not in read_audit_actions(work_item_id)


@pytest.mark.asyncio
async def test_an_armed_memory_parks_even_at_the_retry_cap(time_skipping_env):
    """wi_6f00bf0a, morte 2: a 4ª falha de L1 (test exclusivo, spec na memória)
    bateu no cap check ANTES do parque — o teto executou primeiro exatamente o
    item que o parque existia para salvar. O parque preempta o teto: parquear
    É o substituto do teto quando a exaustão está reconhecida."""
    work_item_id = new_work_item_id("exh-cap")
    insert_work_item(work_item_id)
    component = "src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_findings_by_call=[
            [L1Finding(check="build", passed=False, detail="NG8001: unknown element")],
            [L1Finding(check="build", passed=False, detail="NG8001: unknown element")],
            [L1Finding(check="test", passed=False, detail=_NOOP_PINCER_DETAIL)],
            [L1Finding(check="test", passed=False, detail=_NOOP_PINCER_DETAIL)],
        ],
        coder_files_changed=[component],
        tester_test_files=[_BADGE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        assert state.coder_turn_calls == 4, "a 4ª falha parqueia; o teto não a alcança"
        actions = read_audit_actions(work_item_id)
        assert "coder_retry_cap_exhausted" not in actions
        d = _audit_details(work_item_id, "spec_conflict_detected")[0]
        assert d.get("reason") == "tester_spec_exhaustion"
        await handle.signal(
            "spec_conflict_resolution", {"verdict": "reauthor", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_an_error_gate_neither_blocks_the_park_nor_disarms_the_pincer(time_skipping_env):
    """wi_6f00bf0a, agravante medido: a rodada final veio com lint ERROR —
    artefato de infra (documentado na autópsia do wi_32eb136f), não veredito.
    ERROR não entra no teste de exclusividade e não desarma a pinça; FAIL real
    continua contando."""
    work_item_id = new_work_item_id("exh-err")
    insert_work_item(work_item_id)
    from dse_contracts.activities import GateStatus
    err = L1Finding(check="lint", passed=False, status=GateStatus.ERROR,
                    detail="kill artifact — no verdict taken")
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_findings_by_call=[
            [err, L1Finding(check="test", passed=False, detail=_NOOP_PINCER_DETAIL)],
            [L1Finding(check="lint", passed=False, status=GateStatus.ERROR,
                       detail="kill artifact — no verdict taken"),
             L1Finding(check="test", passed=False, detail=_NOOP_PINCER_DETAIL)],
        ],
        coder_files_changed=["src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"],
        tester_test_files=[_BADGE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        assert state.coder_turn_calls == 2, "parqueia na 2ª reprovação da spec, ERROR ignorado"
        d = _audit_details(work_item_id, "spec_conflict_detected")[0]
        assert d.get("reason") == "tester_spec_exhaustion"
        assert d["specs"] == [_BADGE_SPEC]
        await handle.signal(
            "spec_conflict_resolution", {"verdict": "reauthor", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_the_verdict_comment_reaches_the_reauthor_prompt(time_skipping_env):
    """A instrução do humano viaja COM a ordem (wi_53c820f1: a sonda provou a
    asserção de display computado insatisfazível no JSDOM, e a regra 'estilo
    por classe/atributo, nunca display computado' precisa chegar ao modelo —
    um veredito sem canal de instrução obriga o humano a torcer)."""
    work_item_id = new_work_item_id("exh-comment")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_WARNING_DETAIL,
        coder_files_changed=["src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"],
        tester_test_files=[_BADGE_SPEC, _DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        instruction = "Asserções de estilo por classe/atributo, nunca por display computado."
        await handle.signal(
            "spec_conflict_resolution",
            {"verdict": "reauthor", "actor": "usr_test", "comment": instruction},
        )
        await wait_for_status(handle, {"review_ready"})
        ctx = state.tester_reauthor_contexts[-1] or ""
        assert instruction in ctx, "o comment do veredito chega ao prompt da ordem"
        assert "warning" in ctx, "e a evidência da falha continua junto"
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_the_full_chain_park_reauthor_rewrite_green_pr_ready(time_skipping_env):
    """A CADEIA inteira em fake, elo a elo — porque os três últimos vãos foram
    interações entre mecanismos individualmente corretos: parque (R10, no-op
    duplo) → veredito reauthor → reescrita executada COM tester_spec_reauthored
    no ledger → L1 verde → review_ready → done. O elo da reescrita auditada é
    exatamente o que faltou mudo no wi_6f00bf0a."""
    work_item_id = new_work_item_id("exh-chain")
    insert_work_item(work_item_id)
    component = "src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_findings_by_call=[
            [L1Finding(check="build", passed=False, detail="NG8001: unknown element")],
            [L1Finding(check="test", passed=False, detail=_NOOP_PINCER_DETAIL)],
        ],
        coder_files_changed_by_turn=[
            [component],
            [component],
            [],
            [],
        ],
        tester_test_files=[_BADGE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        d = _audit_details(work_item_id, "spec_conflict_detected")[0]
        assert d.get("reason") == "tester_spec_exhaustion"
        assert d["specs"] == [_BADGE_SPEC]
        joined = " ".join(d.get("expected_vs_received") or [])
        assert "in-progress" in joined and "in_progress" in joined

        await handle.signal(
            "spec_conflict_resolution", {"verdict": "reauthor", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        reauthored = _audit_details(work_item_id, "tester_spec_reauthored")
        assert reauthored, "a reescrita deixa evidência — nunca silêncio"
        assert reauthored[0]["files"] == [_BADGE_SPEC]
        assert reauthored[0]["reason"] == "human_order"
        assert state.tester_reauthor_orders[-1] == [_BADGE_SPEC]
        assert all(o == [] for o in state.tester_reauthor_orders[:-1])
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "coder_retry_cap_exhausted" not in actions
    assert "escalated" not in actions


# ---------------------------------------------------------------------------
# O veredito `reauthor`: humano autoriza, agente executa. A spec do Tester
# vive no Pod (commit sem push até o finalize) — não existe caminho out-of-band
# para o humano corrigi-la, então `retry` religa um laço em que ninguém pode
# agir. O veredito novo carrega a ORDEM: o julgamento de "a asserção está
# errada" é do humano; a reescrita, in-place e gateada por posse, é do Tester.
# ---------------------------------------------------------------------------


async def _wait_until(cond, *, attempts: int = 120, sleep_s: float = 0.25) -> None:
    """Espelho do wait_for_status para condições fora do status (ex.: linhas de
    auditoria) — mesmo intervalo de 250ms que preserva a janela do time-skip."""
    import asyncio
    for _ in range(attempts):
        if cond():
            return
        await asyncio.sleep(sleep_s)
    raise AssertionError("condição não alcançada no teto do helper")


@pytest.mark.asyncio
async def test_retry_reparks_because_no_actor_in_the_loop_can_move(time_skipping_env):
    """A resposta executável de "o retry basta?": não. O retry religa o laço,
    o Coder não pode tocar a spec (revert determinístico), o Tester reusa o
    alvo byte-idêntico e a porta 5 não age com veredito presente — o mesmo
    vermelho volta e o item RE-PARQUEIA, uma rodada inteira mais pobre. Só a
    ordem de re-autoria sai do ciclo."""
    work_item_id = new_work_item_id("exh-retry")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=3,
        l1_fail_detail=_WARNING_DETAIL,
        coder_files_changed=["src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"],
        tester_test_files=[_BADGE_SPEC, _DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        await handle.signal(
            "spec_conflict_resolution", {"verdict": "retry", "actor": "usr_test"},
        )
        await _wait_until(
            lambda: len(_audit_details(work_item_id, "spec_conflict_detected")) >= 2
        )
        await wait_for_status(handle, {"spec_conflict"})
        assert state.coder_turn_calls == 3, "o retry comprou exatamente uma rodada, e nada mudou"
        detected = _audit_details(work_item_id, "spec_conflict_detected")
        assert [d.get("reason") for d in detected] == ["tester_spec_exhaustion"] * 2

        await handle.signal(
            "spec_conflict_resolution", {"verdict": "reauthor", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_reauthor_order_reaches_the_tester_and_the_warning_converges(time_skipping_env):
    """DoD: o cenário do 'warning' converge com a ordem. O veredito `reauthor`
    NÃO compra turno de Coder (não há o que codar — o código está certo); o
    turno seguinte é do Tester, com os caminhos parqueados na ordem, one-shot."""
    work_item_id = new_work_item_id("exh-reauth")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_WARNING_DETAIL,
        coder_files_changed=["src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"],
        tester_test_files=[_BADGE_SPEC, _DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"spec_conflict"})
        coder_turns_at_park = state.coder_turn_calls
        await handle.signal(
            "spec_conflict_resolution", {"verdict": "reauthor", "actor": "usr_test"},
        )
        await wait_for_status(handle, {"review_ready"})
        assert state.coder_turn_calls == coder_turns_at_park, (
            "a rodada da ordem é do Tester; um turno de Coder aqui perseguiria "
            "a asserção que o humano acabou de julgar errada"
        )
        assert state.tester_reauthor_orders, "o Tester recebeu a ordem"
        assert state.tester_reauthor_orders[-1] == [_BADGE_SPEC, _DSE_SPEC]
        assert all(o == [] for o in state.tester_reauthor_orders[:-1]), (
            "a ordem é one-shot, não um estado que vaza para turnos futuros"
        )
        ordered = _audit_details(work_item_id, "tester_reauthor_ordered")
        assert ordered and ordered[0]["specs"] == [_BADGE_SPEC, _DSE_SPEC]
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "coder_retry_cap_exhausted" not in actions


def test_reauthor_is_only_honoured_for_the_testers_own_park():
    """`reauthor` só vale no parque de exaustão de spec PRÓPRIA — o DSE nunca
    reescreve spec de cliente, nem por ordem humana.

    EVOLUIU em 2026-08-10 (F2): este pin era um cenário ponta a ponta que
    parqueava a porta 1 e mandava `reauthor` para vê-lo recusado. Com a decisão
    de operador, spec de cliente NÃO PARQUEIA MAIS — o cenário ficou
    inalcançável, mas o invariante não morreu: ele vive na guarda de
    `_park_spec_conflict`, que só honra o veredito quando
    `reason == "tester_spec_exhaustion"`. É o que este teste passa a pinar, na
    unidade, onde a regra de fato está.

    A segunda camada continua onde sempre esteve: `_pod_reauthor_partition`
    recusa no git do Pod qualquer caminho que não seja autoria da plataforma
    (`test_tester_reauthor_order.py::test_order_is_executed_only_on_dse_authored_files`)."""
    import inspect

    from dse_orchestrator import workflows as _wf

    src = inspect.getsource(_wf.WorkItemLifecycleWorkflow._park_spec_conflict)
    assert 'reason == "tester_spec_exhaustion"' in src, (
        "a guarda que restringe o reauthor ao parque de spec própria sumiu — "
        "sem ela, uma ordem humana passaria a reescrever spec de cliente"
    )
    assert "_EscalateNow" in src, (
        "veredito não aplicável ao parque continua escalando, nunca resumindo"
    )


@pytest.mark.asyncio
async def test_a_mixed_failure_stays_in_the_normal_flow(time_skipping_env):
    """DoD 3: FAIL que inclui spec fora da posse do Tester não é o beco 1 —
    segue o laço normal (retry) e completa quando o L1 passa."""
    work_item_id = new_work_item_id("exh-mix")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=2,
        l1_fail_detail=_MIXED_DETAIL,
        coder_files_changed=["app.py"],
        tester_test_files=[_DSE_SPEC],
    )
    worker, handle = await _start(state, work_item_id, time_skipping_env)
    async with worker:
        await wait_for_status(handle, {"review_ready"})
        actions = read_audit_actions(work_item_id)
        assert "l1_failed_retrying" in actions
        assert "spec_conflict_detected" not in actions
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value
