"""Materialization of skills into the workspace (skill_files.py).

Invariants:
  - ONLY materializes into a provisioned (git) workspace — creating the
    directory before the clone made provision_sandbox skip the clone (found in
    wi_17eefa, 2026-07-23); without `.git` => no-op;
  - writes `.claude/skills/<key>/SKILL.md` with name/description frontmatter;
  - a skill COMMITTED in the target repo is never overwritten (repo beats
    registry);
  - a skill materialized by US on a previous run IS updated (marker);
  - everything we materialize goes into `.git/info/exclude` — it never becomes
    a diff;
  - `workspace_skills_note` lists what exists and is empty when there are no
    skills.
"""
from __future__ import annotations

import subprocess

from sandbox_runtime.skill_files import materialize_skills, workspace_skills_note
from sandbox_runtime.skill_registry import Skill


def _skill(key: str, body: str = "guidance") -> Skill:
    return Skill(tenant_id="dev", skill_key=key, title=f"Title {key}", body=body, category="general")


def _git_ws(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def test_non_git_workspace_is_noop(tmp_path):
    """Workspace without `.git` (pre-provision) => NOTHING is created — the
    provision_sandbox clone depends on the directory not existing/staying
    untouched."""
    written = materialize_skills(str(tmp_path / "nonexistent"), [_skill("x")])
    assert written == []
    assert not (tmp_path / "nonexistent").exists()

    (tmp_path / "empty").mkdir()
    assert materialize_skills(str(tmp_path / "empty"), [_skill("x")]) == []
    assert list((tmp_path / "empty").iterdir()) == []


def test_materialize_writes_skill_md(tmp_path):
    ws = _git_ws(tmp_path)
    written = materialize_skills(str(ws), [_skill("handling-money")])
    assert written == ["handling-money"]
    md = ws / ".claude" / "skills" / "handling-money" / "SKILL.md"
    content = md.read_text()
    assert content.startswith("---\nname: handling-money\n")
    assert "description: Title handling-money" in content
    assert content.endswith("guidance")


def test_repo_committed_skill_wins(tmp_path):
    ws = _git_ws(tmp_path)
    committed = ws / ".claude" / "skills" / "repo-own" / "SKILL.md"
    committed.parent.mkdir(parents=True)
    committed.write_text("from the repo — sovereign")

    written = materialize_skills(str(ws), [_skill("repo-own", body="from the registry")])
    assert written == []
    assert committed.read_text() == "from the repo — sovereign"


def test_rematerialization_updates_our_files(tmp_path):
    ws = _git_ws(tmp_path)
    materialize_skills(str(ws), [_skill("evolving", body="v1")])
    written = materialize_skills(str(ws), [_skill("evolving", body="v2")])
    assert written == ["evolving"]
    md = ws / ".claude" / "skills" / "evolving" / "SKILL.md"
    assert md.read_text().endswith("v2")


def test_git_exclude_keeps_skills_out_of_diff(tmp_path):
    ws = _git_ws(tmp_path)
    (ws / "app.py").write_text("print('x')\n")

    materialize_skills(str(ws), [_skill("guarded")])

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ws, check=True, capture_output=True, text=True
    ).stdout
    assert ".claude" not in status  # excluded locally — the Coder's commit never drags it in
    assert "app.py" in status


def test_workspace_skills_note(tmp_path):
    ws = _git_ws(tmp_path)
    assert workspace_skills_note(str(ws)) == ""
    materialize_skills(str(ws), [_skill("money"), _skill("pii")])
    note = workspace_skills_note(str(ws))
    assert "MANDATORY guidance" in note
    assert ".claude/skills/money/SKILL.md" in note
    assert ".claude/skills/pii/SKILL.md" in note
