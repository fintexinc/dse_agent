"""A doença medida três vezes (badge 'warning', pageSize, sortField): a nota
de skills do Tester manda o modelo "read each SKILL.md below" — mas a autoria
é one-shot SEM ferramenta de leitura. O modelo não pode abrir arquivo nenhum;
a instrução de leitura para ator sem leitura é prompt vazio com moldura de
obrigação. A resposta esteve no Pod o tempo todo (angular-testbed/SKILL.md:71
e a spec de referência da sonda em references/): o wi_53c820f1 re-parqueou com
o MESMO sortField que a skill resolve em duas linhas.

O vermelho prova o MECANISMO, não só o resultado: um modelo fake cujo output
depende exclusivamente do que está NO PROMPT — sem o conteúdo da referência
produz o initialState incompleto; com o conteúdo inline, produz o completo.
A única alavanca é o prompt; se este teste virar verde, a causa era essa.
Vermelho antes do fix.
"""
from __future__ import annotations

import json
import subprocess
import sys
import types
from types import SimpleNamespace

from dse_contracts import RunTesterTurnInput
from sandbox_runtime import activities

_REFERENCE_SPEC = """// .claude/skills/angular-testbed/references/dashboard-list-integration.spec.ts.txt
provideMockStore({
  initialState: {
    dashboard: { data: mockDashboardData, loading: false, error: null },
    pagination: { currentPage: 0, pageSize: 10 },
    tableSorting: { sortField: '', sortOrder: 0 as const },
  },
})
"""


class _FakePodSh:
    """Responde os reads bounded do `_pod_tester_context` por padrão do script.
    A ordem dos elifs importa: o read da referência também menciona
    `.claude/skills`, então ele é testado ANTES da listagem de skills."""

    def __call__(self, script: str, **_kw) -> subprocess.CompletedProcess:
        out = ""
        if "cat package.json" in script:
            out = '{"scripts": {"test": "jest"}}'
        elif "git rev-parse" in script:
            out = "./src/app/existing-dse.spec.ts\n"
        elif "cat -- " in script:
            out = "describe('existing', () => { it('works', () => {}); });"
        elif "git show" in script:
            out = "diff --git a/dashboard-list.component.html b/dashboard-list.component.html"
        elif "references" in script:
            out = _REFERENCE_SPEC
        elif ".claude/skills" in script:
            out = "- .claude/skills/angular-testbed/SKILL.md — TestBed/MockStore shapes for this repo"
        return subprocess.CompletedProcess(args=script, returncode=0, stdout=out, stderr="")


_BADGE_SPEC = "src/app/components/homepage/components/report-status-badge/report-status-badge.component.spec.ts"
_BADGE_TS = "src/app/components/homepage/components/report-status-badge/report-status-badge.component.ts"
_BADGE_HTML = "src/app/components/homepage/components/report-status-badge/report-status-badge.component.html"

#: Fonte do sujeito, com os dois marcadores que só existem NO CONTEÚDO (nunca
#: em caminhos): a declaração de signal input e o data-test real do template.
_SUBJECT_SOURCES = {
    _BADGE_TS: (
        "export class ReportStatusBadgeComponent {\n"
        "  currentPage = input<string>('');\n"
        "  protected status = computed(() => {\n"
        "    const page = this.currentPage()?.toLowerCase() ?? '';\n"
        "    return page === 'generate_report' ? 'finished' : 'in-progress';\n"
        "  });\n}"
    ),
    _BADGE_HTML: (
        '<span data-test="report-status-badge" class="inline-flex items-center">\n'
        "  {{ statusLabel }}\n</span>"
    ),
}


def test_subject_mapping_for_ordered_spec():
    """O mapeamento determinístico da porta 1, agora do lado da ordem: a spec
    aponta seu sujeito por convenção de nome (tira -dse e .spec), e o sujeito
    Angular vem em par ts+html."""
    assert activities._subject_candidates_for_spec(_BADGE_SPEC) == [_BADGE_TS, _BADGE_HTML]
    assert activities._subject_candidates_for_spec(
        "src/app/x/dashboard-list.component-dse.spec.ts"
    ) == ["src/app/x/dashboard-list.component.ts", "src/app/x/dashboard-list.component.html"]
    assert activities._subject_candidates_for_spec("scripts/build.sh") == []


def test_the_subject_source_in_the_prompt_is_what_fixes_the_mechanics(monkeypatch):
    """wi_53c820f1, re-parque 2 da ordem: a reescrita clobrou o signal input
    (`component.currentPage = 'finished'` → TypeError no componente:18) e
    inventou `data-test="status-badge"` — porque no turno de reauthor o diff é
    o commit anterior do PRÓPRIO Tester e a fonte do componente sumiu do
    prompt. O fake só emite `setInput` + o data-test real se a FONTE estiver
    no prompt — se este teste é verde, a causa era essa."""
    def fake_chat_completion(*, messages, **_kw):
        prompt = messages[0]["content"]
        sees_subject = (
            'data-test="report-status-badge"' in prompt and "input<" in prompt
        )
        body = (
            "fixture.componentRef.setInput('currentPage', 'generate_report');\n"
            "const badge = fixture.nativeElement.querySelector('[data-test=\"report-status-badge\"]');"
            if sees_subject
            else
            "component.currentPage = 'finished';\n"
            "const badge = fixture.nativeElement.querySelector('[data-test=\"status-badge\"]');"
        )
        return SimpleNamespace(
            content=json.dumps({"files": [{"path": "src/app/badge-dse.spec.ts", "content": body}]}),
            cost_usd=0.01,
        )

    gw_stub = types.ModuleType("model_gateway_client.gateway_call")
    gw_stub.chat_completion = fake_chat_completion
    pkg_stub = types.ModuleType("model_gateway_client")
    pkg_stub.gateway_call = gw_stub
    monkeypatch.setitem(sys.modules, "model_gateway_client", pkg_stub)
    monkeypatch.setitem(sys.modules, "model_gateway_client.gateway_call", gw_stub)

    feedback = activities._reauthor_order_feedback(
        [_BADGE_SPEC],
        "Expected: \"inline-block\"\nReceived: \"block\"",
        lambda p: _SUBJECT_SOURCES.get(p, ""),
    )
    ctx = activities._pod_tester_context(_FakePodSh())
    inp = RunTesterTurnInput(work_item_id="wi_test", tenant_id="t", instruction="badge")
    script, _cost = activities._model_authored_test_script(
        inp, ctx, headers=None, virtual_key="vk", error_feedback=feedback
    )
    contents = [s["content"] for s in (script or []) if s.get("tool") == "write_file"]
    assert contents, "a ordem produziu um write_file"
    assert "setInput(" in contents[0], "com a fonte no prompt, o input é signal — setInput"
    assert '[data-test="report-status-badge"]' in contents[0], "e o data-test é o REAL do template"


def test_the_reference_content_in_the_prompt_is_what_completes_the_authoring(monkeypatch):
    prompts: list[str] = []

    def fake_chat_completion(*, messages, **_kw):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        # O fake é sensível SÓ ao prompt: vê a forma completa da store apenas
        # se ela estiver inline — exatamente como um modelo sem tools.
        complete = "tableSorting" in prompt and "pageSize: 10" in prompt
        body = (
            "provideMockStore({ initialState: { dashboard: { data: d, loading: false, "
            "error: null }, pagination: { currentPage: 0, pageSize: 10 }, "
            "tableSorting: { sortField: '', sortOrder: 0 } } })"
            if complete
            else "provideMockStore({ initialState: { dashboard: { data: d } } })"
        )
        return SimpleNamespace(
            content=json.dumps({"files": [{"path": "src/app/badge-dse.spec.ts", "content": body}]}),
            cost_usd=0.01,
        )

    gw_stub = types.ModuleType("model_gateway_client.gateway_call")
    gw_stub.chat_completion = fake_chat_completion
    pkg_stub = types.ModuleType("model_gateway_client")
    pkg_stub.gateway_call = gw_stub
    monkeypatch.setitem(sys.modules, "model_gateway_client", pkg_stub)
    monkeypatch.setitem(sys.modules, "model_gateway_client.gateway_call", gw_stub)

    ctx = activities._pod_tester_context(_FakePodSh())
    inp = RunTesterTurnInput(work_item_id="wi_test", tenant_id="t", instruction="badge specs")
    script, _cost = activities._model_authored_test_script(
        inp, ctx, headers=None, virtual_key="vk"
    )
    contents = [s["content"] for s in (script or []) if s.get("tool") == "write_file"]
    assert contents, "a autoria produziu um write_file"
    assert "tableSorting" in contents[0] and "pageSize" in contents[0], (
        "sem o conteúdo da referência no prompt o mock sai incompleto (a doença); "
        "com ele inline, completo — a causa é o prompt, não o modelo"
    )
