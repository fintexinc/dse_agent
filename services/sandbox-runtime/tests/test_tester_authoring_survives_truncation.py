"""Uma resposta de autoria truncada não pode matar o item inteiro.

Medido no wi_95a54cb4 (`calculation-engine-service`, 2026-08-19, rc.101): o
modelo de autoria devolveu JSON cortado no meio de uma string ("Unterminated
string", char 160), o parse falhou, `tests_ran=false`, e o workflow terminou o
item TERMINAL (`tester_contract_failed`) — levando junto o turno de Coder de
US$ 2,77 que já estava pago. Uma resposta ruim de UM call = tarefa morta.

O próprio código já registrava o precedente: "4000 truncated the JSON in the
middle of the content ('Unterminated string')" — a resposta foi subir para
8000. Spec Java de verdade estourou os 8000. Teto maior adia; o que fecha é o
RETRY: uma segunda chamada, com o erro na cara e a ordem de encolher.
"""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

from dse_contracts import RunTesterTurnInput
from sandbox_runtime import activities


def _install_gateway(monkeypatch, respostas: list[str], prompts: list[str]):
    def fake_chat_completion(*, messages, **_kw):
        prompts.append(messages[0]["content"])
        return SimpleNamespace(content=respostas.pop(0), cost_usd=0.01)

    gw = types.ModuleType("model_gateway_client.gateway_call")
    gw.chat_completion = fake_chat_completion
    pkg = types.ModuleType("model_gateway_client")
    pkg.gateway_call = gw
    monkeypatch.setitem(sys.modules, "model_gateway_client", pkg)
    monkeypatch.setitem(sys.modules, "model_gateway_client.gateway_call", gw)


def _ctx():
    return activities._TesterContext(
        package_json="(no package.json — likely Python/pytest)",
        example_test="", existing_tests=set(), diff="+ new endpoint",
        skills_note="", reference_spec="",
    )


_BOA = json.dumps({"files": [{
    "path": "rest-adapter/src/test/java/com/x/MetricsTest.java",
    "content": "class MetricsTest {}",
}]})

_TRUNCADA = '{"files": [{"path": "rest-adapter/src/test/java/com/x/MetricsTest.java", "content": "class MetricsTest {\\n  void a() {'


def test_a_truncated_first_response_gets_one_retry(monkeypatch):
    prompts: list[str] = []
    _install_gateway(monkeypatch, [_TRUNCADA, _BOA], prompts)
    inp = RunTesterTurnInput(work_item_id="wi_t", tenant_id="t", instruction="metrics")

    script, cost = activities._model_authored_test_script(inp, _ctx(), headers=None, virtual_key="vk")

    assert script is not None, (
        "uma resposta truncada matou a autoria sem retry — foi assim que o "
        "wi_95a54cb4 morreu terminal com o Coder já pago"
    )
    assert any(s.get("tool") == "write_file" for s in script)
    assert len(prompts) == 2, "o retry é UM — nem zero, nem laço"
    assert "truncated" in prompts[1] or "not valid JSON" in prompts[1], (
        "a segunda chamada tem que dizer O QUE deu errado"
    )
    assert cost >= 0.02, "as DUAS chamadas custaram; o custo soma, nunca some"


def test_two_bad_responses_still_end_cleanly(monkeypatch):
    """O retry é um: duas respostas ruins = None, custo das duas somado, e o
    gate para limpo como antes — sem laço infinito de modelo."""
    prompts: list[str] = []
    _install_gateway(monkeypatch, [_TRUNCADA, "tampouco é json"], prompts)
    inp = RunTesterTurnInput(work_item_id="wi_t", tenant_id="t", instruction="metrics")

    script, cost = activities._model_authored_test_script(inp, _ctx(), headers=None, virtual_key="vk")

    assert script is None
    assert len(prompts) == 2
    assert abs(cost - 0.02) < 1e-9


def test_a_good_first_response_never_pays_for_a_second(monkeypatch):
    prompts: list[str] = []
    _install_gateway(monkeypatch, [_BOA], prompts)
    inp = RunTesterTurnInput(work_item_id="wi_t", tenant_id="t", instruction="metrics")

    script, cost = activities._model_authored_test_script(inp, _ctx(), headers=None, virtual_key="vk")
    assert script is not None
    assert len(prompts) == 1
