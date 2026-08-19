"""How the Tester bridge on K8s reports an ending it did not measure.

Three real incidents behind this file:
  - 2026-07-29, audit_log 23474: `tester_turn_completed` with returncode=137.
    A SIGKILL from the cgroup OOM killer went back to the Coder as a failing
    assertion, and the Coder spent a paid turn fixing a test that never ran.
  - the same bridge's `subprocess.run` calls had no `TimeoutExpired` handler:
    on expiry the activity returned NOTHING, so Temporal retried the whole turn
    — cold `npm install` included — with no result anywhere saying why.
  - `TesterTurnResult.cost_usd` was the literal 0.0, so the per-work-item
    ceiling counted only the Coder.

Everything here drives the real `_tester_pod_sync` with `subprocess.run`
replaced by a fake cluster, and asserts on the RESULT and the audit row — the
two surfaces the workflow and a human actually read.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from dse_contracts import GateStatus, RunTesterTurnInput
from sandbox_runtime import activities

_SUITE_MARKERS = ("npm test", "python3 -m pytest")
_REUSE_MARKER = "--grep='^tester('"

# Kept before any monkeypatching so a test can still shell out for real.
_REAL_RUN = subprocess.run


def _done(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _fake_cluster(*, suite, reused=("tests/test_dse.py",), listing=None, seen=None):
    """Stands in for every `kubectl` the bridge shells out to. `suite` is what
    the suite run returns (a CompletedProcess) or raises (an exception);
    `listing` does the same for the `find` that proves /workspace is readable."""

    def fake_run(argv, **kwargs):
        if seen is not None:
            # The kwargs too: `timeout` is the clock that decides whether an
            # overrun is named `exec_timeout` or `suite_hung`.
            seen.append((argv, kwargs))
        joined = " ".join(argv)
        # The authoring context is READ from the Pod now — five bounded commands
        # instead of a copy of the tree.
        if "head -c" in joined:
            if "find ." in joined:
                if isinstance(listing, BaseException):
                    raise listing
                return _done(argv, 0, stdout="./src/app.spec.ts\n./tests/test_dse.py\n")
            if "cat package.json" in joined:
                return _done(argv, 0, stdout='{"name":"fixture"}')
            if "git show" in joined:
                return _done(argv, 0, stdout="diff --git a/x b/x\n")
            return _done(argv, 0, stdout="")
        if _REUSE_MARKER in joined:
            return _done(argv, 0, stdout="".join(f"{f}\n" for f in reused))
        if any(m in joined for m in _SUITE_MARKERS):
            if isinstance(suite, BaseException):
                raise suite
            return suite
        # git config / git add+commit / git rev-parse / the local `git show`
        # that feeds the authoring prompt.
        return _done(argv, 0, stdout="deadbeef\n")

    return fake_run


@pytest.fixture()
def audits(monkeypatch):
    """audit_emit talks to Postgres; capture it and read the row instead."""
    rows: list[dict] = []
    monkeypatch.setattr(
        activities, "audit_emit", lambda **kw: rows.append(kw)
    )
    return rows


def _run_bridge(monkeypatch, *, seen=None, **cluster):
    monkeypatch.setattr(subprocess, "run", _fake_cluster(seen=seen, **cluster))
    return activities._tester_pod_sync(
        RunTesterTurnInput(work_item_id="wi-137", tenant_id="tenant-t", instruction="cover it"),
        "dse-sbx-wi-137",
        None,
        "vk-fixture",
        False,
    )


def _completed_row(audits):
    return next(a for a in audits if a["action"] == "tester_turn_completed")


def _suite_call(seen):
    """The `kubectl exec` that runs the suite: (argv, kwargs)."""
    return next(call for call in seen if "npm test" in call[0][-1])


# ---------------------------------------------------------------------------
# 0.3 — rc=137 (OOM kill) is not a failing assertion
# ---------------------------------------------------------------------------


def test_oom_kill_is_not_reported_as_a_test_failure(monkeypatch, audits):
    monkeypatch.setenv("DSE_SANDBOX_MEM_LIMIT", "1536Mi")
    result = _run_bridge(monkeypatch, suite=_done([], 137, stdout="ok 1 - adds\n"))

    assert result.returncode == 137
    assert result.tests_passed is False
    # FAIL is a verdict on the code under test. This one never got that far.
    assert result.status is GateStatus.ERROR
    assert "NOT A TEST FAILURE" in result.failure_output
    assert "1536Mi" in result.failure_output, "the limit that was hit has to be in the text"
    assert _completed_row(audits)["details"]["outcome"] == "resource_kill"


def test_the_memory_limit_is_named_even_when_the_env_does_not_carry_it(monkeypatch, audits):
    """A message that says the suite went over "its limit" without naming the
    limit sends the reader to the same hunt the incident already cost."""
    monkeypatch.delenv("DSE_SANDBOX_MEM_LIMIT", raising=False)
    result = _run_bridge(monkeypatch, suite=_done([], 137))

    assert "DSE_SANDBOX_MEM_LIMIT" in result.failure_output


def test_a_kill_and_a_timeout_are_kept_apart(monkeypatch, audits):
    """124 is a clock, 137 is a memory limit; merging them would send the Coder
    to close a handle when what it has to do is use less memory."""
    result = _run_bridge(monkeypatch, suite=_done([], 137))

    assert _completed_row(audits)["details"]["suite_hung"] is False
    assert "DID NOT TERMINATE" not in result.failure_output


def test_a_failing_assertion_is_classified_as_a_plain_test_failure(monkeypatch, audits):
    """The distinction still holds — a real rc=1 must not acquire an infra
    excuse — but the VERDICT is now L1's. The Tester records what happened and
    defers; see test_a_failing_suite_no_longer_sends_the_coder_back."""
    result = _run_bridge(monkeypatch, suite=_done([], 1, stdout="AssertionError: 1 != 2\n"))

    assert _completed_row(audits)["details"]["outcome"] == "tests_failed"
    assert result.failure_output.startswith("AssertionError")
    assert result.returncode == 1, "the truth about the run is still recorded"


def test_a_failing_suite_no_longer_sends_the_coder_back(monkeypatch, audits):
    """L1's `test` gate runs the same command over the COMMITTED state minutes
    later. Two gates over one suite is not twice the safety: both spend the same
    `coder_retry_count`, so the first one firing means the second never runs.

    Measured across four work items — every one died at
    `tester_retry_cap_exhausted` with `l1_completed` = 0. The real gate never
    saw the code once."""
    result = _run_bridge(monkeypatch, suite=_done([], 1, stdout="AssertionError: 1 != 2\n"))
    assert result.suite_deferred is True
    assert result.status is GateStatus.PASS


def test_an_infrastructure_ending_is_never_deferred(monkeypatch, audits):
    """A hung or OOM-killed suite is the runtime dying, not the tests
    disagreeing with the code — and the Tester's clock is the only one that
    catches a hang before L1's much longer one."""
    for rc in (124, 137):
        result = _run_bridge(monkeypatch, suite=_done([], rc, stdout=""))
        assert result.suite_deferred is False, f"rc={rc} was deferred"
        assert result.tests_passed is False, f"rc={rc} reported as passing"


def test_the_gate_can_be_turned_back_on(monkeypatch, audits):
    """A repository that wants the Tester to gate its own suite says so."""
    monkeypatch.setenv("DSE_TESTER_SUITE_IS_A_GATE", "1")
    result = _run_bridge(monkeypatch, suite=_done([], 1, stdout="AssertionError\n"))
    assert result.suite_deferred is False
    assert result.status is GateStatus.FAIL


def test_a_passing_suite_stays_a_pass(monkeypatch, audits):
    result = _run_bridge(monkeypatch, suite=_done([], 0, stdout="1 passed\n"))

    assert result.tests_passed is True
    assert result.status is GateStatus.PASS
    assert result.failure_output == ""
    assert _completed_row(audits)["details"]["outcome"] == "passed"


def test_the_deferral_is_written_to_the_ledger_row(monkeypatch, audits):
    """`suite_deferred` is the fact that reconciles `tests_passed=True` with
    `outcome=tests_failed, returncode=1` in the SAME row. It existed in the
    in-memory result and was dropped from the audit payload — so the ledger
    read as a contradiction, 7 events out of 7 across two production runs."""
    result = _run_bridge(monkeypatch, suite=_done([], 1, stdout="AssertionError: 1 != 2\n"))

    assert result.suite_deferred is True
    assert _completed_row(audits)["details"]["suite_deferred"] is True


def test_a_plain_pass_reads_not_deferred_in_the_ledger(monkeypatch, audits):
    _run_bridge(monkeypatch, suite=_done([], 0, stdout="1 passed\n"))

    assert _completed_row(audits)["details"]["suite_deferred"] is False


# ---------------------------------------------------------------------------
# 0.3 — the timeout message may only claim what the timeout measured
# ---------------------------------------------------------------------------


def test_a_hung_suite_message_does_not_invent_the_cause(monkeypatch, audits):
    """The old text asserted an open http server or a pending timer as fact. A
    timeout cannot tell stuck from slow, and the Coder paid for turns spent
    hunting a handle that may never have existed."""
    result = _run_bridge(monkeypatch, suite=_done([], 124, stdout="ok 1 - adds\n"))

    assert _completed_row(audits)["details"]["outcome"] == "suite_hung"
    assert result.status is GateStatus.ERROR
    # The budget is configuration now, so the text has to name the one THIS run
    # was measured against — and the ledger has to carry the same number, or a
    # `suite_hung` a week old cannot be told from a ConfigMap that was too tight.
    effective = activities._tester_clocks().suite
    assert f"{effective}s" in result.failure_output
    assert _completed_row(audits)["details"]["suite_timeout_seconds"] == effective
    assert "SLOW" in result.failure_output and "STUCK" in result.failure_output
    assert "may well pass" not in result.failure_output


# ---------------------------------------------------------------------------
# 0.2 — a TimeoutExpired has to become a result, never an escaped exception
# ---------------------------------------------------------------------------


def test_the_suite_exec_timing_out_returns_a_named_result(monkeypatch, audits):
    """Uncaught, this returned NO result at all: Temporal retried the turn from
    zero and re-ran the cold npm install, over and over."""
    boom = subprocess.TimeoutExpired(cmd=["kubectl", "exec"], timeout=600, output=b"partial\n")
    result = _run_bridge(monkeypatch, suite=boom)

    assert result.returncode == activities._RC_POD_EXEC_TIMEOUT
    assert result.tests_passed is False
    assert result.status is GateStatus.ERROR
    assert _completed_row(audits)["details"]["outcome"] == "exec_timeout"
    # the command and the limit that blew, not just "it failed". The limit is
    # the exec's, which is derived from the two configured budgets — reporting
    # the suite's here would point the reader at the wrong clock.
    assert f"{activities._tester_clocks().pod_exec}s" in result.failure_output
    assert "kubectl" in result.failure_output
    assert "partial" in result.failure_output, "whatever the process printed is evidence"


def test_a_timeout_with_no_output_at_all_is_still_readable(monkeypatch, audits):
    """TimeoutExpired.stdout/stderr are None when nothing was read before the
    kill, and bytes when the pipe was binary — neither may blow up the report."""
    boom = subprocess.TimeoutExpired(cmd=["kubectl", "exec"], timeout=600)
    result = _run_bridge(monkeypatch, suite=boom)

    assert result.returncode == activities._RC_POD_EXEC_TIMEOUT
    assert f"TIMEOUT after {activities._tester_clocks().pod_exec}s" in result.failure_output


def test_an_unreadable_workspace_does_not_lose_the_turn(monkeypatch, audits):
    """The authoring context is now READ from the Pod instead of copied out of
    it. When the read that proves /workspace is reachable fails, the turn must
    end as "nothing authored" with a durable row — not blow up and be retried
    from a cold install."""
    boom = subprocess.TimeoutExpired(cmd=["kubectl", "exec"], timeout=30)
    result = _run_bridge(monkeypatch, suite=_done([], 0), reused=(), listing=boom)

    assert result.tests_ran is False, "nothing could be authored without the workspace"
    failed = next(a for a in audits if a["action"] == "tester_workspace_context_failed")
    assert "rc=" in failed["details"]["error"]


def test_the_context_read_never_leaves_the_pod_with_the_repository(monkeypatch, audits):
    """The incident of 2026-08-05: `kubectl cp` of /workspace put an Angular
    node_modules in the worker's /tmp — a 256Mi emptyDir — and kubelet evicted
    the Pod five times in five minutes. Nothing may copy the tree out again."""
    _fake_gateway(monkeypatch, _AUTHORED, 0.01)
    seen: list[tuple[list[str], dict]] = []
    _run_bridge(monkeypatch, seen=seen, suite=_done([], 0), reused=())

    assert not [a for a, _ in seen if "cp" in a[:3]], "the workspace was copied out again"
    assert not [a for a, _ in seen if "tar" in a], "the workspace was streamed out again"
    reads = [a for a, kw in seen if "exec" in a and "head -c" in " ".join(a)]
    assert reads, "the context was never read"
    for argv, kw in seen:
        if "head -c" in " ".join(argv):
            assert kw.get("timeout") == activities._CONTEXT_READ_TIMEOUT_SECONDS


def test_a_broken_npm_install_says_so_instead_of_hiding_in_dev_null(monkeypatch, audits):
    """The install ran under `|| true` with its output in /dev/null, so a
    half-installed node_modules reached the Coder as MODULE_NOT_FOUND — an
    import the runner broke, not the code. The script only ever executes in a
    Pod, so this checks the exact string that would be sent there, and parses
    it for real (`sh -n`) because it is built by concatenation and a quoting
    slip would only show up live."""
    seen: list[tuple[list[str], dict]] = []
    _run_bridge(monkeypatch, suite=_done([], 0), seen=seen)
    script = _suite_call(seen)[0][-1]

    assert "/dev/null" not in script
    assert "npm install FAILED" in script
    assert "not a test problem" in script
    # It also has a clock of its own now — without one it silently ate the
    # suite's share of the exec, and rc=124 had no meaning here.
    assert f"timeout -k 10 {activities._tester_clocks().install} npm install" in script
    parse = _REAL_RUN(["sh", "-n", "-c", script], capture_output=True, text=True)
    assert parse.returncode == 0, parse.stderr


# ---------------------------------------------------------------------------
# 0.1 — the suite's budget is configuration, and it has to REACH the Pod
# ---------------------------------------------------------------------------


def test_the_configured_budgets_are_the_ones_that_reach_the_pod(monkeypatch, audits):
    """Configuration that never reaches the shell is not configuration. This is
    the item: 180s was a module constant, the 180-600s band used to pass before
    it existed, and the exit criterion needs a real suite of 10 to 15 minutes."""
    monkeypatch.setenv("DSE_TESTER_SUITE_TIMEOUT_SECONDS", "840")
    monkeypatch.setenv("DSE_TESTER_INSTALL_TIMEOUT_SECONDS", "240")
    seen: list[tuple[list[str], dict]] = []
    _run_bridge(monkeypatch, suite=_done([], 0), seen=seen)
    argv, kwargs = _suite_call(seen)

    assert "timeout -k 10 840 npm test" in argv[-1]
    assert "timeout -k 10 240 npm install" in argv[-1]
    assert "timeout -k 10 180" not in argv[-1], "the old constant must be gone"
    # And the exec that wraps them is strictly outside both, or the overrun
    # would be reported as the worker's deadline instead of the suite's.
    assert kwargs["timeout"] > 840 + 240


def test_the_pytest_branch_gets_the_same_configured_budget(monkeypatch, audits):
    """The Python path hangs the same way and is the one a fix like this
    forgets — the acceptance repo for the SAST gate is Python."""
    monkeypatch.setenv("DSE_TESTER_SUITE_TIMEOUT_SECONDS", "840")
    seen: list[tuple[list[str], dict]] = []
    _run_bridge(monkeypatch, suite=_done([], 0), seen=seen)
    script = _suite_call(seen)[0][-1]

    assert "timeout -k 10 840 python3 -m pytest" in script


# ---------------------------------------------------------------------------
# The Tester's cost stops being the literal 0.0
# ---------------------------------------------------------------------------


class _FakeCompletion:
    def __init__(self, content: str, cost_usd: float):
        self.content = content
        self.cost_usd = cost_usd


def _fake_gateway(monkeypatch, content, cost_usd):
    from model_gateway_client import gateway_call

    monkeypatch.setattr(
        gateway_call, "chat_completion",
        lambda **kw: _FakeCompletion(content, cost_usd),
    )


_AUTHORED = json.dumps(
    {"files": [{"path": "tests/test_authored_dse.py", "content": "def test_x():\n    assert True\n"}]}
)


def test_the_turn_reports_what_the_authoring_call_cost(monkeypatch, audits):
    _fake_gateway(monkeypatch, _AUTHORED, 0.0123)
    result = _run_bridge(monkeypatch, suite=_done([], 0), reused=())

    assert result.test_files == ["tests/test_authored_dse.py"]
    assert result.cost_usd == pytest.approx(0.0123)
    assert _completed_row(audits)["details"]["cost_usd"] == pytest.approx(0.0123)


def test_an_unusable_answer_costs_exactly_as_much_as_a_usable_one(monkeypatch, audits):
    """The gateway billed the moment it answered. Reporting 0.0 because the JSON
    did not parse is how the money left both the ceiling and the reconciliation.

    Desde 2026-08-19 (wi_95a54cb4) uma resposta imprestável ganha UM retry com
    o erro na cara — então a fatura de duas respostas ruins é o custo das DUAS
    chamadas, nunca zero e nunca só a primeira."""
    _fake_gateway(monkeypatch, "sorry, I cannot do that", 0.0077)
    result = _run_bridge(monkeypatch, suite=_done([], 0), reused=())

    assert result.tests_ran is False
    assert result.cost_usd == pytest.approx(0.0154)


def test_reusing_the_previous_round_costs_nothing(monkeypatch, audits):
    """No model call, no cost — the guard against a cost that appears from
    nowhere on a retry."""
    result = _run_bridge(monkeypatch, suite=_done([], 0))

    assert result.test_files == ["tests/test_dse.py"]
    assert result.cost_usd == 0.0


def test_a_maven_repository_runs_maven_not_pytest(monkeypatch, audits):
    """A Java repo has no package.json, so the suite script fell through to
    `python3 -m pytest` — which finds no tests, exits non-zero, and reports as
    "the tests you wrote fail". Measured: two backend work items died at
    `tester_retry_cap_exhausted` having never run a single Java test, and the
    Coder was sent to fix an assertion that never existed."""
    _fake_gateway(monkeypatch, _AUTHORED, 0.01)
    seen: list[tuple[list[str], dict]] = []
    _run_bridge(monkeypatch, seen=seen, suite=_done([], 0), reused=())

    script = next(
        " ".join(a) for a, _ in seen if any("npm test" in part for part in a)
    )
    assert "pom.xml" in script, "a Maven repository is not recognised at all"
    assert "./mvnw" in script, "must prefer the repo's pinned wrapper"
    assert script.index("pom.xml") < script.index("pytest"), (
        "pytest must remain the LAST fallback, not the one a Java repo hits"
    )


def test_the_tester_writes_where_it_asked_and_never_stacks_copies():
    """O laço reautora a mesma spec. Renomear em vez de substituir empilhava uma
    segunda cópia quebrada ao lado da primeira, e na rodada seguinte uma
    terceira: medido no testbed Angular como `-dse.spec.ts`,
    `-dse-dse.spec.ts` e `-dse-dse2.spec.ts` falhando juntas, a suite piorando
    a cada rodada enquanto o item queimava o orçamento inteiro.

    O guard de renomeação saiu em 2026-08-10 e o empilhamento some pela raiz:
    o caminho pedido é o caminho escrito. Este pin é o que impede alguém de
    reintroduzir um desvio de caminho na autoria."""
    from sandbox_runtime.activities import _model_authored_test_script
    import inspect

    src = inspect.getsource(_model_authored_test_script)
    assert "_write_paths_for_authoring" not in src, (
        "voltou um desvio de caminho na autoria — é o que empilhava as cópias"
    )
    assert '{"tool": "write_file", "path": path' in src, (
        "o caminho escrito tem que ser o caminho que o Tester pediu"
    )
