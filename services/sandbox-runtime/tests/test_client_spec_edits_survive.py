"""Decisão de operador (2026-08-10): o DSE PODE alterar specs pré-existentes
do cliente — a mudança entra no diff da PR e o revisor humano a vê ("o DSE
nunca aprova o próprio trabalho" continua valendo no gate da PR).

O que o revert pós-turno continua protegendo é o INSTRUMENTO DE VERIFICAÇÃO
do próprio laço: as specs que o Tester deste item autorou. Sem isso, o Coder
pode enfraquecer no meio do laço o teste que valida o próprio código — a
separação Coder/Tester deixaria de existir. A fronteira é AUTORIA, decidida
pelo mesmo oráculo do reauthor do Tester: o histórico git (commits da
plataforma são subject-prefixados por scoped_git; arquivo com commit humano
na história é do cliente), com o marcador -dse como fallback sem histórico.

Contexto medido que motivou: wi_d41d893b parqueou 2x nas mesmas três specs de
cliente; o retry humano "you can rewrite the specs" foi mecanicamente anulado
pelo revert e o item morreu por teto de orçamento.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from sandbox_runtime.workspace_hygiene import revert_test_edits


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q", "-b", "main")
    (ws / "src").mkdir()
    (ws / "test").mkdir()
    (ws / "src" / "code.ts").write_text("export const x = 1\n")
    (ws / "test" / "client.spec.ts").write_text("it('old shape', old)\n")
    _git(ws, "add", "-A")
    # commit HUMANO: subject sem prefixo de plataforma → arquivo é do cliente
    _git(ws, "commit", "-q", "-m", "initial customer tests")
    # commit da PLATAFORMA: a spec do Tester deste item (subject prefixado)
    (ws / "test" / "retire-dse.spec.ts").write_text("it('tester verdict', v)\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "tester(wi_x): authored specs")
    return ws


def test_a_coder_edit_to_a_customer_spec_survives_the_turn(tmp_path):
    ws = _workspace(tmp_path)
    base_sha = _git(ws, "rev-parse", "HEAD")
    (ws / "test" / "client.spec.ts").write_text("it('new shape', updated)\n")

    reverted = revert_test_edits(str(ws), base_sha)

    assert "test/client.spec.ts" not in reverted, (
        "spec pré-existente do CLIENTE editada pelo Coder tem que sobreviver — "
        "decisão de operador 2026-08-10; a supervisão é o diff da PR"
    )
    assert (ws / "test" / "client.spec.ts").read_text() == "it('new shape', updated)\n"


def test_the_testers_own_spec_stays_protected(tmp_path):
    """PIN da separação Coder/Tester: a spec que o TESTER autorou é o
    instrumento que valida o código do Coder — o Coder não a reescreve."""
    ws = _workspace(tmp_path)
    base_sha = _git(ws, "rev-parse", "HEAD")
    (ws / "test" / "retire-dse.spec.ts").write_text("it('weakened', pass)\n")

    reverted = revert_test_edits(str(ws), base_sha)

    assert "test/retire-dse.spec.ts" in reverted
    assert (ws / "test" / "retire-dse.spec.ts").read_text() == "it('tester verdict', v)\n"


def test_a_new_coder_authored_test_is_kept_but_a_forged_dse_file_is_removed(tmp_path):
    """Arquivo NOVO de teste do Coder (convenção de cliente) entra no diff —
    escrever teste novo é engenharia legítima e visível. Arquivo novo com o
    MARCADOR do Tester (-dse) é forja de autoria: removido."""
    ws = _workspace(tmp_path)
    base_sha = _git(ws, "rev-parse", "HEAD")
    (ws / "test" / "extra.spec.ts").write_text("it('new coverage', n)\n")
    (ws / "test" / "forged-dse.spec.ts").write_text("it('fake tester', f)\n")

    reverted = revert_test_edits(str(ws), base_sha)

    assert "test/forged-dse.spec.ts" in reverted
    assert not (ws / "test" / "forged-dse.spec.ts").exists()
    assert "test/extra.spec.ts" not in reverted
    assert (ws / "test" / "extra.spec.ts").exists()
