"""Under the K8s driver the workspace lives inside the sandbox Pod, so the host
materializer reached nothing: the Planner read 21 skills while the Coder — the
one that writes the code — worked with none of them, and nothing recorded the
difference. These pin the in-Pod writer with a fake exec, no cluster needed."""

from __future__ import annotations

import base64
import io
import tarfile

from sandbox_runtime.skill_files import materialize_skills_in_pod, plan_materialization
from sandbox_runtime.skill_registry import Skill


def _skill(key: str, body: str = "rule: do the thing") -> Skill:
    return Skill(tenant_id="t", skill_key=key, title=f"Title {key}", body=body,
                 category="engineering", applies_to=["python"])


class FakePod:
    """Records what would have run, and answers the state probe."""

    def __init__(self, existing: str = "", marker: str = ""):
        self.existing, self.marker = existing, marker
        self.writes: list[tuple[list[str], str | None]] = []

    def run(self, argv, stdin):
        script = argv[-1]
        if "--dirs--" in script:
            return 0, f"--dirs--\n{self.existing}\n--marker--\n{self.marker}\n"
        self.writes.append((argv, stdin))
        return 0, "OK\n"

    def extracted(self) -> dict[str, str]:
        """The tar the writer streamed, unpacked — i.e. what lands in the Pod."""
        assert self.writes, "nothing was written"
        raw = base64.b64decode(self.writes[-1][1])
        out: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for m in tar.getmembers():
                out[m.name] = tar.extractfile(m).read().decode()
        return out


def test_the_coder_actually_receives_the_files():
    pod = FakePod()
    written = materialize_skills_in_pod([_skill("handling-money"), _skill("redacting-pii")], run=pod.run)
    assert sorted(written) == ["handling-money", "redacting-pii"]
    files = pod.extracted()
    assert ".claude/skills/handling-money/SKILL.md" in files
    assert "rule: do the thing" in files[".claude/skills/handling-money/SKILL.md"]
    # frontmatter is what makes the substrate load it natively
    assert files[".claude/skills/handling-money/SKILL.md"].startswith("---\nname: handling-money")


def test_a_skill_committed_in_the_target_repo_is_never_overwritten():
    """Repo beats registry. The Pod already has the dir and OUR marker does not
    claim it, so it belongs to the customer's repo."""
    pod = FakePod(existing="handling-money")
    written = materialize_skills_in_pod([_skill("handling-money"), _skill("other")], run=pod.run)
    assert written == ["other"]
    assert ".claude/skills/handling-money/SKILL.md" not in pod.extracted()


def test_a_skill_we_wrote_last_round_is_refreshed():
    """Same dir, but the marker says we put it there — so it is ours to update,
    otherwise a registry change would never reach a rebuilt sandbox."""
    pod = FakePod(existing="handling-money", marker=".claude/skills/handling-money/")
    written = materialize_skills_in_pod([_skill("handling-money", "NEW BODY")], run=pod.run)
    assert written == ["handling-money"]
    assert "NEW BODY" in pod.extracted()[".claude/skills/handling-money/SKILL.md"]


def test_guidance_is_excluded_from_git_so_it_never_reaches_the_customer_pr():
    pod = FakePod()
    materialize_skills_in_pod([_skill("k")], run=pod.run)
    script = pod.writes[-1][0][-1]
    assert ".git/info/exclude" in script
    assert ".claude/.dse-materialized" in script


def test_nothing_is_written_when_the_pod_refuses():
    class Broken(FakePod):
        def run(self, argv, stdin):
            if "--dirs--" in argv[-1]:
                return 0, "--dirs--\n--marker--\n"
            return 1, "exec failed"

    assert materialize_skills_in_pod([_skill("k")], run=Broken().run) == []


def test_an_unreadable_pod_state_does_not_crash_the_provision():
    def dead(argv, stdin):
        return 1, "pod gone"

    assert materialize_skills_in_pod([_skill("k")], run=dead) == []


def test_both_writers_share_one_rule_set():
    """The point of plan_materialization: two implementations of "repo beats
    registry" would drift, and the drift would be invisible — the Coder would
    get different conventions depending on which runtime it ran under."""
    files, excludes, keys = plan_materialization(
        [_skill("a"), _skill("b")],
        existing_skill_keys={"a"}, marker_entries=set(),
    )
    assert keys == ["b"]
    assert excludes == [".claude/skills/b/"]
    assert files[0][0] == ".claude/skills/b/SKILL.md"
