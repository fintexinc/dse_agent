

def test_a_skipped_gate_may_be_passed_without_claiming_it_ran():
    """`SKIPPED` + `passed=True` é o único par fora de PASS que o contrato
    aceita, e ele existe para um caso específico: o gate que não rodou porque
    outro já reprovou a rodada.

    Os dois lados do par são obrigatórios. `passed=False` colocaria o gate em
    `failed_checks` do workflow e mandaria um turno de Coder consertar algo que
    nunca executou. E carimbar `PASS` — que era a única saída antes disto —
    escreveria "test: PASS" no ledger sobre uma suíte que não rodou, que é
    exatamente o falso verde que este repositório passou o dia matando.

    Continua proibido para todo o resto: FAIL e ERROR com `passed=True` seguem
    inválidos, porque ali o gate PRODUZIU veredito."""
    import pytest
    from dse_contracts import GateStatus, L1Finding

    pulado = L1Finding(check="test", passed=True, status=GateStatus.SKIPPED,
                       detail="not run: lint already failed this round",
                       summary="not run: lint already failed this round")
    assert pulado.status is GateStatus.SKIPPED and pulado.passed is True

    for status in (GateStatus.FAIL, GateStatus.ERROR):
        with pytest.raises(ValueError):
            L1Finding(check="test", passed=True, status=status)
    with pytest.raises(ValueError):
        L1Finding(check="test", passed=False, status=GateStatus.SKIPPED)
