"""Post-Coder prune reconciled with the advisory L1 (2026-07-22).

Context: `expected_files` no longer fails the diff at L1 (it is advisory — the
Planner guesses the files from the issue, before reading the code). The
deterministic post-turn prune, which used to delete EVERY new file outside the
plan, would now delete a NEW and legitimate source file that the fix had to
create — silently, before the commit. `_prune_disposable_artifacts` now deletes
ONLY obvious CLI junk (report/log/scratch); legitimate new source survives.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from sandbox_runtime.activities import _prune_disposable_artifacts


def _git(ws: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ws, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def repo(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "t@dse.local")
    _git(ws, "config", "user.name", "t")
    with open(os.path.join(ws, "app.js"), "w") as fh:
        fh.write("// base\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "base")
    return ws


def _write(ws: str, rel: str, content: str = "x\n") -> str:
    dest = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(dest) or ws, exist_ok=True)
    with open(dest, "w") as fh:
        fh.write(content)
    return dest


def test_new_source_stays_and_a_spontaneous_report_is_pruned(repo):
    """The canonical case: the fix created a NEW source module outside the plan
    and the CLI spat out a report. The module has to survive; the report must
    not."""
    src = _write(repo, "src/new-module.js", "export const fix = 1;\n")
    report = _write(repo, "BUG_FIX_REPORT.md", "# What I did\n")

    pruned, kept = _prune_disposable_artifacts(
        repo, expected_files=["src/store.js"], work_item_id="wi_x"
    )

    assert os.path.exists(src), "a legitimate new source file must NOT be deleted"
    assert not os.path.exists(report), "a spontaneous CLI report must disappear"
    assert pruned == ["BUG_FIX_REPORT.md"]
    assert kept == ["src/new-module.js"]


def test_multiple_junk_artifacts_are_pruned(repo):
    for rel in ("run.log", "state.tmp", "app.js.orig", "IMPLEMENTATION_SUMMARY.md"):
        _write(repo, rel)
    _write(repo, "src/feature.py", "def f():\n    return 1\n")

    pruned, kept = _prune_disposable_artifacts(
        repo, expected_files=["src/other.py"], work_item_id="wi_x"
    )

    assert sorted(pruned) == ["IMPLEMENTATION_SUMMARY.md", "app.js.orig", "run.log", "state.tmp"]
    assert kept == ["src/feature.py"]
    assert os.path.exists(os.path.join(repo, "src/feature.py"))


def test_a_file_in_the_plan_is_never_pruned_even_if_it_looks_like_junk(repo):
    """If the plan ASKED for the file, it stays — even if it matches a junk pattern."""
    _write(repo, "REPORT.md", "content requested by the plan\n")

    pruned, kept = _prune_disposable_artifacts(
        repo, expected_files=["REPORT.md"], work_item_id="wi_x"
    )

    assert pruned == []
    assert kept == []
    assert os.path.exists(os.path.join(repo, "REPORT.md"))


def test_test_paths_and_demos_of_the_wi_are_exempt(repo):
    _write(repo, "tests/DEBUG_REPORT.md", "report inside tests/\n")
    _write(repo, "demos/wi_x/output.log", "log of the work item demo\n")

    pruned, kept = _prune_disposable_artifacts(
        repo, expected_files=["src/x.py"], work_item_id="wi_x"
    )

    assert pruned == []
    assert kept == []  # neither of them counts as "outside the plan"
    assert os.path.exists(os.path.join(repo, "tests/DEBUG_REPORT.md"))
    assert os.path.exists(os.path.join(repo, "demos/wi_x/output.log"))


def test_a_demo_of_another_wi_is_not_exempt(repo):
    """`demos/<other-wi>/` is NOT this work item's demo — a .log in there is junk."""
    _write(repo, "demos/wi_other/junk.log", "log of another wi\n")

    pruned, _kept = _prune_disposable_artifacts(
        repo, expected_files=["src/x.py"], work_item_id="wi_x"
    )

    assert pruned == ["demos/wi_other/junk.log"]


def test_a_tracked_file_modified_outside_the_plan_is_never_touched(repo):
    """Only NEW files (untracked, `??`) enter the prune. An EXISTING file
    modified outside the plan stays — it is L1/the budget that judges it."""
    with open(os.path.join(repo, "app.js"), "w") as fh:
        fh.write("// modified outside the plan\n")
    _write(repo, "trash.log", "junk\n")

    pruned, kept = _prune_disposable_artifacts(
        repo, expected_files=["src/other.js"], work_item_id="wi_x"
    )

    assert pruned == ["trash.log"]
    assert kept == []  # app.js is tracked → neither pruned nor counted
    assert os.path.exists(os.path.join(repo, "app.js"))
    with open(os.path.join(repo, "app.js")) as fh:
        assert fh.read() == "// modified outside the plan\n"  # the modification stays
    porcelain = _git(repo, "status", "--porcelain")
    assert "app.js" in porcelain and "trash.log" not in porcelain


def test_unavailable_git_deletes_nothing(tmp_path):
    """Best-effort: without a git repo, the prune does not break and deletes
    nothing (L1 is the hard gate)."""
    ws = str(tmp_path / "not-a-repo")
    os.makedirs(ws)
    _write(ws, "BUG_FIX_REPORT.md", "x\n")

    pruned, kept = _prune_disposable_artifacts(ws, expected_files=["a.py"], work_item_id="wi_x")

    assert pruned == []
    assert kept == []
    assert os.path.exists(os.path.join(ws, "BUG_FIX_REPORT.md"))


# ---------------------------------------------------------------------------
# The Coder does NOT own the tests (found on the real run against issue #1)
# ---------------------------------------------------------------------------
from sandbox_runtime.activities import _revert_coder_test_edits


@pytest.fixture()
def repo_with_test(tmp_path):
    ws = str(tmp_path / "ws2")
    os.makedirs(ws)
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "t@dse.local")
    _git(ws, "config", "user.name", "t")
    os.makedirs(os.path.join(ws, "test"))
    # EXISTING test with a shared seed (like the wallet's test/api.test.js)
    with open(os.path.join(ws, "test", "api.test.js"), "w") as fh:
        fh.write("// seed: Market 2026-07-10\nassert(first === 'Market');\n")
    with open(os.path.join(ws, "src.js"), "w") as fh:
        fh.write("// base\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "base")
    return ws


def test_a_coder_edit_to_a_customer_test_now_survives(repo_with_test):
    """FLIP da política (decisão de operador, 2026-08-10). Este teste nasceu
    pinando o revert total do issue #1 (o Coder mudou o seed compartilhado de
    test/api.test.js e quebrou um teste irmão). Hoje a edição de teste de
    CLIENTE sobrevive e entra no diff da PR; a proteção contra o cenário do
    issue #1 mudou de lugar — o teste irmão quebrado reprova o L1 no mesmo
    turno (o Coder vê e conserta), e o diff da PR mostra a edição ao revisor.
    O revert continua existindo SÓ para o instrumento do Tester
    (test_client_spec_edits_survive.py cobre a fronteira)."""
    ws = repo_with_test
    start = _git(ws, "rev-parse", "HEAD").strip()
    with open(os.path.join(ws, "test", "api.test.js"), "w") as fh:
        fh.write("// seed CHANGED: Market 2026-06-10\nassert(first === 'Market');\n")
    with open(os.path.join(ws, "src.js"), "w") as fh:
        fh.write("// fix real\n")

    reverted = _revert_coder_test_edits(ws, start)

    assert reverted == []
    assert "2026-06-10" in open(os.path.join(ws, "test", "api.test.js")).read()
    assert "fix real" in open(os.path.join(ws, "src.js")).read()


def test_a_new_customer_convention_test_by_the_coder_is_kept(repo_with_test):
    """FLIP (2026-08-10): teste novo do Coder em convenção de cliente é
    engenharia legítima e visível no diff. Forja do marcador -dse continua
    removida (test_client_spec_edits_survive.py)."""
    ws = repo_with_test
    start = _git(ws, "rev-parse", "HEAD").strip()
    _write(ws, "test/coder-invented.test.js", "// a test the Coder invented\n")
    reverted = _revert_coder_test_edits(ws, start)
    assert reverted == []
    assert os.path.exists(os.path.join(ws, "test", "coder-invented.test.js"))


def test_revert_does_not_touch_source(repo_with_test):
    ws = repo_with_test
    start = _git(ws, "rev-parse", "HEAD").strip()
    _write(ws, "src/new.js", "export const x = 1;\n")
    with open(os.path.join(ws, "src.js"), "w") as fh:
        fh.write("// changed\n")
    reverted = _revert_coder_test_edits(ws, start)
    assert reverted == []  # no test was touched
    assert os.path.exists(os.path.join(ws, "src", "new.js"))
    assert "changed" in open(os.path.join(ws, "src.js")).read()
