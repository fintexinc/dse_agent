"""A activity do autofix recebe o handle CRU e precisa decodificá-lo.

Medido em produção (wi_995ed37f1cc, 2026-08-21): a activity foi agendada, rodou
e devolveu

    autofix errored: SandboxHandle without container_id —
    WS-C has not provisioned the real sandbox yet

O workflow manda `{"sandbox": input.sandbox_handle}`, que é um DICT. O
`run_l1_pipeline` funciona porque o payload dele passa por `RunL1PipelineInput`
antes, e o `executor_for_handle` recebe o modelo já decodificado. A minha
activity passava o dict direto — e o `container_id` sumia no caminho.

O segundo defeito é pior que o primeiro: eu envolvi tudo num `except Exception`
que devolve `ran=False` e guarda o motivo num campo que **só a história do
Temporal mostra**. Da superfície, "o autofix não rodou" era indistinguível de
"o repo não declarou o comando". Um passo oportunista pode falhar em silêncio
para o LAÇO; não pode falhar em silêncio para o OPERADOR.
"""
from __future__ import annotations

from dse_validation.activities import _lint_autofix


def test_a_raw_handle_dict_is_decoded_before_it_reaches_the_executor(monkeypatch):
    """O que o workflow manda é dict. Se a activity não decodificar, o
    `container_id` some e o executor recusa o handle."""
    vistos: dict = {}

    def _fake_executor(handle, repo_dir="/workspace/repo"):
        vistos["container_id"] = getattr(handle, "container_id", None)

        class _Exec:
            def run(self, argv, cwd=None, timeout=None):
                from dse_validation.sandbox_exec import ExecResult
                return ExecResult(argv=argv, returncode=0, stdout="", stderr="")

        return _Exec()

    monkeypatch.setattr("dse_validation.activities.executor_for_handle", _fake_executor)
    monkeypatch.setattr(
        "dse_validation.config.L1Config.from_trusted_manifest",
        classmethod(lambda cls, *a, **kw: cls(lint_fix_cmd=["fmt"])),
    )

    r = _lint_autofix({
        "sandbox": {"container_id": "dse-sbx-wi-x", "work_item_id": "wi_x",
                    "tenant_id": "t", "branch": "b"},
        "base_sha": "a" * 40,
        "failed_checks": ["lint"],
    })

    assert vistos["container_id"] == "dse-sbx-wi-x", (
        "o executor recebeu um dict cru: o handle não foi decodificado"
    )
    assert r["ran"] is True


def test_an_errored_autofix_is_never_invisible(monkeypatch):
    """Falhar em silêncio para o LAÇO é o desenho (o turno de modelo é o
    caminho de sempre). Falhar em silêncio para o OPERADOR foi o defeito: o
    motivo real ficou só na história do Temporal, e da superfície "não rodou"
    era igual a "o repo não declarou"."""
    auditado: list = []

    def _boom(handle, repo_dir="/workspace/repo"):
        raise RuntimeError("sandbox unreachable")

    monkeypatch.setattr("dse_validation.activities.executor_for_handle", _boom)
    monkeypatch.setattr("dse_validation.activities.audit_emit",
                        lambda **kw: auditado.append(kw))

    r = _lint_autofix({"sandbox": {"container_id": "c"}, "base_sha": "a" * 40,
                       "failed_checks": ["lint"], "work_item_id": "wi_x",
                       "tenant_id": "t"})

    assert r["ran"] is False and r["changed"] is False
    assert auditado, "um autofix que estourou tem de deixar linha no ledger"
    assert auditado[0]["action"] == "lint_autofix_failed"
    assert "sandbox unreachable" in str(auditado[0]["details"])
