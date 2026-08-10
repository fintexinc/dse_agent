"""The automated fix loop must tell the Coder what went wrong.

Real incident: a Slack task ("download a csv with the historic") failed four
times and hit the retry cap. The instruction sent to the Coder was byte-for-byte
identical on all four attempts — same md5, same 687 characters — because the
tester/L1 loop rebuilt it from the original request and appended nothing. The
last attempt changed no files at all: there was nothing new to act on.

The human-review loop had always appended its objections. These tests pin the
other loop to the same contract.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dse_contracts import GateStatus
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow as WF


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides the conftest fixture of the same name.

    Everything here is pure string formatting over values already in hand, so it
    needs no database — and skipping it without one would hide exactly the kind
    of regression these tests exist to catch."""
    return None


class _Finding(SimpleNamespace):
    pass


def _tester(returncode=1, failure_output=""):
    return SimpleNamespace(returncode=returncode, failure_output=failure_output)


def test_the_suite_output_reaches_the_coder():
    ctx = WF._tester_failure_context(
        _tester(returncode=1, failure_output="AssertionError: expected 3 columns, got 2")
    )
    joined = "\n".join(ctx)
    assert "expected 3 columns, got 2" in joined, "the Coder never learns what failed"
    assert "1" in joined  # the exit code travels too


def test_the_coder_is_told_not_to_delete_the_tests():
    """The cheapest way to make a suite pass is to remove it. Say so.

    Evoluiu em 2026-08-10: a frase era ABSOLUTA ("do not weaken or delete the
    tests") e passou a proibir o que a política concede — atualizar spec de
    CLIENTE, cuja supervisão migrou para o diff da PR. O que este pin defende
    continua valendo e ficou MAIS explícito: apagar cobertura nunca é conserto.
    A permissão é para ATUALIZAR uma asserção obsoleta, nunca para deletar."""
    joined = "\n".join(WF._tester_failure_context(_tester(failure_output="boom"))).lower()
    assert "never delete, skip or empty a test" in joined
    assert "deleting coverage is not fixing" in joined


def test_missing_output_is_reported_as_missing_not_as_silence():
    """An older worker returns no output. 'The suite printed nothing' and 'we
    failed to capture it' call for different fixes, so they must read
    differently."""
    joined = "\n".join(WF._tester_failure_context(_tester(returncode=7, failure_output="")))
    assert "not captured" in joined
    assert "7" in joined


def test_a_run_the_runtime_killed_is_not_announced_as_a_failing_suite():
    """Real incident (audit_log 23474, 2026-07-29): rc=137, the cgroup OOM
    killer, went to the Coder as a failing assertion. The runtime now names that
    ending and says it is not a test failure — and this context is prepended to
    that very text, so it must not open by contradicting it."""
    killed = _tester(returncode=137, failure_output="THIS IS NOT A TEST FAILURE: ...")
    killed.status = GateStatus.ERROR
    joined = "\n".join(WF._tester_failure_context(killed))
    assert "FAILING" not in joined
    assert "did NOT produce a verdict" in joined
    assert "THIS IS NOT A TEST FAILURE" in joined, "the runtime's explanation still travels"
    assert "137" in joined


def test_a_real_failing_assertion_is_still_announced_as_one():
    """The guard above must not blunt the ordinary case."""
    failed = _tester(returncode=1, failure_output="AssertionError: 3 != 2")
    failed.status = GateStatus.FAIL
    joined = "\n".join(WF._tester_failure_context(failed))
    assert "is FAILING" in joined
    assert "never delete, skip or empty a test" in joined.lower()


def test_the_message_and_the_escalation_agree_on_what_ended_the_run():
    """The two must never disagree about the same result.

    `_tester_infra_outcome` keys on the RETURNCODE first precisely because the
    incident came from a worker that reported rc=137 with no ERROR status, and
    the runtime that sets ERROR ships in this same change — so every rolling
    deploy reproduces that shape. Whenever the workflow treats a result as an
    infra ending, THIS text (the one the Coder actually reads) has to say the
    same thing.
    """
    for status in (None, GateStatus.FAIL, GateStatus.NOT_CONFIGURED):
        for rc in (137, 124, -1001):
            result = _tester(returncode=rc, failure_output="ok so far\n...")
            if status is not None:
                result.status = status
            assert WF._tester_infra_outcome(result) is not None, "premise of the test"
            joined = "\n".join(WF._tester_failure_context(result))
            assert "FAILING" not in joined, f"rc={rc} status={status} read as an assertion"
            assert "did NOT produce a verdict" in joined
            assert str(rc) in joined


def test_a_killed_run_that_captured_nothing_is_not_sent_hunting_for_a_mismatch():
    """A hang reported by an older worker arrives with an EMPTY failure_output.
    "look for the mismatch" is an instruction for a failing assertion, and there
    was none — the process was killed."""
    hung = _tester(returncode=124, failure_output="")
    joined = "\n".join(WF._tester_failure_context(hung))
    assert "look for the mismatch" not in joined
    assert "did NOT produce a verdict" in joined
    assert "no output" in joined.lower()
    assert "124" in joined


def test_only_the_failing_l1_checks_are_reported():
    """`l1_completed` records every check that RAN. A real run had seven checks
    passing and only `sast` erroring, and the record made it look like all eight
    had failed — so the Coder was told to fix everything and fixed nothing."""
    ctx = WF._l1_failure_context([
        _Finding(check="sast", passed=False, status=None, detail="semgrep exited 2"),
    ])
    joined = "\n".join(ctx)
    assert "sast" in joined and "semgrep exited 2" in joined
    for clean in ("lint", "typecheck", "build", "secret_scan"):
        assert clean not in joined, f"{clean} passed and must not be reported as a failure"


def test_a_finding_without_detail_still_names_the_check():
    joined = "\n".join(WF._l1_failure_context([
        _Finding(check="build", passed=False, status=None, detail=""),
    ]))
    assert "build" in joined and "no detail reported" in joined


def test_nothing_failing_produces_no_context():
    assert WF._l1_failure_context([]) == []


def test_the_instruction_actually_grows_when_a_retry_is_pending():
    """The regression itself: same input, and the instruction must now differ
    between the first attempt and the retry."""
    wf = WF.__new__(WF)
    base = SimpleNamespace(
        task_content="create a feature that allow me to download a csv",
        acceptance_criteria="repo=andre2654/fintex-wallet",
        clarification_notes=[],
        l2_objections=[],
        fix_context=[],
    )
    wf._input = base
    first = wf._agent_instruction(include_objections=True)

    base.fix_context = WF._tester_failure_context(
        _tester(returncode=1, failure_output="AssertionError: expected 3 columns, got 2")
    )
    retry = wf._agent_instruction(include_objections=True)

    assert retry != first, "the retry repeats the original instruction verbatim"
    assert first in retry, "the original task must survive alongside the failure"
    assert "expected 3 columns, got 2" in retry
