"""Porta 5 (diagnosticada 2026-08-07; dois itens mortos): o reuso do alvo do
Tester era EXISTENCIAL — se os arquivos de rodadas anteriores existem, re-roda
— sem consultar se o alvo produz VEREDITO. Uma spec própria que não compila
(`@MockBean`, wi_5620d2c1) ou importa módulo inexistente (`@ngx-translate`
herdado, wi_8edaef39) reprova test+build para sempre: o Coder não pode tocá-la
(revert) e o Tester nunca re-autora ($0 por rodada, mesmo erro até o teto).

A exceção cirúrgica, preservando o racional do alvo fixo: re-autoria SÓ para
arquivo cuja suite falhou SEM executar teste algum (carga/compilação — zero
veredito), in-place no MESMO caminho, gateada pela posse via git e classificada
POR ARQUIVO. Asserção falhando = veredito = intocável (deferral inalterado).
Vermelho antes do fix.
"""
from __future__ import annotations

import subprocess

from dse_contracts import RunTesterTurnInput
from sandbox_runtime import activities

_NGX_SPEC = "src/app/components/dashboard-list/dashboard-list.component-dse.spec.ts"
_HEALTHY_SPEC = "test/badge-visual-dse.spec.ts"
_JAVA_SPEC = "src/test/java/com/fintex/bmofeecalculatorbe/controller/rest/ReportOptionsControllerTest.java"

#: Verbatim (abreviado) do wi_8edaef39: suite morre na CARGA, zero asserções.
_JEST_ZERO_VERDICT = f"""
FAIL {_NGX_SPEC}
  ● Test suite failed to run

    Cannot find module '@ngx-translate/core' from 'src/app/shared/components/report-status-badge/report-status-badge.component.ts'
"""

#: Verbatim (abreviado) do wi_5620d2c1: testCompile do Maven, zero veredito.
_MAVEN_ZERO_VERDICT = f"""
[ERROR] /workspace/{_JAVA_SPEC}:[18,34] cannot find symbol
  symbol:   class MockBean
[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.11.0:testCompile (default-testCompile) on project bmo-fee-calculator-be: Compilation failure
"""

#: Asserção falhando: a suite EXECUTOU e entregou veredito — alvo fixo intocável.
_JEST_ASSERTION_RED = f"""
FAIL {_HEALTHY_SPEC}
  ● Badge › shows the finished state

    expect(received).toBe(expected)

Tests: 1 failed, 3 passed, 4 total
"""

#: Misto: uma quebrada na carga + uma executando com falha de asserção.
_JEST_MIXED = f"""
FAIL {_NGX_SPEC}
  ● Test suite failed to run

    Cannot find module '@ngx-translate/core' from 'report-status-badge.component.ts'
FAIL {_HEALTHY_SPEC}
  ● Badge › shows the finished state
    expect(received).toBe(expected)

Tests: 2 failed, 5 passed, 7 total
"""

_JEST_GREEN = "PASS everything\nTests: 4 passed, 4 total\n"
_MAVEN_GREEN = "[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS\n"


def _done(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _fake_cluster_seq(*, suite_seq, reused_files, seen):
    """Fake do cluster com SEQUÊNCIA de resultados de suite (1º run vermelho,
    re-run pós-reparo) e posse via git respondida como do Tester."""
    suite_results = list(suite_seq)

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        joined = " ".join(argv)
        if "head -c" in joined:
            if "find ." in joined:
                return _done(argv, 0, stdout="./src/app.spec.ts\n")
            if "cat package.json" in joined:
                return _done(argv, 0, stdout='{"name":"fixture","scripts":{"test":"jest"}}')
            if "git show" in joined:
                return _done(argv, 0, stdout="diff --git a/x b/x\n")
            return _done(argv, 0, stdout="")
        if "--grep='^tester('" in joined:
            return _done(argv, 0, stdout="".join(f"{f}\n" for f in reused_files))
        if "git log --format=%s" in joined:
            # posse: história só com sujeitos do DSE
            return _done(argv, 0, stdout="tester(wi_fixture): authored\n")
        if any(m in joined for m in ("npm test", "python3 -m pytest")):
            return suite_results.pop(0) if suite_results else _done(argv, 0, stdout=_JEST_GREEN)
        return _done(argv, 0, stdout="deadbeef\n")

    return fake_run


def _repair_script_stub(calls, files_content):
    """Substitui a autoria pelo modelo: grava a chamada e devolve um script de
    reparo com os arquivos pedidos (e um arquivo NOVO intruso, que o filtro
    determinístico tem que descartar)."""

    def stub(inp, ctx, headers, virtual_key, *, error_feedback=""):
        calls.append({"error_feedback": error_feedback})
        script = [
            {"tool": "write_file", "path": p, "content": c} for p, c in files_content
        ]
        script.append({"tool": "write_file", "path": "test/intruso-novo-dse.spec.ts", "content": "x"})
        script.append({"tool": "run_tests"})
        return script, 0.05

    return stub


def _run(monkeypatch, *, suite_seq, reused_files, stub_files):
    seen: list = []
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_cluster_seq(
        suite_seq=suite_seq, reused_files=reused_files, seen=seen))
    monkeypatch.setattr(activities, "_model_authored_test_script",
                        _repair_script_stub(calls, stub_files))
    rows: list[dict] = []
    monkeypatch.setattr(activities, "audit_emit", lambda **kw: rows.append(kw))
    result = activities._tester_pod_sync(
        RunTesterTurnInput(work_item_id="wi-p5", tenant_id="t", instruction="cover"),
        "dse-sbx-wi-p5", None, "vk", False,
    )
    return result, seen, calls, rows


def _writes_to(seen, path):
    return [a for a, _k in seen if any(f"cat > {path}" in s for s in a if isinstance(s, str))]


def test_zero_verdict_spec_is_reauthored_in_place(monkeypatch):
    """(a) spec que não compila/carrega → re-autoria IN-PLACE + re-run; o turno
    converge quando o reparo devolve uma suite com veredito."""
    result, seen, calls, rows = _run(
        monkeypatch,
        suite_seq=[_done([], 1, stdout=_JEST_ZERO_VERDICT), _done([], 0, stdout=_JEST_GREEN)],
        reused_files=[_NGX_SPEC],
        stub_files=[(_NGX_SPEC, "// spec reparada sem ngx-translate\n")],
    )
    assert calls, "o reparo tem que chamar a autoria"
    assert _writes_to(seen, _NGX_SPEC), "reparo é in-place, no MESMO caminho"
    assert result.returncode == 0, "o re-run pós-reparo é o resultado do turno"
    repaired = [r for r in rows if r["action"] == "tester_spec_repaired"]
    assert repaired and repaired[0]["details"]["files"] == [_NGX_SPEC]


def test_assertion_failure_keeps_the_fixed_target(monkeypatch):
    """(b) asserção falhando = veredito = alvo fixo intocável (reuso de hoje)."""
    result, seen, calls, rows = _run(
        monkeypatch,
        suite_seq=[_done([], 1, stdout=_JEST_ASSERTION_RED)],
        reused_files=[_HEALTHY_SPEC],
        stub_files=[(_HEALTHY_SPEC, "nunca usado")],
    )
    assert not calls, "veredito existe: re-autorar aqui é reescrever o teste que reprova"
    assert not _writes_to(seen, _HEALTHY_SPEC)
    assert result.returncode == 1


def test_only_the_broken_file_is_repaired(monkeypatch):
    """(c)+(d) misto: só a quebrada é re-autorada; a saudável e o arquivo NOVO
    que o modelo tentar criar são descartados pelo filtro determinístico."""
    result, seen, calls, rows = _run(
        monkeypatch,
        suite_seq=[_done([], 1, stdout=_JEST_MIXED), _done([], 1, stdout=_JEST_ASSERTION_RED)],
        reused_files=[_NGX_SPEC, _HEALTHY_SPEC],
        stub_files=[(_NGX_SPEC, "reparada"), (_HEALTHY_SPEC, "NUNCA")],
    )
    assert calls
    assert _writes_to(seen, _NGX_SPEC)
    assert not _writes_to(seen, _HEALTHY_SPEC), "spec com veredito não é reescrita"
    assert not _writes_to(seen, "test/intruso-novo-dse.spec.ts"), "reparo nunca cria arquivo novo"


def test_mockbean_scenario_converges(monkeypatch):
    """(DoD 3) wi_5620d2c1: testCompile do Maven (zero veredito) → reparo →
    suite verde com contagem Surefire."""
    result, seen, calls, rows = _run(
        monkeypatch,
        suite_seq=[_done([], 1, stdout=_MAVEN_ZERO_VERDICT), _done([], 0, stdout=_MAVEN_GREEN)],
        reused_files=[_JAVA_SPEC],
        stub_files=[(_JAVA_SPEC, "// @MockitoBean\n")],
    )
    assert calls and _writes_to(seen, _JAVA_SPEC)
    assert result.returncode == 0 and result.tests_passed is True


def test_ngx_translate_scenario_converges(monkeypatch):
    """(DoD 3) wi_8edaef39: módulo inexistente herdado → reparo → verde."""
    result, seen, calls, rows = _run(
        monkeypatch,
        suite_seq=[_done([], 1, stdout=_JEST_ZERO_VERDICT), _done([], 0, stdout=_JEST_GREEN)],
        reused_files=[_NGX_SPEC],
        stub_files=[(_NGX_SPEC, "reparada")],
    )
    assert result.returncode == 0 and result.tests_passed is True
    repaired = [r for r in rows if r["action"] == "tester_spec_repaired"]
    assert repaired, "o reparo é auditável no ledger"


def test_the_repair_scope_is_this_turns_targets_not_a_git_question():
    """A porta 5 repara o que está na PRÓPRIA lista de alvos do turno.

    Até 2026-08-10 ela perguntava ao git do Pod "algum sujeito humano na
    história deste arquivo?" como PROXY para "isto é meu". O proxy era
    redundante — `test_files` já é a resposta direta — e errava com o clone
    raso (`--depth 50`), em que o histórico humano do cliente fica fora da
    janela e o arquivo dele passava por nosso. Saiu com o resto do oráculo de
    autoria.

    O escopo é o que impede a porta 5 de virar licença para reescrever
    qualquer spec quebrada do repositório: uma spec fora da lista de alvos não
    é reparada nem quando aparece quebrada na mesma saída."""
    from sandbox_runtime.activities import _zero_verdict_specs

    output = (
        "FAIL src/mine.spec.ts\n  ● Test suite failed to run\n"
        "FAIL src/theirs.spec.ts\n  ● Test suite failed to run\n"
    )
    assert _zero_verdict_specs(output, ["src/mine.spec.ts"]) == ["src/mine.spec.ts"], (
        "o alvo deste turno é reparável"
    )
    assert "src/theirs.spec.ts" not in _zero_verdict_specs(output, ["src/mine.spec.ts"]), (
        "spec fora da lista de alvos não entra — o escopo É a regra de posse "
        "que sobrou"
    )
