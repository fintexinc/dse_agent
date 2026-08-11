#!/usr/bin/env python3
"""Run every Python test suite in an isolated pytest process.

The repository intentionally contains one ``tests`` package per component.
Collecting all of them in a single pytest process makes Python resolve the
first ``tests.conftest`` for every component, which both breaks collection and
can silently install the wrong fixtures.  This runner is the canonical entry
point for local and CI test execution: each suite gets its own interpreter
process and its own JUnit artifact.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from collections.abc import Iterable
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

SUITE_GROUPS: dict[str, tuple[str, ...]] = {
    "tooling": ("tests",),
    "contracts": ("packages/contracts",),
    "packages": ("packages/dse_audit", "packages/dse_identity"),
    "adapters": (
        "services/adapter-github",
        "services/adapter-jira",
        "services/adapter-slack",
        "services/adapter-teams",
    ),
    "control-plane": (
        "services/console-projector",
        "services/ingest-gateway",
        "services/orchestrator",
    ),
    "runtime": ("services/sandbox-runtime",),
    "validation": ("services/validation",),
    "security": ("services/egress-proxy", "services/model-gateway"),
    "platform": ("services/platform",),
    # A shard of control-plane; see SUITE_SHARDS.
    "control-plane-slow": ("services/orchestrator",),
}

# Sharding by FILE. A group whose wall clock dominates the matrix is carved into
# two groups that run as separate CI jobs, in parallel.
#
# Why by file and not by directory: `control-plane` is 275s of suite time and
# `services/orchestrator` alone is 270s of it, so peeling off the other two
# components saves ~11s. Inside the orchestrator, one FILE —
# test_plan_approval_timeout.py — is 119s: 16 tests that each stand up a real
# Temporal time-skipping server and drive a whole work-item lifecycle through
# it. Nothing coarser than a file can split that.
#
# The complement is DERIVED, never enumerated: the shard group runs exactly the
# files below, and the parent group runs the suite with exactly those files
# ignored. A test file added tomorrow therefore lands in the parent group
# automatically and cannot fall through the gap between the two.
#
# "Os dois grupos juntos rodam cada arquivo exatamente uma vez" só valia para
# DUAS INVOCAÇÕES separadas — que é como o CI roda (um job por grupo). Numa
# invocação só, pedir os dois lados (ou `--group all`, o `make test`) caía no
# ramo do pai COM os --ignore, e os três arquivos não rodavam em lugar nenhum.
# É o que `covered_groups` em `pytest_targets` corrige.
SUITE_SHARDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "control-plane-slow": (
        "services/orchestrator",
        (
            "tests/test_plan_approval_timeout.py",
            "tests/test_phase4_merge_base_and_learning.py",
            "tests/test_iteration_caps_debounce.py",
        ),
    ),
}

SUITE_COVERAGE_TARGETS: dict[str, str] = {
    "tests": "scripts",
    "packages/contracts": "packages/contracts/dse_contracts",
    "packages/dse_audit": "packages/dse_audit/dse_audit",
    "packages/dse_identity": "packages/dse_identity/dse_identity",
    "services/adapter-github": "services/adapter-github/adapter_github",
    "services/adapter-jira": "services/adapter-jira/adapter_jira",
    "services/adapter-slack": "services/adapter-slack/adapter_slack",
    "services/adapter-teams": "services/adapter-teams/adapter_teams",
    "services/console-projector": "services/console-projector/console_projector",
    "services/egress-proxy": "services/egress-proxy/egress_proxy",
    "services/ingest-gateway": "services/ingest-gateway/ingest_gateway",
    "services/model-gateway": "services/model-gateway/model_gateway_client",
    "services/orchestrator": "services/orchestrator/src/dse_orchestrator",
    "services/platform": "services/platform/dse_platform",
    "services/sandbox-runtime": "services/sandbox-runtime/sandbox_runtime",
    "services/validation": "services/validation/dse_validation",
}

# Coverage ratchet (plan 09 F4): the floor NEVER goes down — when a suite's
# coverage goes up, raise its floor in the SAME PR. Suites with no entry are
# report-only until the first CI measurement seeds their floor (local
# measurements 2026-07-23: contracts 94%, tooling 23%).
# Per-test ceiling (plan 09 F4). Generous: integration tests with Temporal
# time-skipping + chaos are the slowest, but no healthy test should exceed ~2min.
# A hang becomes a NAMED failure instead of a dead job with no diagnosis.
DEFAULT_TEST_TIMEOUT_S = 180
SUITE_TIMEOUTS: dict[str, int] = {
    # The orchestrator suite is the only one that starts a real Temporal
    # environment per test, and the CI wait tests chain continue_as_new
    # executions inside one. Measured on this laptop with the exact CI command
    # (`--coverage`, Postgres and Temporal from compose): 149 tests in 239s,
    # slowest single test 27.8s. The CI runner has two cores shared with
    # Postgres, Temporal and coverage instrumentation, and there the same tests
    # crossed 180s and the job died with a stack dump instead of a test name.
    # Raised rather than removed: the ceiling exists so a hang is a NAMED
    # failure, and 27.8s of headroom to 420s still catches one.
    "services/orchestrator": 420,
}

SUITE_COVERAGE_FLOORS: dict[str, int] = {
    "packages/contracts": 90,
    "tests": 20,
}


def _registered_suites(groups: Iterable[str]) -> list[str]:
    suites: list[str] = []
    for group in groups:
        for suite in SUITE_GROUPS[group]:
            if suite not in suites:
                suites.append(suite)
    return suites


def _discovered_suites() -> set[str]:
    suites: set[str] = {"tests"} if any((ROOT / "tests").glob("test_*.py")) else set()
    for parent in (ROOT / "packages", ROOT / "services"):
        for tests_dir in parent.glob("*/tests"):
            if any(tests_dir.glob("test_*.py")):
                suites.add(str(tests_dir.parent.relative_to(ROOT)))
    return suites


def _validate_manifest() -> None:
    _validate_shards()
    registered = set(_registered_suites(SUITE_GROUPS))
    discovered = _discovered_suites()
    missing = sorted(discovered - registered)
    stale = sorted(registered - discovered)
    coverage_missing = sorted(registered - set(SUITE_COVERAGE_TARGETS))
    coverage_stale = sorted(set(SUITE_COVERAGE_TARGETS) - registered)
    if missing or stale or coverage_missing or coverage_stale:
        details: list[str] = []
        if missing:
            details.append(f"unregistered suites: {', '.join(missing)}")
        if stale:
            details.append(f"missing suite directories: {', '.join(stale)}")
        if coverage_missing:
            details.append(f"coverage targets missing: {', '.join(coverage_missing)}")
        if coverage_stale:
            details.append(f"stale coverage targets: {', '.join(coverage_stale)}")
        raise SystemExit("invalid test matrix: " + "; ".join(details))


def pytest_targets(
    shard, suite: str, covered_groups: tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    """What this invocation runs for `suite`, and what it must leave out.

    A pure function so the partition can be TESTED. Returning the pair from the
    middle of `main` meant a test could only re-derive the split itself and
    would have stayed green with the exclusions deleted."""
    if shard is not None and shard[0] == suite:
        return [f"{suite}/{rel}" for rel in shard[1]], []
    # `covered_groups`: os grupos de shard que ESTA MESMA invocação já cobre.
    # Sem isto, pedir os dois lados de uma vez (`--group control-plane --group
    # control-plane-slow`, ou `--group all`, que é o `make test`) rodava a
    # suite uma vez só, pelo ramo do pai, COM os --ignore — e os três arquivos
    # não rodavam em lugar nenhum. O comentário em SUITE_SHARDS prometia o
    # oposto, e era nele que a Definição de pronto se apoiava: o CI só escapou
    # porque cada grupo é um job com um `--group` só.
    return [suite], [
        f"--ignore={suite}/{rel}"
        for group, (shard_suite, files) in SUITE_SHARDS.items()
        if shard_suite == suite and group not in covered_groups
        for rel in files
    ]


def _validate_shards() -> None:
    """A renamed or deleted sharded file must fail loudly.

    Silence here is the one way this mechanism could lose coverage: the shard
    group would run fewer files, the parent would ignore a path that no longer
    exists, and both would stay green."""
    problems: list[str] = []
    for group, (suite, files) in SUITE_SHARDS.items():
        if suite not in SUITE_GROUPS.get(group, ()):
            problems.append(f"{group} does not contain {suite}")
        for rel in files:
            if not (ROOT / suite / rel).is_file():
                problems.append(f"{group}: {suite}/{rel} does not exist")
    if problems:
        raise SystemExit("invalid test shards: " + "; ".join(problems))


def _report_name(suite: str) -> str:
    return suite.replace("/", "-") + ".xml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--group",
        action="append",
        choices=("all", *SUITE_GROUPS),
        help="group to run; may be repeated (default: all)",
    )
    parser.add_argument(
        "--suite",
        action="append",
        choices=tuple(_registered_suites(SUITE_GROUPS)),
        help="run an individual registered suite; may be repeated",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=ROOT / "test-results",
        help="directory for per-suite JUnit XML files",
    )
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--coverage", action="store_true", help="emit per-suite branch coverage XML")
    parser.add_argument("--list", action="store_true", help="print the resolved suites and exit")
    parser.add_argument(
        "--pytest-arg",
        action="append",
        default=[],
        help="additional argument passed verbatim to every pytest process",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _validate_manifest()
    if args.suite:
        suites = list(dict.fromkeys(args.suite))
        selected_groups = []
    else:
        selected_groups = args.group or ["all"]
        if "all" in selected_groups:
            selected_groups = list(SUITE_GROUPS)
        suites = _registered_suites(selected_groups)
    # Which shard, if any, this invocation is. `--group all` runs both sides, so
    # it must behave exactly as before: parent takes everything, no carve.
    shard = next(
        (SUITE_SHARDS[g] for g in selected_groups if g in SUITE_SHARDS),
        None,
    ) if len(selected_groups) == 1 else None
    # Mais de um grupo pedido: não há carve nenhum a fazer para os grupos de
    # shard que ESTA invocação já inclui — a suite roda inteira, uma vez.
    covered_groups = tuple(
        g for g in selected_groups if g in SUITE_SHARDS
    ) if shard is None else ()

    if args.list:
        for suite in suites:
            print(suite)
        return 0

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    test_run_id = os.environ.setdefault("DSE_TEST_RUN_ID", f"local-{uuid.uuid4().hex[:12]}")
    print(f"DSE test run: {test_run_id}", flush=True)

    failures: list[str] = []
    for suite in suites:
        report = args.reports_dir / _report_name(suite)
        # The shard runs its files; the parent runs the suite minus them. The
        # parent's exclusion is by --ignore so it never has to know what else
        # exists.
        targets, carved = pytest_targets(shard, suite, covered_groups)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *targets,
            *carved,
            f"--junitxml={report}",
            # Per-test ceiling (plan 09 F4): a stuck test becomes a NAMED
            # failure with a traceback at the hang point, instead of killing the
            # whole job via the 45min timeout with no diagnosis. 'thread' works
            # even with asyncio/subprocess. Per-suite override via SUITE_TIMEOUTS.
            f"--timeout={SUITE_TIMEOUTS.get(suite, DEFAULT_TEST_TIMEOUT_S)}",
            "--timeout-method=thread",
        ]
        if args.coverage:
            coverage_report = report.with_suffix(".coverage.xml")
            command.extend(
                [
                    f"--cov={SUITE_COVERAGE_TARGETS[suite]}",
                    "--cov-branch",
                    f"--cov-report=xml:{coverage_report}",
                    "--cov-report=term-missing:skip-covered",
                ]
            )
            floor = SUITE_COVERAGE_FLOORS.get(suite)
            if floor is not None:
                command.append(f"--cov-fail-under={floor}")
        command.extend(args.pytest_arg)
        print(f"\n[{suite}] {' '.join(command)}", flush=True)
        completed = subprocess.run(command, cwd=ROOT, env=os.environ.copy(), check=False)
        if completed.returncode != 0:
            failures.append(suite)
            if args.fail_fast:
                break

    if failures:
        print(f"\nFAILED suites ({len(failures)}): {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(suites)} isolated suite(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
