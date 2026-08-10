"""O oráculo de autoria não pode depender da PROFUNDIDADE do clone.

`_is_tester_instrument` decide se o pós-turno reverte a edição do Coder. Hoje ele
pergunta: "todos os commits deste arquivo são da plataforma, e algum é `tester(`?".

O clone do sandbox é RASO — `--depth 50` nos dois caminhos (`repo_clone.py` e
`agent-runner/gitops.py`). Numa spec de cliente cujos commits humanos ficaram fora
dessa janela, o `git log` mostra APENAS o commit `tester(` que este item acabou de
fazer — e o oráculo conclui "instrumento", revertendo em silêncio uma edição que a
política de 2026-08-10 permite.

Isso já era um defeito latente. A decisão desta sessão (o Coder passa a editar specs
de cliente rotineiramente, sem parar para perguntar) **aumenta a exposição**: mais
edições de spec = mais chances de cair nele, e o sintoma é silencioso — a edição some
e o item queima uma rodada sem ninguém saber por quê. Foi exatamente esse formato de
falha que custou o cap inteiro no wi_fadd43185.

O oráculo certo não é "só commits de plataforma" e sim **"este laço escreveu este
arquivo"** — e o repositório já carrega essa informação: todo commit da plataforma é
`<stage>(<work_item_id>): …`. Perguntar pelo id DESTE item é imune à profundidade do
clone, porque o commit em questão foi feito agora, dentro do sandbox.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from sandbox_runtime.workspace_hygiene import _has_tester_marker, revert_test_edits

_WI = "wi_abc123"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _shallow_workspace(tmp_path: Path) -> Path:
    """Uma spec de CLIENTE cujo histórico humano ficou fora do clone, e que o
    Tester deste item tocou — o cenário exato do `--depth 50`."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q", "-b", "main")
    (ws / "test").mkdir()
    spec = ws / "test" / "client.spec.ts"
    spec.write_text("it('old', old)\n")
    _git(ws, "add", "-A")
    # O commit HUMANO existe no repositório real, mas NÃO no clone raso — então
    # aqui ele simplesmente não é criado: o que o Pod enxerga começa depois.
    _git(ws, "commit", "-q", "-m", "tester(%s): authored coverage for this task" % _WI)
    return ws


def test_a_client_spec_beyond_the_clone_depth_is_not_mistaken_for_instrument(tmp_path):
    ws = _shallow_workspace(tmp_path)
    base_sha = _git(ws, "rev-parse", "HEAD")
    (ws / "test" / "client.spec.ts").write_text("it('updated for the new behaviour', x)\n")

    reverted = revert_test_edits(str(ws), base_sha, work_item_id="wi_OUTRO_ITEM")

    assert "test/client.spec.ts" not in reverted, (
        "o arquivo foi escrito pelo Tester de OUTRO item; para este laço ele é "
        "código pré-existente, e a edição do Coder tem que sobreviver — senão o "
        "clone raso reverte em silêncio o que a política permite"
    )


def test_the_instrument_of_this_very_loop_is_still_protected(tmp_path):
    """PIN: a proteção que importa fica MAIS precisa, não mais frouxa — o que
    ESTE laço escreveu para julgar o Coder continua intocável por ele."""
    ws = _shallow_workspace(tmp_path)
    base_sha = _git(ws, "rev-parse", "HEAD")
    (ws / "test" / "client.spec.ts").write_text("it('weakened', pass)\n")

    reverted = revert_test_edits(str(ws), base_sha, work_item_id=_WI)

    assert "test/client.spec.ts" in reverted
    assert "authored coverage" not in (ws / "test" / "client.spec.ts").read_text()


def test_the_name_marker_does_not_match_dse_in_the_middle_of_a_word():
    """`-dse` em qualquer posição casava — uma spec de cliente chamada
    `parse-dserializer.spec.ts` era tratada como instrumento e revertida. O
    marcador é uma CONVENÇÃO de sufixo do stem, não uma substring."""
    assert _has_tester_marker("test/retire-dse.spec.ts")
    assert _has_tester_marker("test/retire_dse.spec.ts")
    assert not _has_tester_marker("test/parse-dserializer.spec.ts"), (
        "substring no meio da palavra não é o marcador do Tester"
    )
    assert not _has_tester_marker("test/client.spec.ts")
