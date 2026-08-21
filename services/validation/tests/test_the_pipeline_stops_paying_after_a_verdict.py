"""Um gate barato já reprovou; a suíte de sete minutos não muda nada.

Medido em wi_2325adc (2026-08-21), duas rodadas seguidas do MESMO item:

    16:15  lint FAIL em 18,8s  →  test rodou 252s, build 27,7s
    16:33  lint FAIL em  7,2s  →  test rodou 438s

690 segundos de suíte para reprovar por uma formatação que o lint já tinha
achado em 26. O pipeline comprava informação que o veredito daquela rodada
não ia usar — e paga isso a CADA volta do laço de fix, que é justamente
quando o item já está caro.

A regra: os gates CAROS (`test`, `build`) rodam por último e só se nada tiver
reprovado antes. Tudo que é barato ou de segurança continua rodando sempre —
`sast`, `secret_scan`, `forbidden_paths` e o orçamento de diff medem fatos
sobre o que já está no branch remoto, e um segredo não deixa de estar exposto
porque o lint reprovou.

O que isto NÃO afrouxa, e é o motivo de ser seguro: pular só acontece em
rodada que JÁ está reprovada. Uma rodada verde executa exatamente os mesmos
oito gates de sempre, pelo mesmo custo. Não existe caminho em que um `test`
pulado vire autorização para seguir.
"""
from __future__ import annotations

from dse_contracts import GateStatus
from dse_validation.config import L1Config
from dse_validation.l1 import pipeline


def _finding(check, *, passed, status=None):
    from dse_validation.l1.pipeline import L1Finding

    return L1Finding(check=check, passed=passed,
                     status=status or (GateStatus.PASS if passed else GateStatus.FAIL),
                     detail="", summary="")


def _corre(monkeypatch, *, lint_ok: bool, registro: list):
    """Roda o pipeline com todos os gates falsos, anotando quem executou."""
    from dse_validation.l1 import plan_compliance, quality_checks, sast, secret_scan

    def marca(nome, passed=True):
        def _f(*a, **kw):
            registro.append(nome)
            return _finding(nome, passed=passed)
        return _f

    monkeypatch.setattr(quality_checks, "lint_check", marca("lint", lint_ok))
    monkeypatch.setattr(quality_checks, "typecheck_check", marca("typecheck"))
    monkeypatch.setattr(quality_checks, "test_check", marca("test"))
    monkeypatch.setattr(quality_checks, "build_check", marca("build"))
    monkeypatch.setattr(sast, "sast_check", marca("sast"))
    monkeypatch.setattr(secret_scan, "secret_scan_check", marca("secret_scan"))
    monkeypatch.setattr(plan_compliance, "compute_diff_or_none", lambda *a, **kw: None)

    def _plan_findings(*a, **kw):
        registro.append("plan_compliance")
        return [_finding("forbidden_paths", passed=True)]

    monkeypatch.setattr(plan_compliance, "plan_compliance_findings", _plan_findings)
    # `manifest_status` explícito: o default do construtor é NOT_CONFIGURED, que
    # sozinho já reprovaria a rodada e mascararia o que estes testes medem.
    monkeypatch.setattr(L1Config, "from_trusted_manifest",
                        classmethod(lambda cls, *a, **kw: cls(
                            test_cmd=["x"], manifest_status=GateStatus.PASS)))
    monkeypatch.setattr(pipeline.db, "record_validation_run", lambda *a, **kw: None)

    from dse_contracts import PlanArtifact

    return pipeline.run_l1_pipeline_core(
        executor=object(), work_item_id="wi_x", tenant_id="t",
        plan=PlanArtifact(work_item_id="wi_x", steps=[], expected_files=[]),
        base_sha="a" * 40, head_sha="b" * 40, persist=False,
    )


def test_a_green_round_still_runs_every_gate(monkeypatch):
    """A rodada que passa custa o mesmo de sempre — o ganho é só nas que já
    estão perdidas."""
    registro: list = []
    resultado = _corre(monkeypatch, lint_ok=True, registro=registro)

    assert resultado.passed is True
    for gate in ("lint", "typecheck", "test", "build", "sast", "secret_scan"):
        assert gate in registro, gate


def test_a_cheap_failure_does_not_buy_the_expensive_gates(monkeypatch):
    registro: list = []
    resultado = _corre(monkeypatch, lint_ok=False, registro=registro)

    assert resultado.passed is False
    assert "test" not in registro, "a suíte não roda depois do veredito"
    assert "build" not in registro


def test_the_security_gates_run_even_on_a_failing_round(monkeypatch):
    """Segredo e caminho proibido são fatos sobre o que JÁ está no branch
    remoto. Deixar de olhar porque o lint reprovou seria trocar segurança por
    segundos."""
    registro: list = []
    _corre(monkeypatch, lint_ok=False, registro=registro)

    for gate in ("sast", "secret_scan", "plan_compliance"):
        assert gate in registro, gate


def test_a_skipped_gate_never_reads_as_a_verdict_on_the_diff(monkeypatch):
    """O gate pulado entra no ledger com SKIPPED e `passed=False`: ele não
    rodou, então não passou, e o contrato não abre exceção para isso.

    Quem impede o laço de mandar um turno de Coder consertar um `test` que
    NUNCA RODOU é o consumidor: `failed_checks` no workflow exclui SKIPPED,
    classificando por status como `_l1_infra_gates` já fazia. A separação vive
    lá porque é lá que a pergunta "o que falhou?" é feita."""
    registro: list = []
    resultado = _corre(monkeypatch, lint_ok=False, registro=registro)

    pulados = [f for f in resultado.findings if f.status is GateStatus.SKIPPED]
    assert {f.check for f in pulados} == {"test", "build"}
    assert all(not f.passed for f in pulados), "não rodou não é o mesmo que passou"
    assert all("already failed" in f.summary for f in pulados)


def test_skipping_can_only_happen_on_a_round_that_is_already_lost(monkeypatch):
    """A invariante de segurança inteira em uma linha: se algo foi pulado, a
    rodada NÃO passou. Sem isto, um `test` pulado seria autorização para
    seguir."""
    for lint_ok in (True, False):
        registro: list = []
        resultado = _corre(monkeypatch, lint_ok=lint_ok, registro=registro)
        houve_pulo = any(f.status is GateStatus.SKIPPED for f in resultado.findings)
        assert not (houve_pulo and resultado.passed)


def test_the_loop_is_never_sent_to_fix_a_gate_that_did_not_run(monkeypatch):
    """A invariante que sobrou do desenho errado da rc.115, agora no lugar
    certo: o workflow monta `failed_checks` excluindo SKIPPED.

    Sem isso o laço leria o `test` pulado como reprovado e compraria um turno
    de Coder para consertar uma suíte que nunca executou — o mesmo defeito que
    `_l1_infra_gates` existe para impedir, entrando por outra porta. Este teste
    fica AQUI, ao lado de quem produz o SKIPPED, porque é a produção e o
    consumo juntos que formam o contrato."""
    from dse_contracts import GateStatus

    registro: list = []
    resultado = _corre(monkeypatch, lint_ok=False, registro=registro)

    # o que o workflow faria (workflows.py: montagem de failed_checks)
    failed = [f for f in resultado.findings
              if not f.passed and f.status is not GateStatus.SKIPPED]

    nomes = {f.check for f in failed}
    assert "lint" in nomes, "o gate que decidiu tem de estar lá"
    assert "test" not in nomes and "build" not in nomes, (
        "gate pulado não compra turno de Coder"
    )
