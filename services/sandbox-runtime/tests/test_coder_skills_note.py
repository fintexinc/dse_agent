"""H3 — the Coder's skills note under the K8s runtime.

`workspace_skills_note(workspace_dir)` reads the WORKER's filesystem, but on
the K8s runtime the workspace lives in the Pod volume
(`workspace_is_host_visible=False`): the host path does not exist, `ws.is_dir()`
is False and every production Coder turn shipped with an EMPTY note — while the
skills sat materialized in the Pod and the Tester, whose note is built by a
command run INSIDE the Pod, kept listing them. Writing a skill "for the Coder"
was writing into the void, and the symptom (the same error coming back) was
indistinguishable from "the model ignored the rule".

This pins the production shape without a cluster: skills exist ONLY in the
simulated Pod volume, and the instruction that crosses to the runner must list
every one of them.
"""
from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from dse_contracts import (
    AgentTurnResult,
    CheckpointOpResult,
    PostTurnResult,
    RunCoderTurnInput,
)

from sandbox_runtime.activities import _pod_tester_context, _run_coder_turn_impl
from sandbox_runtime.driver import StageExecutionResult
from sandbox_runtime.remote_substrate import RemoteSubstrate
from sandbox_runtime.skill_files import (
    _NOTE_HEADER,
    workspace_skills_note,
    workspace_skills_note_in_pod,
)

# The three real skills written on 2026-08-06 — the PrimeNG one was authored
# specifically for the Coder and reached nobody.
THE_SKILLS = {
    "providemockstore": "Specs that inject Store must use provideMockStore",
    "primeng-table-typing": "Type PrimeNG table templates via $implicit",
    "setinput-signals": "Set signal inputs with fixture.componentRef.setInput",
}


class SkillsAwarePodDriver:
    """K8s-shaped driver: the workspace is a tmp dir the worker never touches
    with its own filesystem — the only way to see it is `run_in_pod`, which
    really executes the script against the 'Pod volume' (path swapped in, the
    same effect kubectl exec gets by running where /workspace is real)."""

    def __init__(self, pod_workspace: str):
        self.pod_workspace = pod_workspace
        self.turn_instructions: list[str] = []


    @property
    def workspace_is_host_visible(self) -> bool:
        return False

    def sandbox_id_for(self, work_item_id: str) -> str:
        return f"pod-{work_item_id}"

    def execute_op(
        self, sandbox_id: str, op: str, payload: dict[str, Any], *, timeout_seconds: float = 180.0
    ) -> dict[str, Any]:
        if op == "checkpoint":
            return CheckpointOpResult(sha="a" * 40, phase=str(payload.get("phase", ""))).model_dump()
        if op == "post_turn":
            return PostTurnResult(sha="b" * 40, files_changed=["src/x.ts"]).model_dump()
        raise AssertionError(f"unexpected op: {op}")

    def execute_stage(self, request):
        self.turn_instructions.append(str(request.input_payload.get("instruction", "")))
        return StageExecutionResult(
            stage=request.stage,
            output_payload=AgentTurnResult(done=True).model_dump(),
            exit_code=0,
            duration_seconds=0.01,
        )

    def run_in_pod(
        self, sandbox_id: str, argv: list[str], input_text: str | None = None, *, timeout: int = 120
    ) -> tuple[int, str]:
        script = argv[-1].replace("/workspace", self.pod_workspace)
        proc = subprocess.run(
            [*argv[:-1], script], input=input_text,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout or ""


def test_coder_instruction_lists_the_skills_that_live_in_the_pod(tmp_path, work_item_id, state_dir):
    """Production shape: the skills are in the POD workspace (materialized at
    provision time / committed in the target repo) and the worker path for this
    work item does not exist. Reading the note on the worker's filesystem is
    exactly the H3 bug — `""` on every production Coder turn."""
    pod_ws = tmp_path / "pod-volume" / "workspace"
    for key, description in THE_SKILLS.items():
        d = pod_ws / ".claude" / "skills" / key
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {key}\ndescription: {description}\n---\n\nrule body\n",
            encoding="utf-8",
        )

    driver = SkillsAwarePodDriver(str(pod_ws))
    remote = RemoteSubstrate(driver=driver, substrate_name="fake")

    asyncio.run(
        _run_coder_turn_impl(
            RunCoderTurnInput(
                work_item_id=work_item_id, tenant_id="tenant-a", instruction="implement it",
            ),
            substrate=remote,
        )
    )

    assert driver.turn_instructions, "the turn never reached the runner"
    instruction = driver.turn_instructions[0]
    assert "## Repository skills (MANDATORY guidance)" in instruction, (
        "the Coder's instruction carries no skills note although the Pod "
        "workspace has three skills — the note was read on the WORKER's "
        "filesystem, where the workspace does not exist (H3)"
    )
    for key, description in THE_SKILLS.items():
        assert f".claude/skills/{key}/SKILL.md" in instruction
        assert description in instruction


def _pod_workspace_with_skills(tmp_path) -> str:
    ws = tmp_path / "workspace"
    for key, description in THE_SKILLS.items():
        d = ws / ".claude" / "skills" / key
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {key}\ndescription: {description}\n---\n\nrule body\n",
            encoding="utf-8",
        )
    return str(ws)


def _run_against(ws: str):
    """`run` in the injected shape, really executing the script — the same
    path-swap the driver stub above does."""
    def run(argv: list[str], stdin: str | None) -> tuple[int, str]:
        script = argv[-1].replace("/workspace", ws)
        proc = subprocess.run([*argv[:-1], script], input=stdin,
                              capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stdout or ""
    return run


def test_host_and_pod_readers_produce_the_same_note(tmp_path):
    """The two runtimes must hand the Coder the SAME note for the same
    workspace — a formatting drift would not fail anything, it would quietly
    hand different guidance depending on where the work item happened to run."""
    ws = _pod_workspace_with_skills(tmp_path)
    assert workspace_skills_note_in_pod(_run_against(ws)) == workspace_skills_note(ws)
    assert workspace_skills_note_in_pod(_run_against(ws)).startswith(_NOTE_HEADER)


def test_a_production_sized_pod_does_not_truncate_the_late_alphabet_skill(tmp_path):
    """The real pod carries the 21 registry skills (long, sentence-sized
    descriptions) plus the repo's committed ones — ~9 kB of entries. A note
    capped at the Tester's 2 kB prompt budget cuts mid-alphabet and silently
    drops `primeng-typing`, the very skill H3 was about delivering. The host
    reader ships everything; the pod reader must keep matching it at this
    size."""
    ws = tmp_path / "workspace"
    keys = [f"skill-{c}{i:02d}" for i, c in enumerate("abcdefghijklmnopqrst")]
    keys += ["angular-testbed", "primeng-typing"]
    for key in keys:
        d = ws / ".claude" / "skills" / key
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {key}\ndescription: {key} — " + "guidance " * 40 + "\n---\n\nbody\n",
            encoding="utf-8",
        )
    pod_note = workspace_skills_note_in_pod(_run_against(str(ws)))
    assert pod_note == workspace_skills_note(str(ws))
    assert ".claude/skills/primeng-typing/SKILL.md" in pod_note
    assert len(pod_note) > 2_000  # the size class the old cap silently cut


def test_pod_note_degrades_to_empty_never_to_a_failed_turn(tmp_path):
    # exec failure (pod gone, kubectl refused) → no note, not an exception
    assert workspace_skills_note_in_pod(lambda argv, stdin: (1, "pod gone")) == ""
    # workspace without skills → no noise in the prompt
    empty = tmp_path / "workspace"
    empty.mkdir()
    assert workspace_skills_note_in_pod(_run_against(str(empty))) == ""


def test_tester_and_coder_notes_share_the_header(tmp_path):
    """The Tester's note is built inline in activities.py with its own copy of
    the header ("Same shape as workspace_skills_note", says the comment). Pin
    that claim: the Coder reading the header from skill_files and the Tester
    drifting away from it would split the vocabulary the two prompts use for
    the same skills."""
    ws = _pod_workspace_with_skills(tmp_path)
    run = _run_against(ws)

    def pod_sh(script: str, *, timeout: int = 30, input_text: str | None = None):
        rc, out = run(["sh", "-c", script], input_text)
        return subprocess.CompletedProcess(args=["sh", "-c", script],
                                           returncode=rc, stdout=out, stderr="")

    # the git probe the required listing makes has to succeed inside "the Pod"
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    tester = _pod_tester_context(pod_sh)
    coder_note = workspace_skills_note_in_pod(run)
    assert tester.skills_note.startswith(_NOTE_HEADER)
    assert coder_note.startswith(_NOTE_HEADER)
    for key in THE_SKILLS:
        assert f".claude/skills/{key}/SKILL.md" in tester.skills_note
        assert f".claude/skills/{key}/SKILL.md" in coder_note
