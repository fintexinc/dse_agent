"""The provenance marker stays out of the customer's pull request.

`.claude/.dse-materialized` is ours: it records which skills WE wrote into the
workspace, so a later round can tell "we put this here and may refresh it" from
"the repository ships this and it is untouchable". It has to exist on disk and
it must never be committed.

The host materializer gets this right — `_write_materialized_marker` ends with
`_git_exclude(ws, [_MARKER])`. The in-Pod writer, which is the one that runs on
the k3s driver in production, builds its exclude list from the skill DIRECTORIES
only and then writes the marker without ever excluding it. So `git add -A` picks
it up.

Measured on 2026-09-09, BFA-1092: `skills_materialized` at 12:50:10, the
`checkpoint(base)` commit at 12:50:11, and `.claude/.dse-materialized` shipped in
pull request #524 of the customer's repository — before the Coder had touched
anything.

This is the third time this file escapes. It leaked in PR #792, the whole
materialization was removed in rc.125, and rc.132 reintroduced the in-Pod writer
verbatim — defect included. The test that was supposed to catch it
(`test_skills_in_pod.py::test_guidance_is_excluded_from_git_so_it_never_reaches_the_customer_pr`)
asserts that the string `.claude/.dse-materialized` appears in the generated
script, and it does appear — as the target of the write. It passes either way.

So this one runs the real script against a real git repository and asks git,
not the script, which is the same shape `test_task_branch_marker.py` uses for
the sibling marker `.dse-task-branch`.
"""
from __future__ import annotations

import subprocess

import pytest

from sandbox_runtime import skill_files
from sandbox_runtime.skill_files import materialize_skills_in_pod
from sandbox_runtime.skill_registry import Skill


def _git(ws: str, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=ws, capture_output=True, text=True, check=True
    )
    return out.stdout


def _tracked(ws: str) -> list[str]:
    return _git(ws, "ls-files").split()


def _skill(key: str, body: str = "rule: do the thing") -> Skill:
    return Skill(
        tenant_id="t", skill_key=key, title=f"Title {key}", body=body,
        category="engineering", applies_to=["python"],
    )


def _shell_runner(ws: str):
    """Executes what the Pod would execute, in a real shell, in `ws`."""

    def run(argv, stdin):
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, cwd=ws)
        return p.returncode, p.stdout

    return run


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    _git(str(ws), "init", "-q")
    _git(str(ws), "config", "user.email", "dse@test.local")
    _git(str(ws), "config", "user.name", "DSE Test")
    (ws / "app.py").write_text("x = 1\n")
    _git(str(ws), "add", "-A")
    _git(str(ws), "commit", "-q", "-m", "base")
    # The script cds into the Pod's workspace path, which does not exist here.
    monkeypatch.setattr(skill_files, "_POD_WORKSPACE", str(ws))
    return str(ws)


def test_the_marker_is_written_but_never_tracked(workspace):
    """The whole point: it lands on disk, git never sees it."""
    materialize_skills_in_pod([_skill("writing-frontend-code")], run=_shell_runner(workspace))

    marker = f"{workspace}/.claude/.dse-materialized"
    assert subprocess.run(["test", "-f", marker]).returncode == 0, (
        "the marker must exist on disk — a later round reads it to know which "
        "skills are ours to refresh"
    )
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "coder: deterministic commit", "--allow-empty")
    assert ".claude/.dse-materialized" not in _tracked(workspace), (
        "the provenance marker reached the commit, and from there the customer's "
        "pull request — this is the PR #792 / PR #524 leak"
    )


def test_the_skills_themselves_are_not_tracked_either(workspace):
    """The guidance bodies were already excluded; keep them that way."""
    materialize_skills_in_pod([_skill("reviewing-code")], run=_shell_runner(workspace))
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "coder", "--allow-empty")
    assert not [p for p in _tracked(workspace) if p.startswith(".claude/")], (
        f"something under .claude/ was committed: {_tracked(workspace)}"
    )


def test_the_customers_own_work_still_gets_committed(workspace):
    """The exclude must be surgical: it hides ours, never the customer's."""
    materialize_skills_in_pod([_skill("writing-frontend-code")], run=_shell_runner(workspace))
    with open(f"{workspace}/feature.py", "w") as fh:
        fh.write("def added_by_the_coder():\n    return 1\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "coder")
    assert "feature.py" in _tracked(workspace)


def test_a_second_round_does_not_grow_the_marker(workspace):
    """The host writer merges into a sorted set; the in-Pod one appends. Two
    provisions of the same skill would otherwise keep adding the same line, and
    every rebuild would produce a fresh diff for a file nobody should see."""
    run = _shell_runner(workspace)
    materialize_skills_in_pod([_skill("writing-frontend-code")], run=run)
    first = open(f"{workspace}/.claude/.dse-materialized").read()
    materialize_skills_in_pod([_skill("writing-frontend-code")], run=run)
    second = open(f"{workspace}/.claude/.dse-materialized").read()
    assert second == first, (
        f"the marker grew on the second round:\n--- first ---\n{first}\n--- second ---\n{second}"
    )
