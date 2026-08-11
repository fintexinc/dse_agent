from __future__ import annotations

import collections
import pathlib
import re

import pytest

from scripts import test_matrix, with_test_database

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"

# Historical collision PREDATING this gate (0020_wsc4.sql + 0020_wse4.sql): both
# have already been applied (schema_migrations records by NAME; lexicographic
# ordering runs them stably) — renumbering would break every existing
# environment. Frozen here; NO new collision is accepted.
_GRANDFATHERED_PREFIXES = {"0020"}


def test_migration_numeric_prefixes_are_unique() -> None:
    prefixes = [
        re.match(r"(\d+)_", f.name).group(1)
        for f in sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if re.match(r"(\d+)_", f.name)
    ]
    duplicated = {p for p, n in collections.Counter(prefixes).items() if n > 1}
    new_collisions = duplicated - _GRANDFATHERED_PREFIXES
    assert not new_collisions, (
        f"duplicated migration prefix: {sorted(new_collisions)} — reserve the next "
        "free number (CONVENTIONS.md, 'Migrations') instead of colliding"
    )


def test_test_matrix_registers_every_suite_exactly_once() -> None:
    test_matrix._validate_manifest()
    registered = test_matrix._registered_suites(test_matrix.SUITE_GROUPS)
    assert len(registered) == len(set(registered))
    assert set(registered) == test_matrix._discovered_suites()
    assert set(registered) == set(test_matrix.SUITE_COVERAGE_TARGETS)


def test_suite_selection_takes_precedence_over_group(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["test_matrix.py", "--group", "all", "--suite", "packages/contracts", "--list"],
    )
    args = test_matrix.parse_args()
    assert args.suite == ["packages/contracts"]


def test_database_url_replaces_only_database_path() -> None:
    result = with_test_database._database_url(
        "postgresql://user:password@localhost:5432/original?sslmode=disable",
        "dse_test_abcdefgh",
    )
    assert result == (
        "postgresql://user:password@localhost:5432/dse_test_abcdefgh?sslmode=disable"
    )


@pytest.mark.parametrize("target", ["dse", "public", "dse_test_bad-name", ""])
def test_cleanup_guard_rejects_unsafe_target(target: str) -> None:
    with pytest.raises(SystemExit, match="unsafe"):
        with_test_database._assert_safe_target(
            "postgresql://dse:dev@localhost:5432/dse", target
        )


def test_cleanup_guard_rejects_remote_database_by_default(monkeypatch) -> None:
    monkeypatch.delenv("DSE_ALLOW_REMOTE_TEST_DB", raising=False)
    with pytest.raises(SystemExit, match="non-local"):
        with_test_database._assert_safe_target(
            "postgresql://dse:dev@db.example.test:5432/dse",
            "dse_test_abcdefgh",
        )


# ---------------------------------------------------------------------------
# Sharding — the matrix's wall clock was ONE job, and that job was ONE file.
#
# `control-plane` measured 375s while every other group finished under 290s, so
# the whole CI run waited on it. Inside it, `services/orchestrator` is 270s of
# 275s, and inside THAT, `test_plan_approval_timeout.py` alone is 119s: 16 tests
# that each stand up a real Temporal time-skipping server and drive a full
# work-item lifecycle. Nothing coarser than a file could split it.
#
# Splitting a suite is the one optimisation here that can silently DELETE
# coverage, so these tests pin the two properties that make it safe: every test
# still runs exactly once, and a file that moves fails the build loudly.
# ---------------------------------------------------------------------------
def test_every_shard_file_belongs_to_the_suite_its_group_declares():
    from scripts.test_matrix import SUITE_GROUPS, SUITE_SHARDS

    for group, (suite, files) in SUITE_SHARDS.items():
        assert suite in SUITE_GROUPS[group], f"{group} does not run {suite}"
        assert files, f"{group} declares a shard with no files"


def test_a_sharded_file_that_moves_fails_the_manifest(tmp_path, monkeypatch):
    """The silent-loss mode: rename a sharded file and the shard group runs
    fewer tests while the parent ignores a path that no longer exists — both
    green, both wrong. `_validate_shards` is what makes that impossible."""
    from scripts import test_matrix

    monkeypatch.setattr(
        test_matrix,
        "SUITE_SHARDS",
        {"control-plane-slow": ("services/orchestrator", ("tests/nao_existe.py",))},
    )
    with pytest.raises(SystemExit) as exc:
        test_matrix._validate_shards()
    assert "nao_existe.py" in str(exc.value)


def test_the_shard_and_its_complement_partition_the_suite():
    """Together they must run the suite exactly once — no test lost, none twice.

    The complement is derived with `--ignore`, never enumerated, so a test file
    added tomorrow lands in the parent group by construction. This asserts the
    partition through the real command builder rather than by re-deriving it."""
    import subprocess
    import sys

    from scripts.test_matrix import ROOT, SUITE_SHARDS

    suite, files = SUITE_SHARDS["control-plane-slow"]

    def collected(*argv: str) -> int:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", *argv],
            cwd=ROOT, capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            if line.strip().endswith("tests collected") or " tests collected " in line:
                return int(line.split()[0])
        raise AssertionError(f"could not read a count from: {out[-400:]}")

    # Both sides come from the REAL command builder, so deleting the
    # exclusions makes this test fail instead of quietly double-counting.
    from scripts.test_matrix import SUITE_SHARDS as _S, pytest_targets

    shard_spec = _S["control-plane-slow"]
    shard_targets, shard_carved = pytest_targets(shard_spec, suite)
    rest_targets, rest_carved = pytest_targets(None, suite)

    whole = collected(suite)
    shard = collected(*shard_targets, *shard_carved)
    rest = collected(*rest_targets, *rest_carved)

    assert shard + rest == whole, (
        f"the split changes what runs: {shard} + {rest} != {whole}"
    )
    assert shard > 0 and rest > 0, "one side of the split is empty"


def test_selecting_both_groups_at_once_actually_runs_every_file():
    """O comando que o CLAUDE.md manda rodar não pode pular arquivo.

    Achado em 2026-08-10, e o vão é meu: escrevi no CLAUDE.md "ao tocar no
    orchestrator, rode os DOIS grupos" — e `--group control-plane --group
    control-plane-slow` numa invocação só roda a suite UMA vez, pelo ramo do
    PAI, que carrega os `--ignore` dos três arquivos do shard. `shard` só é
    calculado quando `len(selected_groups) == 1`.

    O mesmo vale para `--group all`, isto é, para `make test`: a "suite
    completa" da Definição de pronto nunca executou esses três arquivos. O CI
    escapa por acidente de topologia — cada grupo é um job com um `--group` só.

    O comentário em SUITE_SHARDS afirma o contrário ("os dois grupos juntos
    rodam cada arquivo exatamente uma vez"), e era nele que eu confiava ao
    declarar verde.
    """
    from scripts.test_matrix import SUITE_GROUPS, SUITE_SHARDS, pytest_targets

    suite, sharded_files = SUITE_SHARDS["control-plane-slow"]

    for selected in (
        ["control-plane", "control-plane-slow"],
        list(SUITE_GROUPS),  # `--group all` == `make test`
    ):
        covered = tuple(g for g in selected if g in SUITE_SHARDS)
        _, carved = pytest_targets(None, suite, covered)
        for rel in sharded_files:
            assert f"--ignore={suite}/{rel}" not in carved, (
                f"com {selected} selecionado, {rel} é ignorado — o verde não "
                "cobre esse arquivo, que foi exatamente como o CI de afb9616 "
                "quebrou num teste que meu run local nem executou"
            )


def test_a_group_that_does_not_include_the_shard_still_carves_it_out():
    """PIN de polaridade: `--group control-plane` sozinho continua ignorando os
    três arquivos — senão os dois jobs do CI passam a rodar tudo em duplicata."""
    from scripts.test_matrix import SUITE_SHARDS, pytest_targets

    suite, sharded_files = SUITE_SHARDS["control-plane-slow"]
    _, carved = pytest_targets(None, suite, ("control-plane",))
    for rel in sharded_files:
        assert f"--ignore={suite}/{rel}" in carved
