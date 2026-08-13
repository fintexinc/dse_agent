"""Real Planner (proposer via model through the gateway) — found on the 1st real
run: the Planner ALWAYS used the fixture (empty expected_files) and every task
escalated at the anti-hollow-PR gate. Now, with a real substrate configured, the
model proposes the plan; any failure (import/call/JSON) falls back to the
fixture → CLEAN escalation (P6).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from sandbox_runtime.activities import _default_plan_proposer, _model_plan_proposer


class _Ctx:
    # Mirrors PlannerContext.render's signature, keyword included: the Planner
    # passes skill_body_chars=0 to keep 21 skills inside its 8 KB context
    # budget, and a double that does not accept it fails every test here with a
    # TypeError that says nothing about what actually changed.
    def render(self, *, skill_body_chars: int | None = None) -> str:
        return "## Task\nFix DELETE /api/transactions/:id, which deletes the wrong transaction."


def _inp():
    return SimpleNamespace(instruction="fix delete", work_item_id="wi-x", tenant_id="t",
                           repo="acme/app", base_branch="main")


def _patch_chat(monkeypatch, content: str):
    def fake_chat_completion(**kwargs):
        return SimpleNamespace(content=content, model="anthropic/claude",
                               cost_usd=0.01, tokens_in=100, tokens_out=50, raw={})
    import model_gateway_client.gateway_call as gc
    import sandbox_runtime.activities as acts
    monkeypatch.setattr(gc, "chat_completion", fake_chat_completion)
    # tree via the GitHub API: isolated in tests (best-effort in the real code)
    monkeypatch.setattr(acts, "_repo_tree_for_planner", lambda repo, br: [])


def test_model_proposal_parsed_with_files(monkeypatch):
    _patch_chat(monkeypatch, json.dumps({
        "steps": ["locate the DELETE handler", "fix the lookup by id"],
        "expected_files": ["server/routes/transactions.js", "server/db.js"],
        "test_plan": "curl DELETE ids 2/12/11 and verify exactness",
    }))
    p = _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk")
    assert p is not None
    assert p["expected_files"] == ["server/routes/transactions.js", "server/db.js"]
    assert len(p["steps"]) == 2


def test_model_proposal_with_markdown_fence(monkeypatch):
    _patch_chat(monkeypatch, "```json\n" + json.dumps({
        "steps": ["s1"], "expected_files": ["a.py"], "test_plan": "t"}) + "\n```")
    p = _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk")
    assert p is not None and p["expected_files"] == ["a.py"]


def test_unparseable_response_falls_back_to_none(monkeypatch):
    _patch_chat(monkeypatch, "sorry, I cannot")
    assert _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk") is None


def test_empty_expected_files_from_model_is_rejected(monkeypatch):
    # model answering JSON with an empty list → None → fixture → the gate escalates
    _patch_chat(monkeypatch, json.dumps({"steps": ["s"], "expected_files": [], "test_plan": "t"}))
    assert _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk") is None


def test_gateway_error_falls_back_to_none(monkeypatch):
    import model_gateway_client.gateway_call as gc
    import sandbox_runtime.activities as acts
    def boom(**kwargs):
        raise RuntimeError("budget_exhausted")
    monkeypatch.setattr(gc, "chat_completion", boom)
    monkeypatch.setattr(acts, "_repo_tree_for_planner", lambda repo, br: [])
    assert _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk") is None


def test_tree_paths_flow_into_prompt(monkeypatch):
    """With the real tree available, the prompt demands EXISTING paths —
    plan_compliance validates the diff against the plan (found on the real
    run)."""
    captured = {}
    def fake_chat_completion(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        import json
        return SimpleNamespace(content=json.dumps({
            "steps": ["s"], "expected_files": ["src/store.js"], "test_plan": "t"}),
            model="m", cost_usd=0.0, tokens_in=1, tokens_out=1, raw={})
    import model_gateway_client.gateway_call as gc
    import sandbox_runtime.activities as acts
    monkeypatch.setattr(gc, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(acts, "_repo_tree_for_planner",
                        lambda repo, br: ["server.js", "src/store.js", "package.json"])
    p = _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk")
    assert p is not None and p["expected_files"] == ["src/store.js"]
    assert "Repo tree" in captured["prompt"]
    assert "src/store.js" in captured["prompt"]


def test_fixture_still_yields_empty_files():
    p = _default_plan_proposer(_Ctx(), _inp())
    assert p["expected_files"] == []  # and the WS-B gate escalates — deliberate


def test_the_issue_instruction_reaches_the_prompt(monkeypatch):
    """3rd real run: the PlannerContext does not carry the instruction (render()
    only has AGENTS.md/skills/repo map — all empty for the tenant) and the model,
    never having seen the issue, planned a generic wallet FEATURE instead of the
    DELETE bug. The instruction now goes straight into the '## Task' section."""
    captured = {}

    def fake_chat_completion(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return SimpleNamespace(
            content=json.dumps({"steps": ["s"], "expected_files": ["a.js"], "test_plan": "t"}),
            model="anthropic/claude", cost_usd=0.01, tokens_in=1, tokens_out=1, raw={},
        )

    import model_gateway_client.gateway_call as gc
    import sandbox_runtime.activities as acts
    monkeypatch.setattr(gc, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(acts, "_repo_tree_for_planner", lambda repo, br: [])

    inp = _inp()
    inp.instruction = "Deleting one transaction deletes another transaction — DELETE /api/transactions/:id"
    p = _model_plan_proposer(_Ctx(), inp, headers=None, virtual_key="vk")
    assert p is not None
    prompt = captured["prompt"]
    assert "Deleting one transaction deletes another transaction" in prompt
    # the task comes BEFORE the additional context (it is the target, not a footnote)
    assert prompt.index("Deleting one transaction") < prompt.index("Additional context")


def test_a_missing_instruction_becomes_an_explicit_marker(monkeypatch):
    captured = {}

    def fake_chat_completion(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return SimpleNamespace(
            content=json.dumps({"steps": ["s"], "expected_files": ["a.js"], "test_plan": "t"}),
            model="anthropic/claude", cost_usd=0.01, tokens_in=1, tokens_out=1, raw={},
        )

    import model_gateway_client.gateway_call as gc
    import sandbox_runtime.activities as acts
    monkeypatch.setattr(gc, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(acts, "_repo_tree_for_planner", lambda repo, br: [])

    inp = _inp()
    inp.instruction = ""
    _model_plan_proposer(_Ctx(), inp, headers=None, virtual_key="vk")
    assert "(instruction missing)" in captured["prompt"]


def test_estimated_lines_parsed_and_clamped(monkeypatch):
    """rc.89: o Planner passa a declarar `estimated_lines`. Inteiro entra;
    string numérica entra; acima do teto são (20k) vira o teto — nunca um
    número absurdo que o modal exibiria como fato."""
    for raw, esperado in ((120, 120), ("350", 350), (25_000, 20_000)):
        _patch_chat(monkeypatch, json.dumps({
            "steps": ["s"], "expected_files": ["a.js"], "test_plan": "t",
            "estimated_lines": raw,
        }))
        p = _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk")
        assert p is not None
        assert p["estimated_lines"] == esperado, (
            f"estimated_lines={raw!r} devia virar {esperado}, veio {p.get('estimated_lines')!r}"
        )


def test_estimated_lines_absent_or_garbage_is_none(monkeypatch):
    """Ausente, lixo ou não-positivo → None, nunca um default inventado — foi
    exatamente um default nunca dimensionado (400) que virou 'previsão' na tela."""
    casos = (
        {"steps": ["s"], "expected_files": ["a.js"], "test_plan": "t"},
        {"steps": ["s"], "expected_files": ["a.js"], "test_plan": "t", "estimated_lines": "muitas"},
        {"steps": ["s"], "expected_files": ["a.js"], "test_plan": "t", "estimated_lines": 0},
        {"steps": ["s"], "expected_files": ["a.js"], "test_plan": "t", "estimated_lines": -5},
    )
    for caso in casos:
        _patch_chat(monkeypatch, json.dumps(caso))
        p = _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk")
        assert p is not None
        assert p["estimated_lines"] is None, (
            f"{caso.get('estimated_lines', '<ausente>')!r} devia virar None"
        )


def test_prompt_asks_for_estimated_lines(monkeypatch):
    """Sem pedir, o modelo não declara — e a tela volta a mostrar um número de
    ninguém. O prompt tem de pedir a chave explicitamente."""
    captured = {}

    def fake_chat_completion(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return SimpleNamespace(
            content=json.dumps({"steps": ["s"], "expected_files": ["a.js"], "test_plan": "t"}),
            model="anthropic/claude", cost_usd=0.01, tokens_in=1, tokens_out=1, raw={},
        )

    import model_gateway_client.gateway_call as gc
    import sandbox_runtime.activities as acts
    monkeypatch.setattr(gc, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(acts, "_repo_tree_for_planner", lambda repo, br: [])

    _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk")
    assert "estimated_lines" in captured["prompt"], (
        "o prompt do Planner não pede estimated_lines — a estimativa nunca nasce"
    )
