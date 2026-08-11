"""Validation service configuration.

The L1 commands are part of the policy of the repository under evaluation, not
of the worker's configuration. That is why, in a real run, they are loaded from
``.dse/validation.json`` at the immutable *base SHA*. ``DSE_L1_*_CMD`` variables
are no longer a source of truth: besides allowing drift between workers, they
let an empty command be mistaken for approval.

Operational timeouts and thresholds may still come from the environment. They
do not choose which code to run and therefore do not change the repository's
trusted policy.
"""
from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any

from dse_contracts import GateStatus

if TYPE_CHECKING:
    from dse_validation.sandbox_exec import SandboxExecutor


L1_MANIFEST_PATH = ".dse/validation.json"
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_COMMAND_NAMES = ("lint", "typecheck", "test", "build")
# The two scans belong to the platform, not to the repository, but they run in
# the same activity and therefore spend the same budget. Their timeouts used to
# be literal defaults inside `l1/sast.py` and `l1/secret_scan.py`, reachable
# neither from the manifest nor from the environment.
_SCAN_NAMES = ("sast", "secret_scan")
_STAGE_NAMES = _COMMAND_NAMES + _SCAN_NAMES
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_COMMAND_ARGS = 128
_MAX_ARG_LENGTH = 4096

_DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
# `test` is not one of four interchangeable commands: it is the only one that
# runs the repository's SUITE, and the Tester ran that same suite one activity
# earlier, in the same Pod and the same /workspace, under
# `DSE_TESTER_SUITE_TIMEOUT_SECONDS` — 1200 s today, sized for the 10-15 minute
# suite on 1 vCPU the exit criterion asks for. Two clocks measure one suite and
# only the TIGHTER of them is ever observed, so which service holds it decides
# the DIAGNOSIS: an overrun caught by the Tester is `suite_hung`, named for what
# was measured, worth one Coder turn and then an escalation that says so; the
# same overrun caught here arrives as a `test` finding with status ERROR,
# `coder_retry_count += 1`, up to three paid Coder turns hunting an assert that
# never failed. At 300 s this side was the tighter one, and the suite the Tester
# had just passed died here.
#
# So this is a FLOOR under L1's clock, and it sits deliberately ABOVE the
# Tester's rather than equal to it: L1 runs the suite a SECOND time, and with
# both clocks on the same number which one fires is settled by whichever run
# happens to be slower — a coin toss between the cheap ending and the
# three-turn one. The margin is a quarter of the Tester's clock, the same order
# of headroom the Tester itself keeps over the criterion's 15 minutes.
#
# What the platform guarantees is the ORDER, not the pair of literals, and both
# sides assert it against the other's live value: `tests/test_l1_manifest.py`
# here, `test_the_default_never_outruns_the_l1_clock_on_the_same_suite` there.
_DEFAULT_TEST_TIMEOUT_SECONDS = 1500
_DEFAULT_SCAN_TIMEOUT_SECONDS = {"sast": 120, "secret_scan": 60}
# Floor of every per-stage number derived below. Under a minute a stage timeout
# is not a budget, it is a scheduled ERROR — the same reason the Tester floors
# its own install and suite clocks at 60 s.
_MIN_STAGE_TIMEOUT_SECONDS = 60
# What the SHIPPED defaults resolve to, all six stages together. Kept derived:
# it is the floor of `stage_budget_seconds()`, and a floor below the platform's
# own defaults would refuse every untouched manifest on a small start_to_close.
_DEFAULT_STAGE_BUDGET_SECONDS = (
    (len(_COMMAND_NAMES) - 1) * _DEFAULT_COMMAND_TIMEOUT_SECONDS
    + _DEFAULT_TEST_TIMEOUT_SECONDS
    + sum(_DEFAULT_SCAN_TIMEOUT_SECONDS.values())
)
# Historical range of the scalar `timeout_seconds`. Kept as-is: narrowing it
# would turn manifests that are valid today into escalated work items.
_MAX_MANIFEST_TIMEOUT_SECONDS = 3600
_DEFAULT_ACTIVITY_START_TO_CLOSE_SECONDS = 3600
# Exec time the L1 activity spends OUTSIDE the timed stages, worst case: the two
# `git` calls that load the manifest (15 s each, below) plus the three of
# `plan_compliance.compute_diff_summary` (60 s each). It comes out of the same
# start_to_close, so the stage budget may not claim it.
_PIPELINE_FIXED_COST_SECONDS = 2 * 15 + 3 * 60
# What the activity still has to do AFTER the last stage returns: the
# `validation_runs` insert, the audit row, and encoding the L1Result. Without it
# the budget claims start_to_close down to the last second, and a manifest that
# spends all of it leaves the server killing the activity — no L1Result, retry
# from zero, which is the exact ending the budget guard exists to prevent.
_ACTIVITY_TAIL_MARGIN_SECONDS = 60


def _env_int(name: str, default: int) -> int:
    """Platform-side integer setting.

    A typo in the deployment must not take the whole L1 activity down — which is
    how it would read from the outside, as an infra failure — so anything
    unparseable or non-positive falls back to the default. Floats are accepted
    because the orchestrator publishes ``DSE_ACTIVITY_START_TO_CLOSE_SECONDS``
    as one.
    """

    try:
        value = int(float(os.environ[name]))
    except (KeyError, ValueError):
        return default
    return value if value > 0 else default


def _activity_start_to_close_seconds() -> int:
    """The clock the SERVER will enforce on THIS L1 attempt.

    Read from the live activity first, env only as the fallback. The env var is
    this worker's *copy* of a number that lives in the WORKFLOW's input, and the
    two drift the moment an operator changes one of them — it is in no ConfigMap
    today, so both sides currently sit on the same 3600 s default. Lowering
    start_to_close on the orchestrator alone would leave this guard admitting
    manifests the activity can no longer finish, and an activity the server kills
    returns NO L1Result at all: the precise ending the guard exists to prevent.
    The context survives the ``asyncio.to_thread`` hop the pipeline runs in
    (contextvars are copied into the worker thread), which is how the Tester side
    reads the same clock.
    """

    try:
        from temporalio import activity

        if activity.in_activity():
            scheduled = activity.info().start_to_close_timeout
            if scheduled is not None and scheduled.total_seconds() > 0:
                return int(scheduled.total_seconds())
    except Exception:  # noqa: BLE001 — no context, no temporalio: use the env
        pass
    return _env_int(
        "DSE_ACTIVITY_START_TO_CLOSE_SECONDS", _DEFAULT_ACTIVITY_START_TO_CLOSE_SECONDS
    )


def stage_budget_seconds() -> int:
    """Seconds of stage timeouts that fit inside one L1 activity.

    The whole pipeline runs in a single Temporal activity, so the sum of the
    stage timeouts — not each one separately — is what has to stay under
    ``start_to_close``.

    Floored at the shipped defaults on purpose: a platform configured with a
    start_to_close too small for its own pipeline must not turn every repo's
    manifest into an ERROR. That would report a platform problem as a repository
    failure and escalate the work item for it.
    """

    return max(
        _DEFAULT_STAGE_BUDGET_SECONDS,
        _activity_start_to_close_seconds()
        - _PIPELINE_FIXED_COST_SECONDS
        - _ACTIVITY_TAIL_MARGIN_SECONDS,
    )


def _scan_reserve_seconds() -> int:
    """Seconds the two platform scans hold back from the four repo commands.

    Read from the environment, NOT from the shipped defaults: an operator who
    raises ``DSE_L1_SAST_TIMEOUT_SECONDS`` moves the scans' share of the budget,
    and a reserve frozen at 120+60 would let the resolved sum exceed the budget —
    turning every repository's untouched manifest into an ERROR because of a
    platform setting. Deliberately NOT capped by
    ``stage_timeout_ceiling_seconds()``: that function is derived from this one,
    and the reserve only has to be an upper bound of what the scans can resolve
    to for the guard to stay satisfiable.
    """

    return sum(
        _env_int(f"DSE_L1_{name.upper()}_TIMEOUT_SECONDS", _DEFAULT_SCAN_TIMEOUT_SECONDS[name])
        for name in _SCAN_NAMES
    )


def _even_command_share(budget: int) -> int:
    """What ONE repo command may take when all four take the same and the two
    platform scans take their reserve: ``(budget - sast - secret_scan) / 4``.

    This is what the manifest's single SCALAR asks for — one number for four
    commands — so it bounds the scalar and the scans, and nothing else. It is
    deliberately not the per-stage ceiling: see below.
    """

    return max(_MIN_STAGE_TIMEOUT_SECONDS, (budget - _scan_reserve_seconds()) // len(_COMMAND_NAMES))


def stage_timeout_ceiling_seconds() -> int:
    """Platform ceiling for a single stage — the repository may go under it,
    never over it.

    NOT an even share of the budget. The four commands are not alike: `test`
    runs the suite and the other three are short, so dividing the budget in four
    made the only distribution that matters — the 10-15 minute suite of the exit
    criterion — inexpressible, while protecting nothing that
    ``_validate_timeout_budget`` did not already protect. The sum is what shares
    the activity's start_to_close, and the sum is what is guarded.

    What is left for the ceiling is the one thing the sum cannot say: no single
    stage may take so much that the other mandatory stages are left under a
    workable floor. Hence budget minus the floor of every OTHER command minus
    the scans' reserve — 2970 s at the shipped 3600 s start_to_close. Derived
    from the activity's own budget so the two cannot drift apart, and the
    platform — never the manifest — can override it.
    """

    others = (len(_COMMAND_NAMES) - 1) * _MIN_STAGE_TIMEOUT_SECONDS + _scan_reserve_seconds()
    return _env_int(
        "DSE_L1_TIMEOUT_CEILING_SECONDS",
        max(_MIN_STAGE_TIMEOUT_SECONDS, stage_budget_seconds() - others),
    )


def _legacy_command_cap() -> int:
    """Cap applied to the manifest's single scalar.

    The scalar is one number for four commands, and `test` now resolves to at
    least ``_DEFAULT_TEST_TIMEOUT_SECONDS`` regardless of it, so the largest
    scalar that still fits is the smaller of two shares: an even quarter (when
    the scalar is above the test floor and therefore governs all four) and a
    third of what is left once `test` takes that floor and the scans take their
    reserve. Bounded by the ceiling too. That is what keeps a manifest carrying
    only the old scalar from ever failing the budget guard: it gets clamped,
    never refused.

    Floored at ``_MIN_STAGE_TIMEOUT_SECONDS`` for the same reason every other
    number here is: "not refused" is not the invariant, "not escalated" is. A
    scan reserve wide enough to drive this share to a second does not produce a
    rejected manifest — it produces lint, typecheck and build all timing out,
    three findings with status ERROR, `coder_retry_count += 1` and up to three
    paid Coder turns, from a platform setting no repository asked for. That is
    the outcome the clamp exists to avoid, arrived at by the other door.
    """

    room = stage_budget_seconds() - _scan_reserve_seconds()
    return max(
        _MIN_STAGE_TIMEOUT_SECONDS,
        min(
            stage_timeout_ceiling_seconds(),
            _even_command_share(stage_budget_seconds()),
            (room - _DEFAULT_TEST_TIMEOUT_SECONDS) // (len(_COMMAND_NAMES) - 1),
        ),
    )


def default_scan_timeout_seconds(stage: str) -> int:
    """Timeout of a platform scan for a caller with no manifest at hand.

    Public because ``l1/sast.py`` and ``l1/secret_scan.py`` are the callers: this
    is the mechanism that puts those two numbers inside the escape hatch.
    """

    default = _DEFAULT_SCAN_TIMEOUT_SECONDS[stage]
    return min(
        _env_int(f"DSE_L1_{stage.upper()}_TIMEOUT_SECONDS", default),
        stage_timeout_ceiling_seconds(),
        # ALSO the even share, and not only the ceiling: the ceiling can be
        # overridden by the platform while `_even_command_share` — and therefore
        # `_legacy_command_cap` — is derived from the RAW scan reserve. Without
        # this third bound the two settings can disagree, the resolved scans
        # claim more of the budget than the clamped scalar left them, and a
        # manifest carrying only the old scalar gets REFUSED by
        # `_validate_timeout_budget` — the escalated work item this whole path
        # exists to prevent. Reproduced with DSE_L1_SAST_TIMEOUT_SECONDS=3000 +
        # DSE_L1_TIMEOUT_CEILING_SECONDS=3000 + start_to_close=600: 4563s of
        # resolved stages against a 2580s budget, from a manifest nobody edited.
        _even_command_share(stage_budget_seconds()),
    )


def _default_test_timeout_seconds(scalar: int) -> int:
    """The `test` clock when the manifest states no per-stage policy for it.

    A floor, not an override. The scalar is a statement about "commands" in
    general — one number for four — and cannot say "test is the long one"; only
    ``timeouts.test`` can, and it wins outright. Until a repository says it, L1
    must not run the suite on a shorter clock than the Tester already gave the
    very same suite, or it reports as a repository failure a run the platform cut
    short. A scalar ABOVE the floor still governs: raising it lengthens `test`.
    """

    return min(max(scalar, _DEFAULT_TEST_TIMEOUT_SECONDS), stage_timeout_ceiling_seconds())


def _fit_to_budget(resolved: dict[str, int]) -> dict[str, int]:
    """Shrink the stages to fit the activity instead of refusing the manifest.

    "Clamped, never refused" is this module's rule for every other input — see
    `timeout_seconds` in `L1Config.__init__` and `_legacy_command_cap`. Declared
    per-stage values were the one exception, and the exception cost a work item.

    `bmo-fee-calculator-be-dse` declared 900+900+900+900+120+120 = 3840s against
    a 3330s budget. `_validate_timeout_budget` refused the whole manifest, so
    every command read NOT_CONFIGURED and the item escalated with ZERO stages
    run — after the Tester had already passed with a real Java test. The repo
    was asking for 15 minutes per stage; it got none. Eight of those 510 excess
    seconds were real: `typecheck` there runs a command byte-identical to
    `lint`.

    Scaling is proportional, so a manifest that says "test is the long one"
    keeps saying it, just on a shorter clock. Each stage is floored at
    `_MIN_STAGE_TIMEOUT_SECONDS`; if even the floors do not fit — which needs a
    start_to_close far below anything the workflow schedules, since
    `stage_budget_seconds()` never returns less than 2580 against a 360s
    floor-sum — the dict is returned unchanged and `_validate_timeout_budget`
    still refuses it. Refusal survives as the last resort, not the first.
    """

    budget = stage_budget_seconds()
    total = sum(resolved.values())
    floor = _MIN_STAGE_TIMEOUT_SECONDS
    if total <= budget or budget < floor * len(resolved):
        return resolved

    fitted = {
        name: max(floor, int(seconds * budget / total)) for name, seconds in resolved.items()
    }
    # Flooring and integer truncation can leave the sum above the budget again.
    # Take the excess off the longest stages first, never below the floor.
    excess = sum(fitted.values()) - budget
    for name in sorted(fitted, key=lambda k: -fitted[k]):
        if excess <= 0:
            break
        take = min(excess, fitted[name] - floor)
        fitted[name] -= take
        excess -= take
    return fitted


def _budget_fit_note(before: dict[str, int], after: dict[str, int]) -> str:
    """What to tell the repository when its clocks were shortened."""

    changed = ", ".join(
        f"{name} {before[name]}s->{after[name]}s" for name in _STAGE_NAMES if before[name] != after[name]
    )
    return (
        f"; the declared stage timeouts summed to {sum(before.values())}s, over the "
        f"{stage_budget_seconds()}s available inside the L1 activity, so they were "
        f"scaled to fit rather than refused ({changed})"
    )


def _requested_stage_timeouts(scalar: int, declared: dict[str, int] | None) -> dict[str, int]:
    """What the repository asked for, before it is made to fit the activity."""

    resolved = {name: scalar for name in _COMMAND_NAMES}
    resolved["test"] = _default_test_timeout_seconds(scalar)
    resolved.update({name: default_scan_timeout_seconds(name) for name in _SCAN_NAMES})
    resolved.update(declared or {})
    return resolved


def _resolve_stage_timeouts(scalar: int, declared: dict[str, int] | None) -> dict[str, int]:
    resolved = _fit_to_budget(_requested_stage_timeouts(scalar, declared))
    # NOT clamped, and that is a known hole with a recommendation rather than a
    # fix. The floor above `test` protects the ORDER of two clocks measuring one
    # suite: the Tester's must fire first (cheap `suite_hung`, one Coder turn)
    # rather than L1's (a `test` ERROR worth up to three paid turns hunting an
    # assertion that never failed). A DECLARED value walks straight through it —
    # the testbed's own manifest says `timeouts.test: 900` against the Tester's
    # 1200.
    #
    # Raising the declared value to the floor was tried and reverted: it takes
    # that manifest's stage sum from 3240s to 3840s against a 3330s budget, so
    # the manifest is REFUSED and the work item escalates instantly — trading a
    # rare mis-diagnosis for a certain outage.
    #
    # The order has to be restored from the other side: derive the Tester's
    # suite clock as min(its default, this value minus a margin), so the Tester
    # stays the tighter clock without inflating a budget it does not spend.
    # That is a change in sandbox-runtime, not here.
    return resolved


class L1ManifestError(ValueError):
    """Missing or invalid manifest, carrying an explicit gate outcome."""

    def __init__(self, status: GateStatus, detail: str, summary: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        # `detail` is free to name the offending manifest keys — it only reaches
        # validation_runs, which retention can clean. `summary` is what the
        # append-only audit_log gets, so any message interpolating content the
        # CUSTOMER chose (object keys, command argv, file paths) must pass an
        # explicit one. The messages that interpolate only `source`, stage names
        # from the fixed set, or integers are platform text and reuse `detail`.
        self.summary = summary or detail


def _validate_command(name: str, raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise L1ManifestError(
            GateStatus.ERROR,
            f"commands.{name} must be a JSON array of arguments, never a shell string",
        )
    if len(raw) > _MAX_COMMAND_ARGS:
        raise L1ManifestError(
            GateStatus.ERROR,
            f"commands.{name} exceeds the limit of {_MAX_COMMAND_ARGS} arguments",
        )
    command: list[str] = []
    for index, arg in enumerate(raw):
        if not isinstance(arg, str) or not arg or "\x00" in arg:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"commands.{name}[{index}] must be a non-empty string with no NUL byte",
            )
        if len(arg) > _MAX_ARG_LENGTH:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"commands.{name}[{index}] exceeds {_MAX_ARG_LENGTH} characters",
            )
        command.append(arg)
    return command


def _validate_timeouts(raw: Any, *, source: str) -> dict[str, int]:
    """Per-stage block. Optional: absent means "the scalar governs everything",
    which is what every manifest written so far says."""

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise L1ManifestError(
            GateStatus.ERROR,
            f"manifest {source}: timeouts must be a JSON object of stage -> seconds",
        )
    unknown = sorted(set(raw) - set(_STAGE_NAMES))
    if unknown:
        raise L1ManifestError(
            GateStatus.ERROR,
            f"manifest {source} has unknown timeouts: {unknown} "
            f"(valid stages: {list(_STAGE_NAMES)})",
            summary=(
                f"manifest has {len(unknown)} unknown timeout stage(s) "
                f"(valid stages: {list(_STAGE_NAMES)})"
            ),
        )
    ceiling = stage_timeout_ceiling_seconds()
    declared: dict[str, int] = {}
    for name in _STAGE_NAMES:
        if name not in raw:
            continue
        value = raw[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source}: timeouts.{name} must be a positive integer of seconds",
            )
        if value > ceiling:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source}: timeouts.{name}={value}s exceeds the platform "
                f"ceiling of {ceiling}s per stage",
            )
        declared[name] = value
    return declared


def _validate_timeout_budget(resolved: dict[str, int], *, source: str) -> None:
    """The stages run one after another inside a single activity, so what has to
    fit is the SUM. Nothing validated it before: four commands at the 3600 s the
    scalar allowed added up to well past the activity's own start_to_close."""

    total = sum(resolved.values())
    budget = stage_budget_seconds()
    if total <= budget:
        return
    requested = ", ".join(f"{name}={resolved[name]}s" for name in _STAGE_NAMES)
    raise L1ManifestError(
        GateStatus.ERROR,
        f"manifest {source}: the stage timeouts add up to {total}s, over the {budget}s "
        f"available inside the L1 activity (start_to_close of "
        f"{_activity_start_to_close_seconds()}s minus {_PIPELINE_FIXED_COST_SECONDS}s "
        f"of git execs the pipeline itself runs and {_ACTIVITY_TAIL_MARGIN_SECONDS}s "
        f"to persist the result); requested {requested}",
    )


class L1Config:
    """L1 policy materialized from a traceable source.

    The empty constructor is deliberately *fail-closed*: every command ends up
    not configured. Unit tests that need explicit commands use
    :meth:`for_test_repo`; the production path uses
    :meth:`from_trusted_manifest`.
    """

    def __init__(
        self,
        *,
        lint_cmd: list[str] | None = None,
        typecheck_cmd: list[str] | None = None,
        test_cmd: list[str] | None = None,
        build_cmd: list[str] | None = None,
        timeout_seconds: int | None = None,
        timeouts: dict[str, int] | None = None,
        sast_severity_gate: str | None = None,
        source: str = "not-configured",
        manifest_status: GateStatus = GateStatus.NOT_CONFIGURED,
        manifest_detail: str = "L1 manifest not loaded",
        manifest_summary: str | None = None,
    ) -> None:
        self.lint_cmd = list(lint_cmd or [])
        self.typecheck_cmd = list(typecheck_cmd or [])
        self.test_cmd = list(test_cmd or [])
        self.build_cmd = list(build_cmd or [])
        # Clamped, never refused: a repository whose scalar is above the ceiling
        # is asking for more than the activity has, but refusing it here would
        # escalate a work item over a manifest that was valid yesterday.
        self.timeout_seconds = min(
            timeout_seconds
            or _env_int("DSE_L1_TIMEOUT_SECONDS", _DEFAULT_COMMAND_TIMEOUT_SECONDS),
            _legacy_command_cap(),
        )
        self.timeouts = _resolve_stage_timeouts(self.timeout_seconds, timeouts)
        self.sast_severity_gate = (
            sast_severity_gate or os.environ.get("DSE_L1_SAST_SEVERITY_GATE", "MEDIUM")
        ).upper()
        self.source = source
        self.manifest_status = manifest_status
        self.manifest_detail = manifest_detail
        self.manifest_summary = manifest_summary or manifest_detail

    def timeout_for(self, stage: str) -> int:
        """Seconds allowed for one pipeline stage.

        ``timeout_seconds`` remains the default of lint, typecheck and build, so
        a manifest carrying only the old scalar behaves exactly as it did for
        them; `test` additionally has the floor described in
        ``_default_test_timeout_seconds``.
        """

        return self.timeouts.get(stage, self.timeout_seconds)

    @classmethod
    def for_test_repo(cls) -> "L1Config":
        """Explicit config for local fixtures; never called by the worker."""

        return cls(
            lint_cmd=["ruff", "check", "."],
            typecheck_cmd=["mypy", "."],
            test_cmd=["pytest", "-q"],
            build_cmd=["python", "-m", "compileall", "-q", "."],
            source="explicit-test-config",
            manifest_status=GateStatus.PASS,
            manifest_detail="explicit test configuration",
        )

    @classmethod
    def from_trusted_manifest(
        cls,
        executor: "SandboxExecutor",
        base_sha: str,
        *,
        manifest_path: str = L1_MANIFEST_PATH,
    ) -> "L1Config":
        """Loads the policy from the base commit, never from the mutable checkout.

        The API takes arguments as an array and passes them through without a
        shell. The manifest path is constant in the production caller; the
        parameter exists only for contract tests.
        """

        source = f"{base_sha}:{manifest_path}"
        try:
            if not _FULL_GIT_SHA_RE.fullmatch(base_sha):
                raise L1ManifestError(
                    GateStatus.ERROR,
                    "base_sha must be a full Git SHA of 40 or 64 hexadecimal characters",
                )

            verify = executor.run(
                ["git", "cat-file", "-e", f"{base_sha}^{{commit}}"], timeout=15
            )
            if not verify.ok:
                raise L1ManifestError(
                    GateStatus.ERROR,
                    f"base_sha {base_sha} does not exist as a commit in the sandbox",
                )

            rendered = executor.run(
                ["git", "show", f"{base_sha}:{manifest_path}"], timeout=15
            )
            if not rendered.ok:
                raise L1ManifestError(
                    GateStatus.NOT_CONFIGURED,
                    f"trusted manifest missing at {source}",
                )
            if len(rendered.stdout.encode("utf-8")) > _MAX_MANIFEST_BYTES:
                raise L1ManifestError(
                    GateStatus.ERROR,
                    f"manifest {source} exceeds {_MAX_MANIFEST_BYTES} bytes",
                )
            try:
                payload = json.loads(rendered.stdout)
            except json.JSONDecodeError as exc:
                raise L1ManifestError(
                    GateStatus.ERROR,
                    f"manifest {source} contains invalid JSON: {exc.msg}",
                ) from exc
            return cls._from_manifest_payload(payload, source=source)
        except L1ManifestError as exc:
            return cls(
                source=source,
                manifest_status=exc.status,
                manifest_detail=exc.detail,
                manifest_summary=exc.summary,
            )

    @classmethod
    def _from_manifest_payload(cls, payload: Any, *, source: str) -> "L1Config":
        if not isinstance(payload, dict):
            raise L1ManifestError(GateStatus.ERROR, f"manifest {source} must be a JSON object")
        allowed = {"version", "commands", "timeout_seconds", "timeouts", "sast_severity_gate"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source} has unknown fields: {unknown}",
                summary=(
                    f"manifest has {len(unknown)} unknown field(s) "
                    f"(valid fields: {sorted(allowed)})"
                ),
            )
        if payload.get("version") != 1:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source} requires version=1",
            )
        commands = payload.get("commands")
        if not isinstance(commands, dict):
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source} requires a commands object",
            )
        unknown_commands = sorted(set(commands) - set(_COMMAND_NAMES))
        if unknown_commands:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source} has unknown commands: {unknown_commands}",
                summary=f"manifest has {len(unknown_commands)} unknown command(s)",
            )

        timeout = payload.get(
            "timeout_seconds",
            _env_int("DSE_L1_TIMEOUT_SECONDS", _DEFAULT_COMMAND_TIMEOUT_SECONDS),
        )
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= _MAX_MANIFEST_TIMEOUT_SECONDS
        ):
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source}: timeout_seconds must be between 1 and "
                f"{_MAX_MANIFEST_TIMEOUT_SECONDS}",
            )
        declared_timeouts = _validate_timeouts(payload.get("timeouts"), source=source)
        effective_scalar = min(timeout, _legacy_command_cap())
        requested = _requested_stage_timeouts(effective_scalar, declared_timeouts)
        fitted = _resolve_stage_timeouts(effective_scalar, declared_timeouts)
        _validate_timeout_budget(fitted, source=source)
        detail = f"trusted manifest loaded from {source}"
        if fitted != requested:
            detail += _budget_fit_note(requested, fitted)
        if effective_scalar != timeout:
            detail += (
                f"; timeout_seconds={timeout}s clamped to {effective_scalar}s per command, "
                f"the largest single scalar that fits the L1 activity's budget of "
                f"{stage_budget_seconds()}s (declare timeouts.<stage> to spend it unevenly)"
            )
        severity = payload.get(
            "sast_severity_gate", os.environ.get("DSE_L1_SAST_SEVERITY_GATE", "MEDIUM")
        )
        if not isinstance(severity, str) or severity.upper() not in {"LOW", "MEDIUM", "HIGH"}:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source}: sast_severity_gate must be LOW, MEDIUM or HIGH",
            )

        parsed = {name: _validate_command(name, commands.get(name)) for name in _COMMAND_NAMES}
        return cls(
            lint_cmd=parsed["lint"],
            typecheck_cmd=parsed["typecheck"],
            test_cmd=parsed["test"],
            build_cmd=parsed["build"],
            timeout_seconds=timeout,
            timeouts=declared_timeouts,
            sast_severity_gate=severity,
            source=source,
            manifest_status=GateStatus.PASS,
            manifest_detail=detail,
        )


class GitHubConfig:
    def __init__(self) -> None:
        self.app_id = os.environ.get("GITHUB_APP_ID")
        self.private_key_pem = os.environ.get("GITHUB_APP_PRIVATE_KEY")
        self.installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")
        self.api_base_url = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com")

    @property
    def is_configured(self) -> bool:
        return bool(self.app_id and self.private_key_pem and self.installation_id)


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


class StrictModeConfig:
    """WSE-E3-T8 — per repo/tenant "strict mode" flag: instead of opening the PR,
    the finalizer only pushes the branch and returns a `PrRef` with `compare_url`
    filled in (`pr_number is None`) + posts the compare link on the tracking
    comment; a human opens the PR with 1 click and the workflow adopts that PR
    (same WorkItem).

    In Phase 2 the `PrRef` contract gained `compare_url` and an optional
    `pr_number`, so this IS now wired into `finalize_pr_core` (see
    `github/pr_finalizer.py`).

    Flag resolution (most specific wins), all via env because `tenant_config`
    (WS-F, the fairness/budget/flags table) does not expose a strict-mode field yet:
      1. `DSE_WSE_STRICT_MODE_TENANT_<TENANT>_<REPO>` (repo with `/`->`_`, upper)
      2. `DSE_WSE_STRICT_MODE_TENANT_<TENANT>`
      3. `DSE_WSE_STRICT_MODE_REPOS` (comma-separated list of `tenant:repo`)
      4. `DSE_WSE_STRICT_MODE` (global, default false)
    Once WS-F publishes the per-tenant flag in `tenant_config`, only
    `is_strict_for` changes to read from there — the signature stays the same."""

    def __init__(self) -> None:
        self.global_enabled = _env_bool("DSE_WSE_STRICT_MODE")
        # Phase 1 compat: `.enabled` still exists (== global flag).
        self.enabled = self.global_enabled
        self._repo_allowlist = {
            entry.strip()
            for entry in os.environ.get("DSE_WSE_STRICT_MODE_REPOS", "").split(",")
            if entry.strip()
        }

    @staticmethod
    def _slug(value: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in value).upper()

    def is_strict_for(self, tenant_id: str, repo: str) -> bool:
        specific = os.environ.get(
            f"DSE_WSE_STRICT_MODE_TENANT_{self._slug(tenant_id)}_{self._slug(repo)}"
        )
        if specific is not None:
            return specific.lower() in ("1", "true", "yes")
        per_tenant = os.environ.get(f"DSE_WSE_STRICT_MODE_TENANT_{self._slug(tenant_id)}")
        if per_tenant is not None:
            return per_tenant.lower() in ("1", "true", "yes")
        if f"{tenant_id}:{repo}" in self._repo_allowlist:
            return True
        return self.global_enabled


class GarageConfig:
    """WSE-E5-T12 (Phase 3) — Garage artifact store (self-hosted S3, no SaaS).

    Default endpoints point at the `garage` service in docker-compose.wse.yml
    (ports 3900/3903 reserved in CONVENTIONS.md). The admin token is DEV-ONLY —
    production injects it via Vault/ESO (WS-F). The S3 key used by the service is
    created (idempotently) through the admin API by the bootstrap itself — no S3
    secret lives in env/files."""

    def __init__(self) -> None:
        self.s3_endpoint = os.environ.get("DSE_GARAGE_S3_ENDPOINT", "http://localhost:3900")
        self.admin_endpoint = os.environ.get("DSE_GARAGE_ADMIN_ENDPOINT", "http://localhost:3903")
        self.admin_token = os.environ.get("DSE_GARAGE_ADMIN_TOKEN", "dse_garage_admin_dev")
        self.region = os.environ.get("DSE_GARAGE_REGION", "garage")
        self.key_name = os.environ.get("DSE_GARAGE_KEY_NAME", "dse-validation")
        # declared capacity of the single-node dev node (layout)
        self.layout_capacity = os.environ.get("DSE_GARAGE_LAYOUT_CAPACITY", "10G")
        # multipart from 5 MiB up (S3 protocol minimum; revised ADR-18)
        self.multipart_threshold_bytes = int(
            os.environ.get("DSE_GARAGE_MULTIPART_THRESHOLD", str(5 * 1024 * 1024))
        )
        self.bucket_prefix = os.environ.get("DSE_GARAGE_BUCKET_PREFIX", "dse-tenant-")


class PreviewConfig:
    """WSE-E4-T10 (Phase 3) — per-PR previews via Argo CD on the real k3d cluster.

    `repo_dir` is the BARE git repo of manifests served to the cluster by the
    `dse-wse-gitserver` container (nginx, dumb HTTP) — the host writes through
    the filesystem and Argo CD reads via http://dse-wse-gitserver/<name>.git
    (dse_net network)."""

    def __init__(self) -> None:
        self.kube_context = os.environ.get("DSE_PREVIEW_KUBE_CONTEXT", "k3d-dse-preview")
        self.repo_dir = os.environ.get(
            "DSE_PREVIEW_REPO_DIR",
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preview_repo"
            ),
        )
        # repo URL AS SEEN FROM INSIDE the cluster (dse_net network)
        self.repo_url_in_cluster = os.environ.get(
            "DSE_PREVIEW_REPO_URL", "http://dse-wse-gitserver/preview-manifests.git"
        )
        self.argocd_namespace = os.environ.get("DSE_PREVIEW_ARGOCD_NS", "argocd")
        self.applicationset_name = os.environ.get("DSE_PREVIEW_APPSET_NAME", "dse-previews")
        self.default_ttl_seconds = int(os.environ.get("DSE_PREVIEW_TTL_SECONDS", "3600"))
        # ADR-26: cap on concurrent previews per tenant from day 1.
        # Per-tenant override lives in the wse_preview_caps table; this is the default.
        self.default_max_concurrent = int(os.environ.get("DSE_PREVIEW_MAX_CONCURRENT", "3"))
        # image of the preview Deployment (pinned, P7). Plan 08 §D (D4): by
        # default it brings up an nginx placeholder (proves the flow); in a
        # pilot, point this env at the REAL image of the PR (built/pushed in
        # CI) — per-PR build + registry come along with the cluster (infra
        # decision).
        self.preview_image = os.environ.get("DSE_PREVIEW_IMAGE", "nginx:1.27-alpine")
        # plan 08 §D (D3): EXTERNAL host reachable from the browser. Without it
        # the URL is the cluster-internal DNS (not clickable from outside). E.g.:
        #   local dev : "http://{namespace}.preview.localhost:8081" (k3d's
        #               Traefik published on localhost:8081; *.localhost
        #               resolves to 127.0.0.1 in modern browsers)
        #   tunnel    : "https://{namespace}.preview.YOURDOMAIN.com" (cloudflared
        #               pointing at localhost:8081 — same Ingress)
        #   VPS later : same template, only the DNS changes. See
        #               infra/preview-exposure.md.
        # `{namespace}` is substituted. When set, build_manifests also generates
        # the INGRESS with that hostname (otherwise no Ingress is created).
        self.external_host_template = os.environ.get("DSE_PREVIEW_EXTERNAL_HOST", "")
        # G-2 (2026-08-09): imagem da receita `deployable` (kind do
        # paths-filter). O build vem do .dse/validation.json do repo (a régua
        # do L1); a imagem só precisa de JDK — jq/git/ssh instalados no boot.
        self.deployable_image = os.environ.get(
            "DSE_PREVIEW_DEPLOYABLE_IMAGE", "eclipse-temurin:17-jdk"
        )
        # G-3 degrau 1: alvo estável do proxy /api do preview FE quando não há
        # irmão com preview vivo. MEDIDO 2026-08-09: não existe API estável na
        # VPS hoje — nasce configurável e vazio; vazio = sem proxy (o shell
        # mostra o 404 real em vez de um alvo inventado).
        self.fe_api_fallback = os.environ.get("DSE_PREVIEW_FE_API_BASE", "")
        # G-1: a secret AGREGADA que o operador semeia no namespace do DSE
        # (runbook deploy/vps/preview-deploy-keys.md) — um item por repo, com a
        # chave SSH read-only daquele repo. O DSE só COPIA dela para o
        # namespace do preview; nunca gera nem registra chave (decisão (a):
        # material de credencial é do operador).
        self.deploy_keys_secret = os.environ.get(
            "DSE_PREVIEW_DEPLOY_KEYS_SECRET", "dse-preview-deploy-keys"
        )
        self.dse_namespace = os.environ.get("DSE_NAMESPACE", "dse")
        # Nome do banco efêmero do preview `deployable`. Default `fee` porque o
        # testbed Java declara `hibernate.default_catalog: fee` — no Postgres,
        # catalog é o BANCO, então um nome diferente faz o Hibernate emitir
        # `fee.tabela` contra um banco que não existe.
        self.preview_db_name = os.environ.get("DSE_PREVIEW_DB_NAME", "fee")
        self.ingress_class = os.environ.get("DSE_PREVIEW_INGRESS_CLASS", "traefik")
        #: O resolver ACME que emite o certificado do preview. Default `le`
        #: porque é o que os ingresses do próprio DSE já usam neste cluster —
        #: um preview sem TLS entrega página em branco em qualquer app que
        #: autentique (medido na PR #19).
        self.tls_cert_resolver = os.environ.get("DSE_PREVIEW_TLS_RESOLVER", "le")
        # plan 08 §D (D4): port the PR's APP listens on inside the container
        # (Service/Ingress always publish 80 → targetPort=app_port).
        self.app_port = int(os.environ.get("DSE_PREVIEW_APP_PORT", "80"))
        # How the preview serves the PR.
        #   "image"  — deploy a prebuilt image (the original design: needs a
        #              Docker daemon to build it and a registry to pull it from).
        #   "source" — run the branch straight from source in the container
        #              (clone + install + start). This is the only mode that
        #              works on a cluster with no Docker and no registry, which
        #              is exactly what the gVisor/k8s sandbox substrate is.
        self.mode = os.environ.get("DSE_PREVIEW_MODE", "image")
        # How the manifests reach the cluster.
        #   "gitops" — write to the manifests repo and let Argo CD sync it.
        #   "kubectl" — apply directly. No Argo CD to install and no GitOps repo
        #              to host; the tradeoff is losing Argo's own GC, so the TTL
        #              reaper becomes the only thing that cleans previews up.
        self.apply_mode = os.environ.get("DSE_PREVIEW_APPLY", "gitops")
        # Base image for `mode="source"`: it only needs a runtime plus git, so
        # the language runtime image is enough. Pinned (P7).
        self.source_image = os.environ.get("DSE_PREVIEW_SOURCE_IMAGE", "node:22-alpine")
        # Port the app listens on when run from source. Node/Express honour
        # $PORT, so the container sets it and the Service targets the same one.
        self.source_port = int(os.environ.get("DSE_PREVIEW_SOURCE_PORT", "3000"))
        # Cloning and installing dependencies takes far longer than starting a
        # prebuilt image, so readiness has to be patient or the Deployment is
        # declared failed while npm is still resolving.
        #
        # 900 desde 2026-08-11 (era 300). Medido no preview da PR #19: o
        # container ainda estava em `Cloning into '/srv/app'` aos 3m37s, e o
        # `npm install` de um Angular real mais a primeira compilação do
        # `ng serve` não cabem em 5 minutos NENHUMA vez. O prazo antigo
        # garantia `degraded` mesmo quando tudo estava indo bem — declarava
        # fracasso enquanto o npm ainda resolvia, que é literalmente o que o
        # comentário acima manda evitar.
        self.source_ready_timeout_s = int(os.environ.get("DSE_PREVIEW_SOURCE_READY_TIMEOUT", "900"))
        # D4 — build of the REAL PR image: when true and the task workspace has
        # a Dockerfile, the preview builds/pushes the image of the PR head
        # instead of the placeholder. push_ref = as seen by the local daemon
        # (localhost:5510); pull_ref = as seen by the cluster nodes
        # (k3d-dse-registry:5510).
        self.build_image = _env_bool("DSE_PREVIEW_BUILD_IMAGE", "false")
        self.registry_push = os.environ.get("DSE_PREVIEW_REGISTRY_PUSH", "localhost:5510")
        self.registry_pull = os.environ.get("DSE_PREVIEW_REGISTRY_PULL", "k3d-dse-registry:5510")
        self.build_timeout_s = int(os.environ.get("DSE_PREVIEW_BUILD_TIMEOUT_S", "420"))
        self.sync_timeout_s = int(os.environ.get("DSE_PREVIEW_SYNC_TIMEOUT_S", "180"))

    def preview_url_for(self, namespace: str) -> str:
        """Preview URL. External (browser-reachable) when
        `external_host_template` is set; otherwise the cluster-internal DNS
        (useful only from the inside — the link still shows up on the PR, D1,
        but D3 is what makes it clickable)."""
        if self.external_host_template:
            url = self.external_host_template.replace("{namespace}", namespace)
            # HTTPS SEMPRE, mesmo que o template diga http. Desde a rc.82 o
            # Ingress do preview declara `entrypoints: websecure` e o router só
            # atende na 443 — mas o template vive no values, em outro
            # repositório de decisões, e os dois divergiram na primeira
            # oportunidade: o preview subiu com TLS e o link que iria para a PR
            # era `http://`, sem responder.
            #
            # Normalizar aqui não é gentileza com um values errado: é fechar a
            # única porta pela qual o esquema anunciado pode discordar do que o
            # Ingress serve. Corrigir só o values conserta hoje; isto impede
            # amanhã.
            if url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            return url
        # Sem template não há Ingress nem certificado: este é o DNS interno do
        # cluster, e forçar https aqui quebraria o único caminho que funciona
        # sem TLS.
        return f"http://preview.{namespace}.svc.cluster.local"

    def external_hostname_for(self, namespace: str) -> str | None:
        """Bare hostname (no scheme/port) for the Ingress `host` field —
        derived from the same URL template (D3). None when not configured."""
        if not self.external_host_template:
            return None
        url = self.external_host_template.replace("{namespace}", namespace)
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return host.split(":", 1)[0] or None


class L2Config:
    """WSE-E2 — parameters of the L2 fresh-context loop + bounded fix-retries.

    - `max_fix_retries`: max number of L2->Coder round-trips before escalating
      to an operator (P6 decline-never — never "keeps trying forever").
    - `budget_cap_usd`: cap on the loop's accumulated cost (L2 + re-Coder); on
      reaching it, escalate instead of spending more (P6). 0 = no cost cap
      (only the iteration cap applies)."""

    def __init__(self) -> None:
        self.max_fix_retries = int(os.environ.get("DSE_L2_MAX_FIX_RETRIES", "3"))
        self.budget_cap_usd = float(os.environ.get("DSE_L2_BUDGET_CAP_USD", "0") or "0")
