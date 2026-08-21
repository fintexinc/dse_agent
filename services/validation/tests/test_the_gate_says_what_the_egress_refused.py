"""O gate dizia "não consegui ler o lint" e ficava nisso — a causa estava no
banco, numa tabela que ninguém correlacionava.

Cronologia real de 2026-08-21, e ela custou três releases:

  00:04  lint ERROR "no diagnostic matched the expected format"
         (spotless morreu; a plataforma não disse por quê)
  00:20  abro `download.eclipse.org` na allowlist          -> falha igual
  00:40  faço a JVM usar o proxy (rc.108)                  -> falha igual
  00:46  `egress_denied {"host": "archive.eclipse.org"}`   <- a resposta, no
         banco, o tempo todo

O P2 do Eclipse consulta DOIS hosts. A rc.108 funcionou — a JVM passou a ir
pelo proxy — e o proxy recusou o segundo. A informação existia desde a primeira
rodada e nada a colocava diante de quem lia a escalada.

Um gate que morre por rede tem de dizer QUE rede. O `egress_denied` do proxy
não carrega work_item_id (o proxy não sabe de quem é a conexão), então a
correlação é por JANELA DE TEMPO e o texto diz isso — "durante esta rodada",
nunca "deste item".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dse_contracts import GateStatus
from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import lint_check
from dse_validation.sandbox_exec import ExecResult


class _Sandbox:
    def __init__(self, result: ExecResult):
        self._r = result

    def run(self, argv, cwd=None, timeout=None):  # noqa: ARG002
        return self._r


_SPOTLESS = (
    "[ERROR] Failed to execute goal com.diffplug.spotless:spotless-maven-plugin:"
    "2.43.0:check (default-cli) on project ce: java.io.IOException: Failed to "
    "load eclipse jdt formatter\n"
)


def _cfg() -> L1Config:
    return L1Config._from_manifest_payload(
        {"version": 1, "commands": {"lint": ["./mvnw", "-B", "-q", "spotless:check"]}},
        source="test")


def _run(denials, started_at=None):
    sandbox = _Sandbox(ExecResult(argv=["x"], returncode=1, stdout=_SPOTLESS, stderr=""))
    return lint_check(
        sandbox, _cfg(), changed_files={"a.java"},
        egress_denials=denials,
        started_at=started_at or datetime.now(timezone.utc) - timedelta(seconds=30),
    )


def test_an_unreadable_gate_names_the_host_the_egress_refused():
    finding = _run([("archive.eclipse.org", 443)])

    assert finding.status is GateStatus.ERROR
    assert "archive.eclipse.org:443" in finding.detail
    assert "egress" in finding.detail.lower()


def test_the_wording_never_claims_the_denial_belongs_to_this_item():
    """O proxy não carrega work_item_id, então a correlação é temporal. Dizer
    "deste item" seria a plataforma afirmando o que não mediu — dois itens em
    paralelo compartilham a janela."""
    finding = _run([("archive.eclipse.org", 443)])

    baixo = finding.detail.lower()
    assert "during this run" in baixo or "while this gate ran" in baixo


def test_several_hosts_are_listed_once_each():
    finding = _run([("archive.eclipse.org", 443), ("archive.eclipse.org", 443),
                    ("repo.spring.io", 443)])

    assert finding.detail.count("archive.eclipse.org:443") == 1
    assert "repo.spring.io:443" in finding.detail


def test_no_denial_leaves_the_message_exactly_as_it_was():
    """Sem negativa, nada de linha vazia nem de "nenhum host recusado": ruído
    num texto que o operador lê no Slack é custo, não informação."""
    finding = _run([])

    assert "egress" not in finding.detail.lower()


def test_a_readable_gate_does_not_get_the_note_even_with_denials():
    """A nota existe para explicar veredito IMPOSSÍVEL. Um lint que reprovou
    com diagnóstico legível não é sobre rede — mencionar egress ali manda o
    leitor para o lado errado."""
    sandbox = _Sandbox(ExecResult(
        argv=["x"], returncode=1,
        stdout="src/A.java:3:1: E501 line too long\n", stderr=""))
    finding = lint_check(sandbox, _cfg(), changed_files={"src/A.java"},
                         egress_denials=[("archive.eclipse.org", 443)],
                         started_at=datetime.now(timezone.utc))

    assert finding.status is GateStatus.FAIL
    assert "egress" not in finding.detail.lower()
