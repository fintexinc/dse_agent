"""O pós-turno não desfaz edição de teste. Nenhuma. De ninguém.

Decisão de operador de 2026-08-10, agora inteira: **o DSE pode alterar qualquer
teste**, e a supervisão é o diff da PR. A primeira metade já valia (spec de
CLIENTE sobrevivia desde a rc.76); esta remove a outra — a "proteção de
instrumento", que revertia a edição do Coder nas specs autoradas pelo Tester.

Por que a metade que sobrava tinha de sair: ela era a razão de existir do
`reauthor` inteiro. Como o Coder não podia consertar a spec do Tester, o
sistema mandava o *Tester* reescrevê-la — e isso arrastava o parque
`spec_conflict`, o signal, os botões do Slack, a rota do dispatcher e a pinça
de no-op. Medido no wi_3355102d: duas ordens automáticas saíram, o Tester
reescreveu no mesmo estilo e o item morreu no `coder_not_converging`. A
auditoria de fluxo ainda achou, no mesmo mecanismo, uma ordem que sobrevive a
`continue_as_new` e trava o item em `UnboundLocalError` com retry infinito.

O que ISTO fixa é a fronteira nova, e ela é uma regra só: nenhum arquivo de
teste volta atrás no pós-turno. O que continua valendo tem pin aqui embaixo —
o prune segue sem apagar teste, e a higiene de lockfile segue intacta.

O que NÃO está aqui, deliberadamente: a proibição de apagar cobertura. Ela
continua existindo, mas como INSTRUÇÃO ao agente (`plan_constraints_note`,
`_tester_failure_context`) e como revisão humana na PR — não como mecanismo
que desfaz escrita pelas costas de quem escreveu.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dse_contracts import CheckpointOpRequest, PostTurnRequest, WorkspaceBootstrapRequest

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.gitops import bootstrap_workspace, checkpoint_workspace  # noqa: E402
from agent_runner.postturn import run_post_turn  # noqa: E402

_WI = "wi-noreverts"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _bootstrap(tmp_path):
    req = WorkspaceBootstrapRequest(
        work_item_id=_WI,
        branch=f"dse/{_WI}",
        workspace_dir=str(tmp_path / "workspace"),
        checkpoint_path=str(tmp_path / "checkpoint.git"),
    )
    res = bootstrap_workspace(req)
    assert not res.failed
    return req, res


def _post_turn(req, boot, ws, expected=("src/app.py",)):
    return run_post_turn(
        PostTurnRequest(
            work_item_id=_WI,
            branch=req.branch,
            turn_start_sha=boot.sha,
            commit_message=f"coder({_WI}): implement",
            expected_files=list(expected),
            workspace_dir=str(ws),
        )
    )


def test_the_coder_may_edit_the_spec_the_tester_wrote_for_this_very_item(tmp_path):
    """O caso que sustentava o reauthor inteiro.

    A spec tem no histórico um commit `tester(<ESTE work_item_id>)` — era
    exatamente essa a assinatura que o oráculo de instrumento procurava para
    reverter. Agora a edição fica, e a PR revisa."""
    req, boot = _bootstrap(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "tests").mkdir(exist_ok=True)
    spec = ws / "tests" / "fee_test.py"
    spec.write_text("def test_fee(): assert fee() == 3\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", f"tester({_WI}): authored coverage for this task")
    base = _git(ws, "rev-parse", "HEAD")

    spec.write_text("def test_fee(): assert fee() == 4  # a regra mudou\n")

    post = run_post_turn(
        PostTurnRequest(
            work_item_id=_WI, branch=req.branch, turn_start_sha=base,
            commit_message=f"coder({_WI}): implement", expected_files=["src/app.py"],
            workspace_dir=str(ws),
        )
    )

    assert not post.failed
    assert "a regra mudou" in spec.read_text(), (
        "a edição do Coder no instrumento deste laço foi desfeita — é a "
        "proteção que ainda obrigava o reauthor a existir"
    )
    assert "tests/fee_test.py" in post.files_changed, (
        "a mudança tem que entrar no commit para chegar ao diff da PR, que é "
        "onde a supervisão passou a morar"
    )


def test_a_file_forging_the_tester_marker_is_no_longer_special(tmp_path):
    """O marcador `-dse` deixa de significar posse.

    Ele existia para o Tester reservar um caminho próprio, e o pós-turno
    apagava arquivo novo que o forjasse. Sem posse de teste, um arquivo novo
    com esse nome é só um arquivo novo — e apagá-lo seria destruir trabalho."""
    req, boot = _bootstrap(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(exist_ok=True)
    (ws / "src" / "app.py").write_text("X = 1\n")
    (ws / "tests").mkdir(exist_ok=True)
    (ws / "tests" / "test_forged-dse.py").write_text("def test_f(): pass\n")

    post = _post_turn(req, boot, ws)

    assert not post.failed
    assert (ws / "tests" / "test_forged-dse.py").exists()
    assert "tests/test_forged-dse.py" in post.files_changed


def test_a_new_test_in_the_customers_convention_still_stays(tmp_path):
    """PIN de polaridade (já valia antes, tem de continuar valendo)."""
    req, boot = _bootstrap(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(exist_ok=True)
    (ws / "src" / "app.py").write_text("X = 1\n")
    (ws / "tests").mkdir(exist_ok=True)
    (ws / "tests" / "test_smuggled.py").write_text("def test_x(): pass\n")

    post = _post_turn(req, boot, ws)

    assert not post.failed
    assert (ws / "tests" / "test_smuggled.py").exists()
    assert "tests/test_smuggled.py" in post.files_changed


def test_the_rest_of_the_hygiene_is_untouched(tmp_path):
    """PIN: sai o revert, ficam o prune e o lockfile — e o prune continua sem
    apagar caminho de teste (é a isenção que impede o prune de virar o revert
    por outro nome)."""
    req, boot = _bootstrap(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(exist_ok=True)
    (ws / "src" / "app.py").write_text("X = 1\n")
    (ws / "BUG_FIX_REPORT.md").write_text("spontaneous report\n")
    (ws / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    (ws / "tests").mkdir(exist_ok=True)
    (ws / "tests" / "test_out_of_plan.py").write_text("def test_x(): pass\n")

    post = _post_turn(req, boot, ws)

    assert not post.failed
    assert post.pruned == ["BUG_FIX_REPORT.md"]
    assert post.restored_lockfiles == ["package-lock.json"]
    assert (ws / "tests" / "test_out_of_plan.py").exists(), (
        "o prune isenta caminho de teste; sem isso ele viraria o revert com "
        "outro nome"
    )
    ck = checkpoint_workspace(
        CheckpointOpRequest(
            work_item_id=_WI, branch=req.branch, phase="verify", workspace_dir=str(ws)
        )
    )
    assert ck.sha == post.sha, "o push escopado continua chegando ao checkpoint"
