

def test_a_skipped_gate_is_not_passed_and_says_so_by_status():
    """O gate que não rodou não passou — e o contrato não abre exceção.

    A rc.115 tentou o contrário (`SKIPPED` com `passed=True`) para manter o
    gate fora do `failed_checks` do workflow, e isso contradizia um teste de
    fronteira que já existia aqui. O invariante "passed é verdadeiro só em
    PASS" é simples e vale a pena manter: dizer que uma suíte não executada
    passou é o falso verde de sempre, agora escrito pelo próprio contrato.

    Quem separa "não rodou" de "reprovou" é o STATUS, e a separação vive no
    CONSUMIDOR: `failed_checks` exclui SKIPPED, do mesmo jeito que
    `_l1_infra_gates` já classificava por status em vez de pelo booleano."""
    import pytest
    from dse_contracts import GateStatus, L1Finding

    pulado = L1Finding(check="test", passed=False, status=GateStatus.SKIPPED,
                       detail="not run: lint already failed this round",
                       summary="not run: lint already failed this round")
    assert pulado.status is GateStatus.SKIPPED and pulado.passed is False

    for status in (GateStatus.FAIL, GateStatus.ERROR, GateStatus.SKIPPED):
        with pytest.raises(ValueError):
            L1Finding(check="test", passed=True, status=status)
