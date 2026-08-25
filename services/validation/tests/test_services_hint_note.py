"""Um `connection refused` sem `services` declarado não se conserta editando
teste — e ninguém dizia isso.

O sandbox não tem docker (testcontainers impossível) e o egress é proxy HTTP
(protocolo Postgres/Redis não atravessa). Uma suite que precisa de banco morre
em ECONNREFUSED, o gate publica FAIL, e o laço de fix gasta turnos pagos
editando asserções — quando o dono do conserto é o MANIFESTO (`services` +
`prepare`). A nota aponta o dono certo. Ela ENRIQUECE o detail e nunca muda o
status: o veredito continua o que foi medido.

Fail-closed da afirmação: com `services` declarado a nota some — dizer
"declare services" para quem declarou mandaria o laço na direção errada.
"""
from __future__ import annotations

from dse_contracts import GateStatus

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import services_hint_note
from dse_validation.l1.quality_checks import test_check as run_test_check
from dse_validation.sandbox_exec import ExecResult


class _CannedSandbox:
    def __init__(self, result: ExecResult):
        self._result = result

    def run(self, argv, timeout=None):  # noqa: ARG002 - signature parity
        return self._result


_REFUSED_RED = (
    "FAIL src/db.test.ts\n"
    "  Error: connect ECONNREFUSED 127.0.0.1:5432\n"
    "1 failed, 2 passed\n"
)


def test_the_note_names_the_manifest_and_the_dead_end():
    nota = services_hint_note(_REFUSED_RED, services_declared=False)
    assert "services" in nota
    assert ".dse/validation.json" in nota
    assert "editing the test will not create a database" in nota


def test_the_note_is_silent_when_services_are_declared_or_nothing_was_refused():
    assert services_hint_note(_REFUSED_RED, services_declared=True) == ""
    assert services_hint_note(
        "1 failed: expected 2 to be 3", services_declared=False
    ) == ""
    # O dialeto Go/psql escreve minúsculo — o marcador não é case-sensitive.
    assert services_hint_note(
        "dial tcp 127.0.0.1:6379: connect: connection refused",
        services_declared=False,
    ) != ""


def test_the_l1_test_gate_appends_the_note_and_never_changes_the_status():
    canned = _CannedSandbox(
        ExecResult(argv=["x"], returncode=1, stdout=_REFUSED_RED, stderr="")
    )
    sem = run_test_check(canned, L1Config(test_cmd=["npm", "test"]))
    assert sem.status is GateStatus.FAIL, "a nota não muda o veredito"
    assert "editing the test will not create a database" in sem.detail

    com = run_test_check(
        canned,
        L1Config(test_cmd=["npm", "test"], services_declared=frozenset({"postgres"})),
    )
    assert com.status is GateStatus.FAIL
    assert "editing the test will not create a database" not in com.detail
