"""The Tester's authoring context is READ from the sandbox, never copied out.

Real incident, 2026-08-05 on the Angular testbed: `run_tester_turn` ran
`kubectl cp <pod>:/workspace <local>` into the orchestrator's own /tmp. After
the L1 gate's `npm ci` that tree is an Angular install; /tmp is an emptyDir
with `sizeLimit: 256Mi`, so kubelet EVICTED the Pod mid-activity — five times
in five minutes, each replacement refilling the volume. Temporal reported it as
"Worker is shutting down and this activity did not complete in time", which
points nowhere near the cause.

Excluding node_modules from the copy would not have been enough: `.angular/
cache` and `dist/` are in there too, GNU tar's `--exclude=./node_modules` does
not match a nested `packages/*/node_modules`, and buffering the archive to
strip it only converts an eviction into an OOMKill.

The prompt wants about 5 kB. These tests hold the reads to that shape.
"""
from __future__ import annotations

import subprocess

from sandbox_runtime.activities import (
    _CONTEXT_READ_TIMEOUT_SECONDS,
    _EXAMPLE_TEST_CHARS,
    _PKG_JSON_CHARS,
    _TESTER_DIFF_CHARS,
    _TesterContextUnavailable,
    _pod_tester_context,
    _sh_quote,
)


def _recorder(responses: dict[str, str], *, fail: str | None = None):
    """A `_pod_sh` stand-in. Returns the first response whose key is a substring
    of the script, and records every script it was asked to run."""
    scripts: list[tuple[str, int | None]] = []

    def pod_sh(script: str, *, timeout: int | None = None, input_text=None):
        scripts.append((script, timeout))
        if fail and fail in script:
            return subprocess.CompletedProcess([script], 1, "", "boom")
        for key, out in responses.items():
            if key in script:
                return subprocess.CompletedProcess([script], 0, out, "")
        return subprocess.CompletedProcess([script], 0, "", "")

    pod_sh.scripts = scripts  # type: ignore[attr-defined]
    return pod_sh


_LISTING = "./src/app.spec.ts\n./tests/test_dse.py\n./src/util.ts\n"


def _ok_reader(**over):
    responses = {
        "cat package.json": '{"name":"app"}',
        "find .": _LISTING,
        "git show": "diff --git a/src/util.ts b/src/util.ts\n",
    }
    responses.update(over)
    return _recorder(responses)


# ---------------------------------------------------------------------------
# Nothing leaves the Pod but the bytes we asked for.
# ---------------------------------------------------------------------------
def test_every_read_is_truncated_inside_the_pod():
    """`head -c` runs in the Pod, so a gigantic tracked file costs one buffer of
    the size we asked for — not its own size. This is what makes the memory
    bound real rather than aspirational."""
    pod = _ok_reader()
    _pod_tester_context(pod)
    for script, _ in pod.scripts:
        assert "head -c" in script, f"an unbounded read: {script}"


def test_the_bounds_are_the_ones_the_prompt_actually_uses():
    pod = _ok_reader()
    _pod_tester_context(pod)
    joined = " ".join(s for s, _ in pod.scripts)
    assert f"head -c {_PKG_JSON_CHARS}" in joined
    assert f"head -c {_EXAMPLE_TEST_CHARS}" in joined
    assert f"head -c {_TESTER_DIFF_CHARS}" in joined



def test_the_reads_carry_the_short_timeout_not_the_control_default():
    pod = _ok_reader()
    _pod_tester_context(pod)
    for script, timeout in pod.scripts:
        assert timeout == _CONTEXT_READ_TIMEOUT_SECONDS, script


# ---------------------------------------------------------------------------
# What the context actually says.
# ---------------------------------------------------------------------------
def test_only_real_test_paths_become_existing_tests():
    ctx = _pod_tester_context(_ok_reader())
    assert ctx.existing_tests == {"src/app.spec.ts", "tests/test_dse.py"}
    assert "src/util.ts" not in ctx.existing_tests


def test_the_diff_is_read_in_the_pod_where_the_repository_is():
    """Running `git show` against a local copy is what broke when the copy
    stopped carrying .git: it exits 128 and the prompt silently reads
    "(diff unavailable)"."""
    ctx = _pod_tester_context(_ok_reader())
    assert ctx.diff.startswith("diff --git")
    assert ctx.workspace_dir is None, "there is no local copy on the K8s path"


def test_a_repo_with_no_tests_still_produces_a_usable_context():
    ctx = _pod_tester_context(_ok_reader(**{"find .": "\n"}))
    assert ctx.existing_tests == set()
    assert "no existing tests" in ctx.example_test


def test_a_repo_with_no_package_json_says_so():
    pod = _recorder({"find .": _LISTING})
    ctx = _pod_tester_context(pod)
    assert "no package.json" in ctx.package_json


# ---------------------------------------------------------------------------
# Failure is loud where it has to be.
# ---------------------------------------------------------------------------
def test_an_unreadable_workspace_raises_instead_of_pretending_it_is_empty():
    """Degrading quietly would send the model an empty repository and bill a
    turn for tests written against nothing."""
    pod = _recorder({}, fail="find .")
    try:
        _pod_tester_context(pod)
    except _TesterContextUnavailable as exc:
        assert "rc=1" in str(exc)
    else:
        raise AssertionError("an unreadable workspace was treated as an empty one")



# ---------------------------------------------------------------------------
# The example test's path comes out of the customer's repository.
# ---------------------------------------------------------------------------

def test_sh_quote_survives_a_single_quote():
    assert _sh_quote("a'b") == "'a'\\''b'"


# Os testes da nota de skills saíram com o serving (2026-08-31): a nota não
# existe mais — convenção de repo chega pelo .claude/ do próprio repositório,
# nativo no substrato agêntico.


# ---------------------------------------------------------------------------
# Behaviour against a REAL shell and a REAL tree.
#
# The first version of these asserted on the text of the command instead of on
# what it does. Mutation proved it: corrupting `find` into a syntax error, and
# replacing `_sh_quote` with the identity, both left them green. Substring
# checks on a command string pin the string, not the behaviour.
# ---------------------------------------------------------------------------
def _real_pod(root):
    """A `_pod_sh` that actually runs the script, against `root` as /workspace."""
    def pod_sh(script, *, timeout=None, input_text=None):
        return subprocess.run(
            ["sh", "-c", script.replace("cd /workspace &&", f"cd {root} &&", 1)],
            capture_output=True, text=True, errors="replace",
        )
    return pod_sh


def _repo(tmp_path, files: dict[str, str]):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    for rel, body in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return tmp_path


def test_a_project_local_venv_does_not_bury_the_repository_own_tests(tmp_path):
    """Measured on this very repo before the prune list was completed: 73% of
    the matches came from `.venv`, and under the listing's bound only 246 of
    2501 real test paths survived. The prompt then names
    `site-packages/aiohttp/test_utils.py` as forbidden while the repo's own
    tests vanish — so the Tester authors a duplicate, which is the exact bug
    `existing_tests` exists to prevent."""
    root = _repo(tmp_path, {
        "tests/test_real.py": "def test_x(): pass\n",
        ".venv/lib/python3.12/site-packages/aiohttp/test_utils.py": "vendored\n",
        "node_modules/jest/spec.js": "vendored\n",
        "__pycache__/test_cached.py": "cached\n",
        ".tox/py312/lib/test_env.py": "vendored\n",
        "coverage/lcov-report/spec.js": "generated\n",
    })
    ctx = _pod_tester_context(_real_pod(root))
    assert ctx.existing_tests == {"tests/test_real.py"}, ctx.existing_tests


def test_a_hostile_filename_is_read_as_a_name_not_run_as_a_command(tmp_path):
    """The example test's path comes out of the customer's repository."""
    evil = "q'; touch PWNED; echo '.test.js"
    root = _repo(tmp_path, {evil: "the file's real content\n"})
    ctx = _pod_tester_context(_real_pod(root))
    assert "the file's real content" in ctx.example_test
    assert not (tmp_path / "PWNED").exists(), "the filename was executed"


def test_a_filename_that_looks_like_an_option_is_still_read(tmp_path):
    """`-` sorts before every other path character, so `-e.test.js` reaches
    `cat` first — and without `--` it is consumed as a FLAG, returning rc 0 and
    empty output. The prompt then claims the repo has no tests while listing
    those same tests as forbidden."""
    root = _repo(tmp_path, {"-e.test.js": "content of the option-shaped file\n"})
    ctx = _pod_tester_context(_real_pod(root))
    assert "content of the option-shaped file" in ctx.example_test


def test_an_unreadable_candidate_falls_through_to_the_next(tmp_path):
    root = _repo(tmp_path, {"a.test.js": "", "b.test.js": "real body\n"})
    ctx = _pod_tester_context(_real_pod(root))
    assert "real body" in ctx.example_test


def test_a_byte_cut_mid_character_does_not_raise_out_of_the_pod_command(tmp_path):
    """Drives the REAL `_run_pod_command`, not a test double, because the
    failure happens inside `subprocess.run` before any of our code sees it:
    `head -c` cuts BYTES and `text=True` decodes strictly, so an em dash
    straddling the bound raised UnicodeDecodeError past every handler. The turn
    was then retried from a cold `npm ci` with nothing durable saying why."""
    from sandbox_runtime.activities import _run_pod_command

    f = tmp_path / "f"
    f.write_text("a" * 9 + "—tail", encoding="utf-8")
    proc = _run_pod_command(
        ["sh", "-c", f"cat {f} | head -c 10"], timeout=30
    )
    assert proc.returncode == 0
    assert proc.stdout.startswith("aaa"), proc.stdout


def test_a_multibyte_character_on_the_byte_bound_does_not_kill_the_activity(tmp_path):
    """`head -c` counts BYTES and `text=True` decodes strictly, so a cut inside
    a multibyte character raised UnicodeDecodeError out of the activity: the
    turn was retried from a cold `npm ci` with nothing durable saying why."""
    from sandbox_runtime.activities import _PKG_JSON_CHARS

    root = _repo(tmp_path, {
        "package.json": "a" * (_PKG_JSON_CHARS - 1) + "—tail",
        "tests/test_x.py": "def test_x(): pass\n",
    })
    ctx = _pod_tester_context(_real_pod(root))
    assert ctx.package_json.startswith("aaa")
    assert "�" not in ctx.package_json[-1:], "the byte cut leaked into the prompt"


def test_an_empty_workspace_is_refused_instead_of_read_as_an_empty_repo(tmp_path):
    """A rebuilt sandbox coming up empty is a state this system has shipped.
    Reading it as "a repository with no tests" bills a whole authoring turn for
    tests written against nothing — and tells a Node repo it is Python. The
    pipeline's own returncode is `head`'s, which is 0 even when everything
    before it failed, so the check has to be a POSITIVE assertion."""
    (tmp_path / "empty").mkdir()
    try:
        _pod_tester_context(_real_pod(tmp_path / "empty"))
    except _TesterContextUnavailable:
        pass
    else:
        raise AssertionError("an empty workspace was accepted as a repository")


def test_the_diff_keeps_its_end_where_the_hunks_are(tmp_path):
    """The Docker path keeps `stdout[-8000:]`. Keeping the HEAD instead would
    give the model the --stat summary and cut the actual changes."""
    from sandbox_runtime.activities import _TESTER_DIFF_CHARS

    # The diff has to OVERFLOW the bound, or head and tail are the same string
    # and the test pins nothing — which is exactly what the first version did.
    body = "".join(f"const line{i} = {i};\n" for i in range(_TESTER_DIFF_CHARS // 10))
    root = _repo(tmp_path, {
        "src/a.ts": body + "const THE_LAST_LINE = 1;\n",
        "tests/test_x.py": "def test_x(): pass\n",
    })
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "c1"], check=True, capture_output=True,
    )
    ctx = _pod_tester_context(_real_pod(root))
    assert len(ctx.diff) > _TESTER_DIFF_CHARS // 2, "the diff did not overflow the bound"
    assert "THE_LAST_LINE" in ctx.diff, "the END of the diff was cut — the hunks are there"
    # The discriminator is what is ABSENT: `line0` sits at the very start of
    # the diff, so it survives only if the head was kept.
    assert "const line0 = 0;" not in ctx.diff, "this kept the HEAD, not the hunks"
