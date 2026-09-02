"""Materialization of skills into the sandbox workspace (console ⇄ engine
integration).

The console (dse_console_pane) is the central skill store; the per-repo
checkbox (`skill_registry.repo_scope`, migration 0029) decides what runs where.
This module writes the served skills as Claude-Agent-Skill files in the
workspace — `.claude/skills/<skill_key>/SKILL.md` — BEFORE the agent turn:

  - `ClaudeAgentSubstrate` loads them natively (`setting_sources=["project"]`
    — project/workspace only, never the host user's settings);
  - any other substrate reaches them by reading files, guided by the
    instruction note (`workspace_skills_note`).

Deliberate rules:
  - a skill already COMMITTED in the target repo (existing
    `.claude/skills/<key>/`) BEATS the registry version — a convention
    versioned alongside the code is sovereign; nothing gets overwritten.
  - everything we materialize goes into `.git/info/exclude` (a LOCAL exclude,
    never committed) — the Coder's deterministic commit (`ScopedGitSession`)
    never drags guidance into the PR.
"""
from __future__ import annotations

import re
from pathlib import Path

from .skill_registry import Skill

_SKILLS_SUBDIR = Path(".claude") / "skills"


def _safe_key(skill_key: str) -> str:
    """Directory name derived from the skill_key — never a path traversal."""
    key = re.sub(r"[^a-zA-Z0-9._-]", "-", skill_key).strip(".-") or "skill"
    return key[:64]


def _frontmatter(skill: Skill) -> str:
    description = " ".join(skill.title.split()) or skill.skill_key
    return f"---\nname: {_safe_key(skill.skill_key)}\ndescription: {description}\n---\n\n"


def _git_exclude(workspace_dir: Path, rel_paths: list[str]) -> None:
    """Register the materialized paths in the clone's LOCAL exclude (this is not
    the repo's .gitignore — it never becomes a diff). With no git repo
    (tests/fixtures), no-op."""
    info_dir = workspace_dir / ".git" / "info"
    if not (workspace_dir / ".git").is_dir():
        return
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude = info_dir / "exclude"
    existing = exclude.read_text() if exclude.is_file() else ""
    lines = [p for p in rel_paths if p not in existing]
    if lines:
        exclude.write_text(existing.rstrip("\n") + ("\n" if existing else "") + "\n".join(lines) + "\n")


def plan_materialization(
    skills: list[Skill],
    *,
    existing_skill_keys: set[str],
    marker_entries: set[str],
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Decide WHAT to write, without touching a filesystem.

    Both writers go through here — the host one below and the in-Pod one used by
    the K8s runtime — because the interesting part of this feature is not the
    writing, it is the rule that a skill COMMITTED in the target repo beats the
    registry copy. Two implementations of that rule would drift, and the drift
    would be invisible: the Coder would silently get a different set of
    conventions depending on which runtime it happened to run under.

    Returns (files, excludes, keys):
      files    — [(relative path, full content)] to write
      excludes — relative directories to add to .git/info/exclude
      keys     — the skill_keys actually materialized
    """
    files: list[tuple[str, str]] = []
    excludes: list[str] = []
    keys: list[str] = []
    for skill in skills:
        key = _safe_key(skill.skill_key)
        rel_dir = f"{_SKILLS_SUBDIR.as_posix()}/{key}/"
        if key in existing_skill_keys and rel_dir not in marker_entries:
            # Present but not written by us => committed in the target repo.
            # Sovereign; leave it alone.
            continue
        files.append((f"{_SKILLS_SUBDIR.as_posix()}/{key}/SKILL.md", _frontmatter(skill) + skill.body))
        excludes.append(rel_dir)
        keys.append(skill.skill_key)
    return files, excludes, keys


def materialize_skills(workspace_dir: str, skills: list[Skill]) -> list[str]:
    """Write each served skill to the workspace's `.claude/skills/<key>/SKILL.md`.
    Idempotent; returns the keys actually materialized (skills already present
    in the target repo are skipped — repo beats registry).

    GUARD (found in the real 2026-07-23 run, wi_17eefa): only materialize into
    an ALREADY PROVISIONED (git) workspace. Creating the directory before the
    clone made `provision_sandbox` skip the clone (`if not exists`) and the
    checkpoint died with "not a git directory". No `.git` => no-op."""
    ws = Path(workspace_dir)
    if not (ws / ".git").exists():
        return []
    existing = {
        d.name for d in (ws / _SKILLS_SUBDIR).iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    } if (ws / _SKILLS_SUBDIR).is_dir() else set()
    files, excluded, written = plan_materialization(
        skills, existing_skill_keys=existing, marker_entries=_materialized_marker(ws),
    )
    for rel_path, content in files:
        target = ws / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if written:
        _git_exclude(ws, excluded)
        _write_materialized_marker(ws, excluded)
    return written


# Provenance marker: distinguishes "a skill WE wrote in a previous run"
# (safe to rewrite/update) from "a skill committed in the repo" (untouchable).
_MARKER = ".claude/.dse-materialized"


def _materialized_marker(ws: Path) -> set[str]:
    p = ws / _MARKER
    if not p.is_file():
        return set()
    return {line.strip() for line in p.read_text().splitlines() if line.strip()}


def _write_materialized_marker(ws: Path, rels: list[str]) -> None:
    p = ws / _MARKER
    p.parent.mkdir(parents=True, exist_ok=True)
    merged = sorted(_materialized_marker(ws) | set(rels))
    p.write_text("\n".join(merged) + "\n")
    _git_exclude(ws, [_MARKER])


# One header for every reader of the note — the host one below, the in-Pod one
# further down. The Tester's copy in activities.py is asserted equal by test;
# a drifted header would not fail anything, it would quietly weaken the prompt.
_NOTE_HEADER = (
    "\n\n## Repository skills (MANDATORY guidance)\n"
    "Before writing code, read each SKILL.md below and follow its rules:\n"
)


def workspace_skills_note(workspace_dir: str) -> str:
    """Instruction section pointing at the skills present in the workspace (both
    the ones materialized from the registry AND the ones committed in the repo).
    Empty when there are none — the prompt gains no noise."""
    ws = Path(workspace_dir) / _SKILLS_SUBDIR
    if not ws.is_dir():
        return ""
    entries: list[str] = []
    for skill_dir in sorted(ws.iterdir()):
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            continue
        description = ""
        try:
            for line in md.read_text(encoding="utf-8").splitlines()[:8]:
                if line.startswith("description:"):
                    description = line.removeprefix("description:").strip()
                    break
        except (OSError, UnicodeDecodeError):
            continue
        entries.append(f"- .claude/skills/{skill_dir.name}/SKILL.md — {description or skill_dir.name}")
    if not entries:
        return ""
    return _NOTE_HEADER + "\n".join(entries)


# ---------------------------------------------------------------------------
# In-Pod materialization (K8s runtime).
#
# Under the K8s driver the workspace lives inside the sandbox Pod, so the host
# writer above reaches nothing: the Planner got the skills as context while the
# Coder — the one that actually writes code — worked with none of them. That
# asymmetry was documented in provision_sandbox as a known limitation and is
# what this closes.
#
# The rules are NOT reimplemented here; both writers go through
# plan_materialization for exactly that reason.
# ---------------------------------------------------------------------------

_POD_WORKSPACE = "/workspace"


def _read_pod_state(run) -> tuple[set[str], set[str]]:
    """One exec: which skill dirs already carry a SKILL.md, and what we wrote on
    a previous round. Anything unreadable degrades to "nothing known", which is
    the safe direction — an unknown dir is then treated as ours to write, and
    the repo-sovereignty check below is what protects a committed skill."""
    script = (
        f"cd {_POD_WORKSPACE} 2>/dev/null || exit 0; "
        "echo '--dirs--'; "
        f"for d in {_SKILLS_SUBDIR.as_posix()}/*/; do "
        '[ -f "$d/SKILL.md" ] && basename "$d"; done 2>/dev/null; '
        "echo '--marker--'; "
        f"cat {_MARKER} 2>/dev/null"
    )
    rc, out = run(["sh", "-c", script], None)
    if rc != 0:
        return set(), set()
    dirs: set[str] = set()
    marker: set[str] = set()
    bucket = None
    for line in (out or "").splitlines():
        line = line.strip()
        if line == "--dirs--":
            bucket = dirs
            continue
        if line == "--marker--":
            bucket = marker
            continue
        if line and bucket is not None:
            bucket.add(line)
    return dirs, marker


def materialize_skills_in_pod(skills: list[Skill], *, run) -> list[str]:
    """Write the served skills into the sandbox Pod's workspace.

    `run(argv, input_text) -> (returncode, stdout)` is injected so this is
    testable without a cluster — the same reason build_pod_manifest is a pure
    function.

    Two execs, not one per skill: 21 skills would otherwise be 21 round trips
    through the API server. The payload travels base64-encoded so arbitrary
    SKILL.md content never has to survive shell quoting.
    """
    if not skills:
        return []
    existing, marker = _read_pod_state(run)
    files, excludes, written = plan_materialization(
        skills, existing_skill_keys=existing, marker_entries=marker
    )
    if not files:
        return []

    import base64
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel_path, content in files:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=rel_path)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    payload = base64.b64encode(buf.getvalue()).decode("ascii")

    exclude_lines = "".join(f"{e}\n" for e in excludes)
    script = (
        f"set -e; cd {_POD_WORKSPACE}; "
        "base64 -d > /tmp/_skills.tgz && tar xzf /tmp/_skills.tgz -C . && rm -f /tmp/_skills.tgz; "
        # LOCAL exclude, never committed: the Coder's deterministic commit must
        # not drag guidance into the customer's PR. Appended idempotently.
        "mkdir -p .git/info; "
        f"printf '%s' {_sh_quote(exclude_lines)} | while IFS= read -r l; do "
        '[ -n "$l" ] && grep -qxF "$l" .git/info/exclude 2>/dev/null || printf \'%s\\n\' "$l" >> .git/info/exclude; '
        "done; "
        # Provenance marker: tells a later round "we wrote this" so it may be
        # refreshed, versus "the repo ships it" which is untouchable.
        f"mkdir -p $(dirname {_MARKER}); "
        f"printf '%s' {_sh_quote(exclude_lines)} >> {_MARKER}; "
        "echo OK"
    )
    rc, out = run(["sh", "-c", script], payload)
    if rc != 0 or "OK" not in (out or ""):
        return []
    return written


def workspace_skills_note_in_pod(run, *, limit: int = 32_000) -> str:
    """The same note as `workspace_skills_note`, read INSIDE the sandbox Pod.

    On the K8s runtime the host reader above sees a workspace directory that
    does not exist on the worker, so it returned "" for every production Coder
    turn — with the skills sitting materialized in the Pod the whole time. The
    listing therefore runs where the workspace is real, exactly like the
    Tester's context read (`_pod_tester_context`).

    `run(argv, input_text) -> (returncode, stdout)` is the same injected shape
    as `materialize_skills_in_pod` — testable without a cluster. Any failure
    degrades to "" (a prompt without guidance, never a failed turn); the entry
    format and header are shared with the host reader so the two runtimes can
    not drift apart.

    `limit` is a guard against pathology, NOT the Tester's 2 kB prompt budget:
    the Coder's host-path note is uncapped, and a production pod carries the
    21 registry skills plus the repo's committed ones (~9 kB of entries) — a
    2 kB cut lands mid-alphabet and silently drops exactly the late-alphabet
    skill this channel existed to deliver."""
    # LC_ALL=C: the glob's order is the locale's collation — byte order under C,
    # which is what Python's sorted() gives the host reader. The sed strips both
    # ends of the description for the same reason: the host does .strip().
    script = (
        "LC_ALL=C; export LC_ALL; "
        f"cd {_POD_WORKSPACE} 2>/dev/null || exit 0; "
        'for d in .claude/skills/*/; do [ -f "$d/SKILL.md" ] || continue; '
        'desc=$(grep -m1 "^description:" "$d/SKILL.md" | cut -d: -f2- | '
        "sed 's/^ *//; s/ *$//'); n=$(basename \"$d\"); "
        'echo "- .claude/skills/$n/SKILL.md — ${desc:-$n}"; '
        f"done 2>/dev/null | head -c {limit}"
    )
    rc, out = run(["sh", "-c", script], None)
    if rc != 0:
        return ""
    entries = (out or "").strip()
    if not entries:
        return ""
    return _NOTE_HEADER + entries


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
