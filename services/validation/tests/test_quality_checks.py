"""WSE-E1-T1 — proves that lint/typecheck/test/build actually run (real
ruff/mypy/pytest/compileall subprocesses via LocalFakeSandbox) and that parsing is
structured (counts issues/errors, does not just look at the exit code)."""
from __future__ import annotations

from dse_contracts import GateStatus

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import build_check, lint_check, typecheck_check
from dse_validation.l1.quality_checks import test_check as run_test_check
from dse_validation.sandbox_exec import ExecResult


def test_lint_check_passes_on_clean_code(sandbox):
    cfg = L1Config.for_test_repo()
    finding = lint_check(sandbox, cfg)
    assert finding.check == "lint"
    assert finding.passed is True


def test_lint_check_fails_and_reports_issue_count(sandbox, git_repo):
    (git_repo / "bad.py").write_text("import os\nx=1\n")  # unused import + missing spaces (ruff issues)
    cfg = L1Config.for_test_repo()
    finding = lint_check(sandbox, cfg)
    assert finding.check == "lint"
    assert finding.passed is False
    assert "issue" in finding.detail


def test_typecheck_check_runs_real_mypy(sandbox):
    cfg = L1Config.for_test_repo()
    finding = typecheck_check(sandbox, cfg)
    assert finding.check == "typecheck"
    # the fixture repo has no declared type error -> must pass
    assert finding.passed is True


def test_typecheck_check_fails_on_real_type_error(sandbox, git_repo):
    (git_repo / "typed.py").write_text("def f(x: int) -> int:\n    return x + 'a'\n")
    cfg = L1Config.for_test_repo()
    finding = typecheck_check(sandbox, cfg)
    assert finding.passed is False
    assert "error" in finding.detail.lower()


def test_test_check_runs_real_pytest_and_passes(sandbox):
    cfg = L1Config.for_test_repo()
    finding = run_test_check(sandbox, cfg)
    assert finding.check == "test"
    assert finding.passed is True
    assert "1 passed" in finding.detail


def test_test_check_fails_on_real_failing_test(sandbox, git_repo):
    (git_repo / "test_broken.py").write_text("def test_broken():\n    assert 1 == 2\n")
    cfg = L1Config.for_test_repo()
    finding = run_test_check(sandbox, cfg)
    assert finding.passed is False
    assert "failed" in finding.detail


def test_build_check_runs_real_compileall(sandbox):
    cfg = L1Config.for_test_repo()
    finding = build_check(sandbox, cfg)
    assert finding.check == "build"
    assert finding.passed is True


def test_build_check_fails_on_syntax_error(sandbox, git_repo):
    (git_repo / "broken_syntax.py").write_text("def f(:\n    pass\n")
    cfg = L1Config.for_test_repo()
    finding = build_check(sandbox, cfg)
    assert finding.passed is False


def test_unknown_command_is_reported_not_silently_skipped(sandbox):
    cfg = L1Config(lint_cmd=["this-tool-does-not-exist"])
    finding = lint_check(sandbox, cfg)
    assert finding.passed is False
    assert finding.status == GateStatus.ERROR
    assert "not found" in finding.detail


def test_empty_commands_are_not_configured_never_green(sandbox):
    cfg = L1Config()
    findings = [
        lint_check(sandbox, cfg),
        typecheck_check(sandbox, cfg),
        run_test_check(sandbox, cfg),
        build_check(sandbox, cfg),
    ]
    assert all(f.passed is False for f in findings)
    assert all(f.status == GateStatus.NOT_CONFIGURED for f in findings)


class _RecordingSandbox:
    """Captures the timeout each check hands to the executor. Nothing else makes
    the number observable — and a per-stage budget that never reaches the
    executor is a manifest field that validates, escalates, and does nothing."""

    def __init__(self) -> None:
        self.timeouts: list[int] = []

    def run(self, argv, cwd=None, timeout: int = 300) -> ExecResult:
        self.timeouts.append(timeout)
        return ExecResult(argv=argv, returncode=0, stdout="", stderr="")


def test_the_manifests_per_stage_timeout_is_the_one_that_runs():
    """`timeouts` is validated against the activity's budget and can ERROR a work
    item; if the value then never reaches the executor, the guard is policing a
    number nothing uses while the stage runs on the scalar."""
    cfg = L1Config(
        lint_cmd=["ruff"], typecheck_cmd=["mypy"], test_cmd=["pytest"], build_cmd=["make"],
        timeout_seconds=300,
        timeouts={"lint": 30, "test": 700},
    )
    box = _RecordingSandbox()

    lint_check(box, cfg)
    typecheck_check(box, cfg)
    run_test_check(box, cfg)
    build_check(box, cfg)

    # declared -> declared; not declared -> the scalar, exactly as before.
    assert box.timeouts == [30, 300, 700, 300]


def test_a_timed_out_stage_names_the_budget_that_actually_ran():
    """The message is what a human reads when L1 goes ERROR. Printing the scalar
    while the stage ran on `timeouts.test` sends them to the wrong knob."""

    class _Slow(_RecordingSandbox):
        def run(self, argv, cwd=None, timeout: int = 300) -> ExecResult:
            super().run(argv, cwd=cwd, timeout=timeout)
            return ExecResult(argv=argv, returncode=-1, stdout="", stderr="", timed_out=True)

    cfg = L1Config(test_cmd=["pytest"], timeout_seconds=300, timeouts={"test": 700})
    finding = run_test_check(_Slow(), cfg)

    assert finding.status == GateStatus.ERROR
    assert "700s" in finding.detail





# ---------------------------------------------------------------------------
# A failing gate must never report the opposite of its own verdict.
#
# Both cases below are transcripts of a REAL L1 run on the Angular testbed
# (wi_pr21, 2026-08-05): lint FAILED reading "no lint issues" and typecheck
# FAILED reading "no type errors", because the parsers only knew ruff's and
# mypy's output shapes. That reason is what the ledger publishes.
# ---------------------------------------------------------------------------
class _CannedSandbox:
    """Replays one recorded ExecResult, whatever it is asked to run."""

    def __init__(self, result: ExecResult):
        self._result = result

    def run(self, argv, timeout=None):  # noqa: ARG002 - signature parity
        return self._result


def _canned(stdout: str, returncode: int) -> _CannedSandbox:
    return _CannedSandbox(
        ExecResult(argv=["x"], returncode=returncode, stdout=stdout, stderr="")
    )


_ESLINT_OUTPUT = """
> bmo-fee-estimator-fe@0.0.0 lint
> ng lint

/src/app/app.component.ts
  12:7  error  'unused' is assigned a value but never used  @typescript-eslint/no-unused-vars

1 problem (1 error, 0 warnings)
"""


def test_a_failed_lint_never_claims_there_were_no_issues():
    """A invariante é esta: saída reprovada NUNCA vira "no lint issues".

    A asserção de `exit=1` no summary saiu junto do dialeto do ESLint: ela
    pinava a ILEGIBILIDADE desta saída (o gate só sabia dizer "reprovou e não
    entendi"), e o stylish agora é lido. O veredito ficou melhor, não mais
    frouxo — FAIL nomeando arquivo e regra, que o Coder conserta, em vez de
    ERROR, que escala sem dono."""
    cfg = L1Config(lint_cmd=["npm", "run", "lint"])
    finding = lint_check(_canned(_ESLINT_OUTPUT, 1), cfg)
    assert finding.passed is False
    assert "no lint issues" not in finding.summary
    assert finding.status is GateStatus.FAIL
    assert "1 lint issue" in finding.summary
    assert "app.component.ts" in finding.detail


_TSC_OUTPUT = (
    "src/app/shared/services/pdf-collect-data.service.spec.ts(690,48): "
    "error TS2345: Argument of type '() => string' is not assignable\n"
    "src/app/shared/services/pdf-data.service.spec.ts(57,5): "
    "error TS2739: Type '{}' is missing the following properties\n"
)


def test_tsc_diagnostics_are_counted_not_just_mypy_ones():
    cfg = L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"])
    finding = typecheck_check(_canned(_TSC_OUTPUT, 2), cfg)
    assert finding.passed is False
    assert finding.summary == "2 type error(s)"


def test_a_failed_typecheck_never_claims_there_were_no_errors():
    """Unrecognised output must degrade to an honest line, not to a denial."""
    cfg = L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"])
    finding = typecheck_check(_canned("something nobody parses\n", 2), cfg)
    assert finding.passed is False
    assert "no type errors" not in finding.summary
    assert "exit=2" in finding.summary


# ---------------------------------------------------------------------------
# An infrastructure failure is not a verdict on the customer's code.
#
# The sandbox runs `ng lint` and `ng build` under a 1536Mi limit while V8 sizes
# its heap from the NODE's memory. Without this distinction the cgroup's OOM
# killer reads as "your code has lint errors": the workflow spends a paid Coder
# turn "fixing" a run that produced no finding, three times, and the work item
# ends `failed` with a reason that blames the diff. The Tester path has had the
# distinction for a while (`_tester_infra_outcome`); L1 had none.
# ---------------------------------------------------------------------------
def _killed(returncode=137, stdout="", stderr=""):
    return _CannedSandbox(
        ExecResult(argv=["x"], returncode=returncode, stdout=stdout, stderr=stderr)
    )


_HEAP_DEATH = (
    "<--- Last few GCs --->\n"
    "[851:0x6c3f000] 37968 ms: Mark-Compact 765.7 (783.1) -> 765.2 (784.8) MB\n"
    "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory\n"
)


def test_a_lint_killed_by_the_oom_killer_is_not_a_lint_error():
    cfg = L1Config(lint_cmd=["npm", "run", "lint"])
    finding = lint_check(_killed(137), cfg)
    assert finding.status is GateStatus.ERROR, "an OOM was scored as a code failure"
    assert "could not run" in finding.summary


def test_the_abort_v8_uses_for_an_exhausted_heap_is_infra():
    """V8 does not get killed when it runs out of heap — it prints FATAL ERROR
    and calls abort(), which is 134. The first version of this classifier only
    knew 137/139 and missed the one code the Angular testbed actually produced:
    `ng lint` died with exit=134 after printing nothing but `Linting "..."`, so
    the marker text went with the process and only the code was left."""
    cfg = L1Config(lint_cmd=["npm", "run", "lint"])
    finding = lint_check(_killed(134, stdout='Linting "bmo-fee-estimator-fe"...\n'), cfg)
    assert finding.status is GateStatus.ERROR
    assert "could not run" in finding.summary


def test_a_build_that_exhausted_the_heap_says_so():
    cfg = L1Config(build_cmd=["npm", "run", "build"])
    finding = build_check(_killed(1, stderr=_HEAP_DEATH), cfg)
    assert finding.status is GateStatus.ERROR
    assert "out of memory" in finding.summary


def test_a_typecheck_killed_is_not_a_type_error():
    cfg = L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"])
    finding = typecheck_check(_killed(137), cfg)
    assert finding.status is GateStatus.ERROR
    assert "type error" not in finding.summary


def test_a_suite_killed_is_not_a_failing_test():
    cfg = L1Config(test_cmd=["npm", "test"])
    finding = run_test_check(_killed(137), cfg)
    assert finding.status is GateStatus.ERROR
    assert "could not run" in finding.summary


def test_a_real_lint_error_is_still_a_code_failure():
    """The classifier must not swallow genuine findings — that would turn every
    failing gate into an infra excuse."""
    cfg = L1Config(lint_cmd=["npm", "run", "lint"])
    finding = lint_check(_canned("src/a.py:1:1: F401 unused import\n", 1), cfg)
    assert finding.status is GateStatus.FAIL
    assert finding.summary == "1 lint issue(s)"


# ---------------------------------------------------------------------------
# The gate judges THIS CHANGE, not the repository's history.
#
# Measured on the Angular testbed: `tsc --noEmit` reported 262 errors, every
# one in a `.spec.ts` the DSE never opened, against a change that added a
# single CONTRIBUTING.md. A markdown file cannot introduce TS2345 — but the
# work item failed for them, the fix loop spent paid Coder turns repairing
# someone else's specs, and no number of rounds could ever have passed.
# ---------------------------------------------------------------------------
_TSC_262 = (
    "src/app/shared/services/pdf-data.service.spec.ts(57,5): error TS2739: msg\n"
    "src/app/shared/utils/deep-compare.utils.spec.ts(488,54): error TS2339: msg\n"
    "src/app/store/features/fee-schedules/fee-schedules.effects.spec.ts(27,9): error TS2741: msg\n"
)


def test_pre_existing_type_errors_are_not_this_change_s_fault():
    cfg = L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"])
    # A source file the diagnostics do not mention: the gate RUNS (this is not
    # a documentation-only change) and then filters. Using a .md here would
    # exercise the skip instead, which is a different property with its own
    # test.
    finding = typecheck_check(_canned(_TSC_262, 2), cfg, {"src/app/fee.service.ts"})
    assert finding.passed is True, "the customer's pre-existing debt failed the work item"
    assert "3 elsewhere in the repository" in finding.summary


def test_a_type_error_the_change_DID_introduce_still_fails():
    """The scope must not become a blanket excuse."""
    cfg = L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"])
    out = _TSC_262 + "src/app/fee.service.ts(12,3): error TS2345: ours\n"
    finding = typecheck_check(_canned(out, 2), cfg, {"src/app/fee.service.ts"})
    assert finding.passed is False
    assert finding.summary == "1 type error(s) in the files this change touched"


def test_pre_existing_lint_issues_are_not_this_change_s_fault():
    cfg = L1Config(lint_cmd=["npm", "run", "lint"])
    out = "src/old/a.ts:1:1: no-unused-vars msg\nsrc/old/b.ts:2:2: eqeqeq msg\n"
    finding = lint_check(_canned(out, 1), cfg, {"src/app/fee.service.ts"})
    assert finding.passed is True
    assert "2 elsewhere in the repository" in finding.summary


def test_a_lint_issue_the_change_DID_introduce_still_fails():
    cfg = L1Config(lint_cmd=["npm", "run", "lint"])
    out = "src/old/a.ts:1:1: no-unused-vars msg\nsrc/new/c.ts:9:9: eqeqeq ours\n"
    finding = lint_check(_canned(out, 1), cfg, {"src/new/c.ts"})
    assert finding.passed is False
    assert finding.summary == "1 lint issue(s) in the files this change touched"


def test_without_a_diff_the_gate_still_judges_everything():
    """`None` means the scope is UNKNOWN, and losing a real finding is worse
    than reporting one that is not ours."""
    cfg = L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"])
    finding = typecheck_check(_canned(_TSC_262, 2), cfg, None)
    assert finding.passed is False
    assert finding.summary == "3 type error(s)"


def test_an_empty_diff_is_not_the_same_as_no_diff():
    """An empty SET would scope every gate down to nothing and pass anything.
    That is why `changed_files_or_none` returns None, never set()."""
    cfg = L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"])
    finding = typecheck_check(_canned(_TSC_262, 2), cfg, set())
    assert finding.passed is True, "an empty diff genuinely touches no file"
    assert "3 elsewhere" in finding.summary


# ---------------------------------------------------------------------------
# The gate does not run what it cannot judge.
#
# Measured on the Angular testbed: ~30 minutes of `npm ci`, `tsc`, `jest` and
# `ng build` over 1030 files, to judge a change that added one CONTRIBUTING.md.
# The findings are already scoped to the changed files, so every one of those
# findings was discarded on arrival — the work was provably wasted.
#
# The asymmetry is deliberate and tested in both directions: skipping a gate
# that could have found something is a FALSE GREEN; running one that could not
# is merely slow.
# ---------------------------------------------------------------------------
class _NeverRuns:
    def run(self, argv, timeout=None):  # noqa: ARG002
        raise AssertionError(f"the gate ran when it did not need to: {argv}")


DOC_ONLY = {"CONTRIBUTING.md", "docs/guide.rst"}


def test_a_documentation_change_does_not_run_the_type_checker():
    cfg = L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"])
    finding = typecheck_check(_NeverRuns(), cfg, DOC_ONLY)
    assert finding.passed is True
    assert "not run" in finding.summary


def test_a_documentation_change_does_not_run_the_linter():
    cfg = L1Config(lint_cmd=["npm", "run", "lint"])
    assert lint_check(_NeverRuns(), cfg, DOC_ONLY).passed is True


def test_a_documentation_change_does_not_run_the_suite_or_the_build():
    assert run_test_check(_NeverRuns(), L1Config(test_cmd=["npm", "test"]), DOC_ONLY).passed
    assert build_check(_NeverRuns(), L1Config(build_cmd=["npm", "run", "build"]), DOC_ONLY).passed


def test_a_source_change_still_runs_every_gate():
    """The skip must never become the default."""
    src = {"src/app/fee.service.ts"}
    cfg = L1Config(typecheck_cmd=["npx", "tsc"], lint_cmd=["npm", "run", "lint"],
                   test_cmd=["npm", "test"], build_cmd=["npm", "run", "build"])
    out = _canned("", 0)
    for finding in (typecheck_check(out, cfg, src), lint_check(out, cfg, src),
                    run_test_check(out, cfg, src), build_check(out, cfg, src)):
        assert "not run" not in finding.summary, finding.check


def test_a_lockfile_change_still_runs_the_suite_and_the_build():
    """A build or a suite is not scoped by file: a dependency bump breaks both
    without touching a single source file."""
    lock = {"package-lock.json"}
    out = _canned("", 0)
    assert "not run" not in run_test_check(out, L1Config(test_cmd=["npm", "test"]), lock).summary
    assert "not run" not in build_check(out, L1Config(build_cmd=["npm", "run", "build"]), lock).summary


def test_a_config_change_still_runs_the_type_checker():
    """tsconfig.json is not a .ts file, but it decides what typechecks."""
    out = _canned("", 0)
    finding = typecheck_check(out, L1Config(typecheck_cmd=["npx", "tsc"]), {"tsconfig.json"})
    assert "not run" not in finding.summary


def test_a_file_with_no_extension_runs_everything():
    """A Dockerfile, a Makefile, an entrypoint script — any of them can affect
    any gate, and none carries a suffix to reason about."""
    out = _canned("", 0)
    for check, cfg in (
        (lint_check, L1Config(lint_cmd=["x"])),
        (typecheck_check, L1Config(typecheck_cmd=["x"])),
        (run_test_check, L1Config(test_cmd=["x"])),
        (build_check, L1Config(build_cmd=["x"])),
    ):
        assert "not run" not in check(out, cfg, {"Dockerfile"}).summary


def test_an_unknown_scope_runs_everything():
    out = _canned("", 0)
    assert "not run" not in typecheck_check(out, L1Config(typecheck_cmd=["x"]), None).summary
    assert "not run" not in build_check(out, L1Config(build_cmd=["x"]), None).summary
