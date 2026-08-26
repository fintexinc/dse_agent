"""O secret scan julga A MUDANÇA, não a dívida do repositório.

Medido em wi_a5a395f8 (glide-path-planner-93, 2026-08-25): o gate reprovou o
item com "82 possible secret(s) detected". Rodando o MESMO scanner sobre o
`main` limpo — nenhuma linha do DSE — saem 87 achados: 65 deles em arquivos
`.test.ts`/fixtures (senhas de teste), o resto em `dist/` compilado e em docs.
Zero introduzidos pelo item. E `secret_scan` é gate de PLATAFORMA: o repo não
pode desligá-lo (nem deve), então não havia saída pelo manifesto — o item
morria por construção, em qualquer repositório maduro.

Este é o MESMO defeito de classe que o lint (`_only_in_changed_files`, contra
os 262 erros de `.spec.ts` do testbed Angular) e o test (`inherited` /
NOT_OUR_FAILURE) já corrigiram. O secret scan era o último gate julgando a
árvore inteira.

A proteção real não muda: se O DIFF introduzir segredo, o gate reprova — é
disso que a plataforma tem que proteger. A dívida pré-existente continua
VISÍVEL (o summary a nomeia), só não é mais imputada a quem não a criou.
"""
from __future__ import annotations

import json

from dse_contracts import GateStatus
from dse_validation.l1.secret_scan import secret_scan_check
from dse_validation.sandbox_exec import ExecResult


class _CannedSandbox:
    def __init__(self, findings: list[dict]):
        self._payload = json.dumps({"findings": findings})

    def run(self, argv, cwd=None, timeout: int = 300) -> ExecResult:
        return ExecResult(argv=argv, returncode=0, stdout=self._payload, stderr="")


def _achado(path: str, kind: str = "high_entropy_assignment") -> dict:
    return {"kind": kind, "file": path, "line": 3, "snippet": "x = '...'"}


_HERDADOS = [
    _achado("./apps/api/src/provisioning/provisioning.controller.test.ts"),
    _achado("./apps/web/src/pages/DesignSystem.tsx"),
    _achado("./apps/api/dist/provisioning/gotrue-admin.client.test.js"),
]


def test_pre_existing_debt_does_not_fail_the_item_that_did_not_create_it():
    sandbox = _CannedSandbox(_HERDADOS)
    finding = secret_scan_check(sandbox, changed_files={"apps/api/src/health.controller.ts"})

    assert finding.passed is True, (
        "o item foi reprovado por segredo que já estava no repositório — o gate "
        "não tem conserto possível pelo diff e o item morre por construção"
    )
    assert finding.status is GateStatus.PASS


def test_the_debt_is_still_named_never_silently_swallowed():
    """Não reprovar não é fingir que não existe: o operador precisa saber."""
    sandbox = _CannedSandbox(_HERDADOS)
    finding = secret_scan_check(sandbox, changed_files={"apps/api/src/health.controller.ts"})

    assert "3" in finding.summary, f"a contagem herdada sumiu: {finding.summary}"
    assert "pre-existing" in finding.summary.lower() or "outside" in finding.summary.lower()


def test_a_secret_introduced_by_this_change_still_fails():
    """A proteção que o gate existe para dar — intacta."""
    sandbox = _CannedSandbox(
        _HERDADOS + [_achado("./apps/api/src/health.controller.ts", "aws_access_key_id")]
    )
    finding = secret_scan_check(sandbox, changed_files={"apps/api/src/health.controller.ts"})

    assert finding.passed is False
    assert finding.status is GateStatus.FAIL
    assert "health.controller.ts" in finding.detail


def test_without_a_diff_every_finding_counts():
    """Mesma regra do lint: sem diff conhecido, perder um achado real é pior
    que reportar um que não é nosso."""
    finding = secret_scan_check(_CannedSandbox(_HERDADOS), changed_files=None)
    assert finding.passed is False


def test_a_clean_change_in_a_clean_repo_still_passes():
    finding = secret_scan_check(_CannedSandbox([]), changed_files={"a.ts"})
    assert finding.passed is True
    assert finding.summary == "no secret/token detected"
