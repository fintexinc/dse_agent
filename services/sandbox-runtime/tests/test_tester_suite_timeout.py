"""A suite whose tests pass but whose process never exits used to be
indistinguishable from a suite that was still running: the only thing that ever
ended it was the 600s activity timeout, which Temporal reports as
TimeoutExpired with no output. The Tester could not see its own defect, so each
retry authored ANOTHER non-terminating file — six of them, one every ten
minutes, observed live on 2026-07-29.

The bound that fixed it was a module constant, 180s, chosen against a 1,4s
suite in a 12-file repo. That constant then became a defect of its own: the
band from 180s to 600s used to pass (the suite ran bare under the exec) and
stopped passing, and the exit criterion — a real suite of 10 to 15 minutes on
1 vCPU — became impossible by construction.

So these pin three things: the suite is bounded inside the Pod, a
non-terminating suite is reported as such, and the bound comes from
CONFIGURATION with the cascade of clocks kept in order.

And a fourth, learned by making the bound configurable: the cascade does not end
at this service. L1 runs the same suite again, in the same Pod, under its own
clock — so the budget here is only half of a pair, and the half that must be the
tighter one."""

from __future__ import annotations

import asyncio
import dataclasses
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sandbox_runtime import activities
from sandbox_runtime.activity_heartbeat import run_sync_with_heartbeat


@pytest.fixture(autouse=True)
def _no_configured_clocks(monkeypatch):
    """Every test states the configuration it is testing. Leaking one env var
    into the next test is how a default gets asserted by accident."""
    monkeypatch.delenv(activities._SUITE_TIMEOUT_ENV_VAR, raising=False)
    monkeypatch.delenv(activities._INSTALL_TIMEOUT_ENV_VAR, raising=False)


def _in_activity(monkeypatch, *, start_to_close=3600.0, schedule_to_close=7200.0):
    """Stand in for a real Activity context, which is the only place the outer
    clock is knowable. `activity.info()` is a frozen dataclass built by the
    worker; the two fields read here are all that matters."""
    monkeypatch.setattr(activities.activity, "in_activity", lambda: True)
    monkeypatch.setattr(
        activities.activity,
        "info",
        lambda: SimpleNamespace(
            start_to_close_timeout=(
                None if start_to_close is None else timedelta(seconds=start_to_close)
            ),
            schedule_to_close_timeout=(
                None if schedule_to_close is None else timedelta(seconds=schedule_to_close)
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 0.1 — the bound is configuration, not a constant
# ---------------------------------------------------------------------------


def test_the_suite_budget_comes_from_the_environment():
    """The whole point of the item: an operator with a 12-minute suite can run
    it without a code change and a redeploy."""
    assert not hasattr(activities, "_SUITE_TIMEOUT_SECONDS"), (
        "the module constant is what made a real suite impossible — it must not "
        "come back as the source of truth"
    )
    assert activities._SUITE_TIMEOUT_ENV_VAR == "DSE_TESTER_SUITE_TIMEOUT_SECONDS"


def test_a_configured_suite_budget_is_the_one_that_runs(monkeypatch):
    monkeypatch.setenv(activities._SUITE_TIMEOUT_ENV_VAR, "780")
    monkeypatch.setenv(activities._INSTALL_TIMEOUT_ENV_VAR, "300")

    clocks = activities._tester_clocks()

    assert (clocks.install, clocks.suite) == (300, 780)
    assert clocks.clamped_from is None


def test_the_default_allows_the_suite_the_exit_criterion_asks_for():
    """"A real suite of 10 to 15 minutes on 1 vCPU" is the acceptance bar. A
    default at or below 900s would fail the run it exists to allow — which is
    also why the fix for the inversion below was L1 coming UP to this number and
    not this one going down to L1's."""
    assert activities._tester_clocks().suite > 15 * 60


def test_the_default_never_outruns_the_l1_clock_on_the_same_suite():
    """The suite is measured TWICE, by two services, and only the tighter of the
    two clocks is ever observed.

    After this turn, L1 runs the repo's `test` command in the same Pod and the
    same /workspace, over the node_modules this turn installed. Every suite in
    the gap between a looser budget here and L1's PASSES the Tester and then
    dies in L1 — as a `test` finding with status ERROR, which is worth up to
    three paid Coder turns before the item fails, every one of them looking for
    an assertion that never failed. That is the diagnosis rc.12 removed from
    this service; a wider budget here recreates it one service to the right.

    Reads L1's LIVE config instead of a number mirrored into this file: a copied
    constant is exactly what let the two drift apart, and this way the assertion
    fails whichever side moves — this default going up, or L1's coming down.

    Against the budget L1 gives a repo that declares no per-stage `timeouts`,
    because that is the only one this side can know. A repository that declares
    `timeouts.test` below this can still invert the pair for itself, and nothing
    here can see that; the fix for it is the manifest becoming the single source
    of BOTH clocks."""
    l1_config = pytest.importorskip("dse_validation.config")

    l1_test_budget = l1_config.L1Config().timeout_for("test")

    assert activities._DEFAULT_SUITE_TIMEOUT_SECONDS <= l1_test_budget, (
        f"the Tester would let a suite run {activities._DEFAULT_SUITE_TIMEOUT_SECONDS}s "
        f"that L1 kills at {l1_test_budget}s (platform ceiling "
        f"{l1_config.stage_timeout_ceiling_seconds()}s per stage): a suite in that gap "
        "passes here and comes back from L1 as a `test` ERROR worth three paid Coder turns"
    )


def test_a_nonsense_value_falls_back_to_the_default_instead_of_failing_the_turn(monkeypatch):
    """A typo in a ConfigMap must not cost a paid turn: the turn is already
    running and the model has already been billed by the time this is read."""
    for junk in ("", "   ", "abc", "0", "-30", "12.5"):
        monkeypatch.setenv(activities._SUITE_TIMEOUT_ENV_VAR, junk)
        assert activities._tester_clocks().suite == activities._DEFAULT_SUITE_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# 0.1 — the cascade: inner strictly inside outer, at every level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("install", "suite"),
    [(60, 60), (300, 780), (600, 1200), (5, 5), (600, 3000)],
)
def test_the_suite_is_always_strictly_inside_the_exec_that_wraps_it(monkeypatch, install, suite):
    """If the exec fires first, the ending is named `exec_timeout` (infra) when
    the truth is `suite_hung` (the suite) — and since rc.12 those escalate down
    different paths, so the order of the clocks decides who gets handed the
    problem."""
    monkeypatch.setenv(activities._INSTALL_TIMEOUT_ENV_VAR, str(install))
    monkeypatch.setenv(activities._SUITE_TIMEOUT_ENV_VAR, str(suite))
    _in_activity(monkeypatch)

    clocks = activities._tester_clocks()

    assert clocks.suite < clocks.pod_exec
    assert clocks.install < clocks.pod_exec
    # Both can run back to back inside one exec — they do, in that order.
    assert clocks.install + clocks.suite < clocks.pod_exec


def test_the_exec_is_strictly_inside_what_the_activity_has_left(monkeypatch):
    """The exec is not the outermost clock: the activity also pays for the
    authoring call, the workspace copy and the git commands. An activity the
    server kills returns NO result, so the whole turn is retried from zero."""
    monkeypatch.setenv(activities._INSTALL_TIMEOUT_ENV_VAR, "600")
    monkeypatch.setenv(activities._SUITE_TIMEOUT_ENV_VAR, "1200")
    _in_activity(monkeypatch, start_to_close=3600.0)

    clocks = activities._tester_clocks()

    assert clocks.pod_exec + activities._TESTER_NON_SUITE_BUDGET_SECONDS < 3600


def test_the_short_pod_commands_do_not_hold_the_suites_room():
    """`git config` and `git rev-parse` inherited a 600s default. None of them
    is a suite, and every second they are allowed is counted against the same
    activity clock the suite has to fit inside — which is why the reserve below
    exists and why it has to stay small."""
    import inspect

    body = inspect.getsource(activities._tester_pod_sync)
    assert "timeout: int = _POD_CONTROL_TIMEOUT_SECONDS" in body, (
        "the default budget of a pod command must be the SHORT one; only the "
        "suite passes its own"
    )
    assert "timeout=600" not in body
    assert (
        activities._TESTER_NON_SUITE_BUDGET_SECONDS
        < activities._DEFAULT_INSTALL_TIMEOUT_SECONDS + activities._DEFAULT_SUITE_TIMEOUT_SECONDS
    ), "the reserve for the plumbing must not outweigh the work being measured"


def test_a_budget_that_would_break_the_cascade_is_clamped_with_a_warning(monkeypatch, caplog):
    """`start_to_close` lives in the WORKFLOW's input, so a legal suite budget
    stops being legal the day someone lowers it — and nothing on this side would
    notice. Clamped rather than refused: refusing produces no verdict at all,
    which is the exact failure this path exists to end."""
    monkeypatch.setenv(activities._INSTALL_TIMEOUT_ENV_VAR, "600")
    monkeypatch.setenv(activities._SUITE_TIMEOUT_ENV_VAR, "3000")
    _in_activity(monkeypatch, start_to_close=1800.0)

    with caplog.at_level("ERROR"):
        clocks = activities._tester_clocks()

    assert clocks.clamped_from == (600, 3000), "the number that was ASKED for is kept"
    assert clocks.suite < 3000
    assert clocks.pod_exec + activities._TESTER_NON_SUITE_BUDGET_SECONDS < 1800
    # The ratio the operator chose between preparing and measuring survives.
    assert clocks.suite > clocks.install


def test_the_clamp_never_goes_below_a_usable_floor(monkeypatch):
    """Under a minute the number is not a budget. An absurd outer clock is a
    misconfiguration to shout about, not a reason to run `timeout -k 10 1`."""
    monkeypatch.setenv(activities._INSTALL_TIMEOUT_ENV_VAR, "600")
    monkeypatch.setenv(activities._SUITE_TIMEOUT_ENV_VAR, "1200")
    _in_activity(monkeypatch, start_to_close=60.0)

    clocks = activities._tester_clocks()

    assert clocks.suite >= activities._MIN_SUITE_TIMEOUT_SECONDS
    assert clocks.install >= activities._MIN_INSTALL_TIMEOUT_SECONDS
    assert clocks.clamped_from == (600, 1200)


def test_outside_an_activity_the_configured_numbers_are_used_as_written(monkeypatch):
    """The Docker/dev path and the tests have no server clock to fit inside.
    Inventing one would silently shrink a budget nobody could see."""
    monkeypatch.setenv(activities._SUITE_TIMEOUT_ENV_VAR, "3000")
    monkeypatch.setattr(activities.activity, "in_activity", lambda: False)

    assert activities._tester_clocks().suite == 3000


def test_schedule_to_close_is_only_the_fallback(monkeypatch):
    """`start_to_close` is the per-attempt ceiling and therefore the right one;
    `schedule_to_close` bounds the whole chain of attempts and would license an
    exec that outlives the attempt running it."""
    monkeypatch.setenv(activities._INSTALL_TIMEOUT_ENV_VAR, "600")
    monkeypatch.setenv(activities._SUITE_TIMEOUT_ENV_VAR, "3000")
    _in_activity(monkeypatch, start_to_close=1800.0, schedule_to_close=7200.0)
    clamped_by_start = activities._tester_clocks()

    _in_activity(monkeypatch, start_to_close=None, schedule_to_close=7200.0)
    assert clamped_by_start.suite < activities._tester_clocks().suite


def _clocks_through_a_real_activity(start_to_close: float) -> activities._TesterClocks:
    """The clocks as computed where they are actually computed: inside a real
    Activity context, from the worker THREAD that `run_sync_with_heartbeat`
    offloads the bridge to.

    That hop is the part worth proving. `activity.info()` lives in a ContextVar,
    and if it did not survive `asyncio.to_thread` the outer clock would read as
    unknown in production and the fit against `start_to_close` would silently
    never happen — while every fake-based test above kept passing."""
    from temporalio.testing import ActivityEnvironment

    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        start_to_close_timeout=timedelta(seconds=start_to_close),
        schedule_to_close_timeout=timedelta(seconds=7200),
        heartbeat_timeout=timedelta(seconds=600),
    )

    async def _turn(_):
        return await run_sync_with_heartbeat(
            lambda _c: activities._tester_clocks(),
            None, stage="tester", work_item_id="wi-clocks", operation="probe",
        )

    return asyncio.run(env.run(_turn, None))


def test_the_cascade_holds_through_a_real_activity_and_its_worker_thread(monkeypatch):
    """The deployed numbers, end to end: suite inside exec, exec plus the
    turn's other work inside `start_to_close`, and `start_to_close` inside
    `schedule_to_close`."""
    monkeypatch.setenv(activities._INSTALL_TIMEOUT_ENV_VAR, "600")
    monkeypatch.setenv(activities._SUITE_TIMEOUT_ENV_VAR, "1200")

    clocks = _clocks_through_a_real_activity(3600.0)

    assert clocks.clamped_from is None, "the deployed defaults must fit as configured"
    assert clocks.suite < clocks.pod_exec < 3600 < 7200
    assert clocks.pod_exec + activities._TESTER_NON_SUITE_BUDGET_SECONDS < 3600


def test_a_lowered_activity_clock_is_noticed_from_inside_the_worker_thread(monkeypatch):
    """`start_to_close` is the WORKFLOW's input, not this side's. If the context
    were lost across the thread hop, a budget that no longer fits would go
    unclamped and the activity would die mid-suite with no result at all."""
    monkeypatch.setenv(activities._INSTALL_TIMEOUT_ENV_VAR, "600")
    monkeypatch.setenv(activities._SUITE_TIMEOUT_ENV_VAR, "1200")

    clocks = _clocks_through_a_real_activity(1800.0)

    assert clocks.clamped_from == (600, 1200)
    assert clocks.pod_exec + activities._TESTER_NON_SUITE_BUDGET_SECONDS < 1800


# ---------------------------------------------------------------------------
# The runner bound itself
# ---------------------------------------------------------------------------


def test_every_runner_is_wrapped_not_just_the_one_the_repo_declares():
    """A Python repo hangs the same way a Node one does. O comando é uma string
    de shell montada inline contra um Pod vivo, então não há costura para
    chamar — ler a fonte é a checagem honesta.

    O que se pina mudou de forma na rc.106 e não de conteúdo: o comando da
    suíte agora vem do manifesto (`commands.test_subset` escopado, ou
    `commands.test` inteiro), e o que a plataforma ainda garante é que TUDO
    roda sob o orçamento da suíte — o caminho declarado e cada degrau da escada
    que sobrou como fallback."""
    import inspect

    body = inspect.getsource(activities._tester_pod_sync)
    assert re.search(r"timeout -k \d+ \{clocks\.suite\} \{suite_cmd\}", body), \
        "o comando declarado pelo repo tem de rodar sob o orçamento da suíte"
    assert re.search(r"timeout -k \d+ \{clocks\.suite\} ", body), \
        "a escada de fallback também"
    assert "npm test --silent -- --coverage=false" in body, (
        "o degrau npm da escada escopa e desliga cobertura enquanto existir: "
        "o jest daquele repo declara thresholds globais, então QUALQUER "
        "subconjunto reprova com todo teste passando"
    )
    assert re.search(r"timeout -k \d+ \{clocks\.suite\} python3 -m pytest", body), \
        "o degrau do pytest também pendura igual"


def test_the_install_has_a_budget_of_its_own():
    """Compartilhava o exec com a suíte, então o que não gastasse pertencia a
    ela e vice-versa — e um run que morria instalando parecia exatamente um run
    que morria testando. Saiu de dentro do exec da suíte na rc.106 e virou
    prefixo próprio, memoizado; o orçamento separado continua."""
    import inspect

    body = inspect.getsource(activities._pod_install_prefix)
    assert re.search(r"timeout -k 10 \{clocks\.install\} \{_shlex\.join\(declared\)\}", body), \
        "o install declarado pelo repo roda sob o orçamento do install"
    assert re.search(r"timeout -k 10 \{clocks\.install\} npm install", body), \
        "e o fallback npm também, enquanto existir"


def test_the_install_budget_covers_a_cold_install_on_the_acceptance_repo():
    """The exit criterion is a real suite on a BIG repository, and an install
    killed at its own clock does not lose the turn — it produces a WRONG one.
    The suite still runs, resolves against a half-written node_modules, fails
    with MODULE_NOT_FOUND, and `tests_failed` says that about the code.

    The reference band is 60-90 s on the laptop and this pod measures 6,7x
    slower, so the band's own slow end is 603 s: a 600 s budget sits under it."""
    assert activities._DEFAULT_INSTALL_TIMEOUT_SECONDS > 90 * 6.7

    # And it is free: nothing in the cascade moves to pay for it.
    clocks = activities._tester_clocks()
    assert clocks.clamped_from is None
    assert clocks.install + clocks.suite < clocks.pod_exec
    assert clocks.pod_exec + activities._TESTER_NON_SUITE_BUDGET_SECONDS < 3600


def test_the_authoring_prompt_demands_a_process_that_exits():
    """The durable half: the runner bound stops the bleeding, but the tests
    themselves have to close what they open."""
    p = activities._TEST_AUTHOR_PROMPT
    assert "MUST EXIT" in p
    assert "after(" in p
    assert "server.close" in p


def test_a_hung_suite_is_named_as_such_not_reported_as_a_failed_assertion():
    """rc=124 is `timeout`'s verdict. The Coder cannot fix an assertion that
    never ran, so the outcome has to say the process did not exit — and stay
    distinct from rc=137, which is a kill from outside (see
    test_tester_pod_outcomes.py for the end-to-end reporting)."""
    assert activities._suite_outcome(tests_ran=True, returncode=124) == "suite_hung"
    assert activities._suite_outcome(tests_ran=True, returncode=1) == "tests_failed"
    note = activities._infra_outcome_note("suite_hung", 124, suite_timeout_seconds=780)
    assert "DID NOT TERMINATE" in note


def test_the_hung_note_names_the_budget_that_actually_ran():
    """The note is what the Coder reads. A number the run did not use is the
    same lie the old fixed diagnosis was, in a different font."""
    note = activities._infra_outcome_note("suite_hung", 124, suite_timeout_seconds=780)

    assert "780s" in note
    assert "180s" not in note


# ---------------------------------------------------------------------------
# "Why did this work item fail?" has to be answerable from the ledger. On the
# 2026-07-29 run it was not: tester_turn_completed audited returncode=1 and
# nothing else, and the assertion text existed only in the orchestrator pod's
# log, truncated to 300 characters and rotating away.
# ---------------------------------------------------------------------------


def test_the_failing_output_is_persisted_on_both_runtimes():
    import inspect

    for fn in (activities._tester_pod_sync, activities._run_tester_turn_impl):
        body = inspect.getsource(fn)
        if "tester_turn_completed" not in body:
            continue
        assert '"failure_output"' in body, (
            f"{fn.__name__} audits the tester result without the reason it failed"
        )


def test_the_audit_copy_is_bounded_and_smaller_than_the_coder_copy():
    """The Coder needs the full tail to fix the test; the ledger needs enough
    for a human to see the assertion. Unbounded audit details are how a hung
    poll loop wrote 16k rows once already."""
    assert activities._FAILURE_OUTPUT_AUDIT_CHARS < activities._FAILURE_OUTPUT_CHARS
    assert activities._FAILURE_OUTPUT_AUDIT_CHARS >= 800


def test_the_log_line_no_longer_cuts_the_assertion_off():
    """At 300 chars the cut landed mid-token and printed `e: 'suite'` — the
    actual failure never reached the log."""
    import inspect

    body = inspect.getsource(activities._tester_pod_sync)
    assert "%.300s" not in body
