"""Comando ausente no manifesto não é trabalho de Coder — é configuração.

Auditoria de 2026-08-20. Um estágio `NOT_CONFIGURED` (o repo não declarou
`build`, ou `test`, ou `typecheck`) cai em `failed_checks` e compra um turno de
Coder. Só que o Coder **não pode consertar**: o manifesto é lido do BASE SHA
(`git show {base_sha}:.dse/validation.json`), e o base SHA não se move dentro do
laço. Ele edita o arquivo, o gate segue lendo a versão antiga, e o `detail` é
constante — então o fingerprint de reparo hasheia igual nas duas rodadas e o item
morre em `coder_not_converging`, que é um diagnóstico ERRADO.

Custo exato por comando ausente: 2 rodadas de Coder+Tester e 3 pipelines de L1.
Precedente do mesmo formato de beco: wi_530a1f56, US$ 18,90.

E o manifesto NÃO exige os quatro comandos — o parser só recusa chave
desconhecida. Com N linguagens (Ruby sem typecheck, Go sem lint, Python sem
build) isto deixa de ser exceção e vira o caso comum.

O conserto é uma linha: `NOT_CONFIGURED` entra no mesmo classificador que já
separa "não conseguiu rodar" de "reprovou" (`_l1_infra_gates`), e o item escala
nomeando o estágio — a saída é declarar o comando ou pôr o nome em
`disabled_stages`, e as duas são decisões humanas no repositório.
"""
from __future__ import annotations

from dse_contracts import GateStatus, L1Finding
from dse_orchestrator.workflows import _l1_infra_gates


def test_a_stage_with_no_command_is_classified_as_infra_not_as_a_failure():
    findings = [
        L1Finding(check="lint", passed=True, status=GateStatus.PASS, detail="", summary="ok"),
        L1Finding(check="build", passed=False, status=GateStatus.NOT_CONFIGURED,
                  detail="no build command in the trusted manifest abc123:.dse/validation.json",
                  summary="no build command in the trusted manifest"),
    ]
    assert "build" in _l1_infra_gates(findings), (
        "estágio sem comando declarado virou trabalho de Coder — que não pode "
        "consertar, porque o manifesto é lido do base SHA imutável"
    )


def test_a_real_failure_is_still_the_coder_s_job():
    """A rede: teste reprovando continua comprando turno de Coder."""
    findings = [
        L1Finding(check="test", passed=False, status=GateStatus.FAIL,
                  detail="2 failing assertions", summary="2 test(s) failed"),
    ]
    assert _l1_infra_gates(findings) == []


def test_an_error_stage_is_still_infra():
    findings = [
        L1Finding(check="lint", passed=False, status=GateStatus.ERROR,
                  detail="the process was killed (exit=137)", summary="lint could not run"),
    ]
    assert _l1_infra_gates(findings) == ["lint"]


def test_the_status_survives_the_serialization_boundary():
    """Payload decodificado traz `status` como string crua; o classificador lê
    os dois lados (a mesma disciplina do `_tester_infra_outcome`)."""
    class _Cru:
        check = "typecheck"
        status = "NOT_CONFIGURED"
        passed = False

    assert _l1_infra_gates([_Cru()]) == ["typecheck"]
