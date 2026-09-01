"""WSE-E4-T9a — minimal consumption of PR status checks. `FakeGitHubClient`
supplies the check-runs (no real GitHub App in this session); persistence to
`wse_ci_status` is REAL Postgres."""
from __future__ import annotations

from dse_validation import db
from dse_validation.github.ci_status import (
    CI_NO_CI,
    aggregate_check_runs,
    aggregate_ci,
    aggregate_combined_status,
    consume_ci_status_core,
)
from dse_validation.github.client import FakeGitHubClient


def test_aggregate_no_check_runs_is_no_ci_not_pending():
    """The bug this vocabulary exists for: an empty array used to read as
    "still running", so a repo that will never report anything waited forever
    and no work item ever finished."""
    assert aggregate_check_runs([]) == CI_NO_CI


def test_aggregate_all_success_is_green():
    runs = [
        {"name": "lint", "status": "completed", "conclusion": "success"},
        {"name": "tests", "status": "completed", "conclusion": "success"},
    ]
    assert aggregate_check_runs(runs) == "green"


def test_aggregate_any_failure_is_red():
    runs = [
        {"name": "lint", "status": "completed", "conclusion": "success"},
        {"name": "tests", "status": "completed", "conclusion": "failure"},
    ]
    assert aggregate_check_runs(runs) == "red"


def test_aggregate_still_running_is_pending_even_if_others_passed():
    runs = [
        {"name": "lint", "status": "completed", "conclusion": "success"},
        {"name": "tests", "status": "in_progress", "conclusion": None},
    ]
    assert aggregate_check_runs(runs) == "pending"


def test_consume_ci_status_core_persists_to_real_postgres(work_item_id, tenant_id):
    github = FakeGitHubClient()
    github.set_check_runs(
        "acme/repo",
        "abc123",
        [{"name": "build", "status": "completed", "conclusion": "success"}],
    )
    result = consume_ci_status_core(
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        pr_number=55,
        ref="abc123",
    )
    assert result.status == "green"
    assert result.pr_number == 55

    row = db.get_ci_status(work_item_id)
    assert row is not None
    assert row["status"] == "green"
    assert row["pr_number"] == 55


def test_consume_ci_status_core_red_on_failed_check(work_item_id, tenant_id):
    github = FakeGitHubClient()
    github.set_check_runs(
        "acme/repo",
        "def456",
        [{"name": "tests", "status": "completed", "conclusion": "failure"}],
    )
    result = consume_ci_status_core(
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        pr_number=56,
        ref="def456",
    )
    assert result.status == "red"


# --------------------------------------------------------------------------
# The legacy commit-status source, and how the two combine.
# --------------------------------------------------------------------------


def test_combined_status_keys_off_the_array_never_off_state():
    """GitHub answers `state: "pending"` for a commit nothing has reported on.
    Trusting `state` would recreate the original bug on the legacy side."""
    assert aggregate_combined_status({"state": "pending", "statuses": [], "total_count": 0}) == CI_NO_CI
    assert aggregate_combined_status(None) == CI_NO_CI


def test_combined_status_maps_real_states():
    ok = {"state": "success", "statuses": [{"context": "ci/jenkins"}]}
    bad = {"state": "failure", "statuses": [{"context": "ci/jenkins"}]}
    running = {"state": "pending", "statuses": [{"context": "ci/jenkins"}]}
    assert aggregate_combined_status(ok) == "green"
    assert aggregate_combined_status(bad) == "red"
    assert aggregate_combined_status(running) == "pending"


def test_no_ci_requires_both_sources_to_be_silent():
    """A repo whose CI reports only through legacy statuses must NOT be read as
    having no CI — that would send an unverified change to a human as if
    nothing were expected."""
    legacy_only = {"state": "success", "statuses": [{"context": "ci/jenkins"}]}
    assert aggregate_ci([], legacy_only) == "green"
    assert aggregate_ci([], {"state": "pending", "statuses": [], "total_count": 0}) == CI_NO_CI
    assert aggregate_ci([], None) == CI_NO_CI


def test_red_beats_pending_and_pending_beats_green_across_sources():
    green_runs = [{"name": "lint", "status": "completed", "conclusion": "success"}]
    running_runs = [{"name": "lint", "status": "in_progress"}]
    red_legacy = {"state": "failure", "statuses": [{"context": "ci/jenkins"}]}
    running_legacy = {"state": "pending", "statuses": [{"context": "ci/jenkins"}]}
    assert aggregate_ci(green_runs, red_legacy) == "red"
    assert aggregate_ci(running_runs, red_legacy) == "red"
    assert aggregate_ci(green_runs, running_legacy) == "pending"


def test_consume_records_no_ci_when_the_repo_reports_nothing(work_item_id, tenant_id):
    """End to end through the persistence layer: with neither source reporting,
    the status the workflow reads — and the row it persists — must be `no_ci`.
    This is the exact shape of andre2654/fintex-wallet, where 8 PRs sat in
    `ci_pending` forever."""
    github = FakeGitHubClient()
    result = consume_ci_status_core(
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        pr_number=42,
        ref="sha-none",
    )
    assert result.status == CI_NO_CI

    row = db.get_ci_status(work_item_id)
    assert row is not None
    assert row["status"] == CI_NO_CI
    # the detail records that BOTH sources were consulted and both were silent
    assert row["detail"]["check_runs"] == []
    assert row["detail"]["combined_status"] == {"state": "pending", "contexts": []}


def test_legacy_statuses_alone_are_enough_to_avoid_no_ci(work_item_id, tenant_id):
    """A repo reporting only through commit statuses must be read as having CI."""
    github = FakeGitHubClient()
    github.set_combined_status(
        "acme/repo", "sha-legacy",
        {"state": "success", "statuses": [{"context": "ci/jenkins"}], "total_count": 1},
    )
    result = consume_ci_status_core(
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        pr_number=43,
        ref="sha-legacy",
    )
    assert result.status == "green"


# --------------------------------------------------------------------------
# What a WAIT costs the ledger. Measured on the cluster: `ci_status_consumed`
# was 6494 rows — one per poll — for the same handful of work items, because
# every look wrote a row whether or not anything had changed.
# --------------------------------------------------------------------------


def _consumed(work_item_id: str) -> list[dict]:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT details FROM audit_log "
                "WHERE work_item_id = %s AND action = 'ci_status_consumed' ORDER BY id",
                (work_item_id,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _poll(github: FakeGitHubClient, work_item_id: str, tenant_id: str, ref: str = "sha-wait") -> str:
    return consume_ci_status_core(
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        pr_number=77,
        ref=ref,
    ).status


def test_a_45_poll_wait_writes_one_audit_row_not_forty_five(work_item_id, tenant_id):
    """The cost of waiting has to be the number of things that CHANGED, not the
    number of times we looked. The first observation is a fact and is written;
    the 44 that restate it are not."""
    github = FakeGitHubClient()
    github.set_check_runs("acme/repo", "sha-wait", [{"name": "tests", "status": "in_progress"}])
    for _ in range(45):
        assert _poll(github, work_item_id, tenant_id) == "pending"

    rows = _consumed(work_item_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    # nothing preceded it — the first observation of a work item is a transition
    # out of "never looked", which is exactly what an auditor needs to see.
    assert rows[0]["from_status"] is None

    # the CURRENT state is still refreshed on every poll — what stopped being
    # written is the ledger row, not the row the rest of the system reads.
    assert db.get_ci_status(work_item_id)["status"] == "pending"


def test_the_transition_row_names_the_status_it_replaced(work_item_id, tenant_id):
    """The row that replaces 44 identical ones must let the audit query rebuild
    the sequence on its own, so it carries where it came from."""
    github = FakeGitHubClient()
    github.set_check_runs("acme/repo", "sha-wait", [{"name": "tests", "status": "in_progress"}])
    for _ in range(45):
        _poll(github, work_item_id, tenant_id)
    github.set_check_runs(
        "acme/repo", "sha-wait",
        [{"name": "tests", "status": "completed", "conclusion": "success"}],
    )
    assert _poll(github, work_item_id, tenant_id) == "green"

    rows = _consumed(work_item_id)
    assert len(rows) == 2
    assert rows[1]["status"] == "green"
    assert rows[1]["from_status"] == "pending"
    # and the evidence for the transition travels with it
    assert rows[1]["check_runs"] == [
        {"name": "tests", "status": "completed", "conclusion": "success"}
    ]


def test_a_retried_activity_does_not_write_the_transition_twice(work_item_id, tenant_id):
    """Temporal retries this Activity on any failure, including one raised after
    the status write already committed. The second attempt observes the same
    GitHub state, finds the row already holding it, and writes nothing — the
    transition is recorded once per transition, not once per attempt."""
    github = FakeGitHubClient()
    github.set_check_runs("acme/repo", "sha-wait", [{"name": "tests", "status": "in_progress"}])
    _poll(github, work_item_id, tenant_id)
    github.set_check_runs(
        "acme/repo", "sha-wait",
        [{"name": "tests", "status": "completed", "conclusion": "success"}],
    )
    _poll(github, work_item_id, tenant_id)
    _poll(github, work_item_id, tenant_id)  # the retry: identical input, identical GitHub

    rows = _consumed(work_item_id)
    assert [r["status"] for r in rows] == ["pending", "green"]


def test_every_real_transition_survives(work_item_id, tenant_id):
    """What leaves is `pending -> pending`. A PR that GitHub has not registered
    any check for yet, then runs, then fails, then passes is four facts and must
    still read as four rows."""
    github = FakeGitHubClient()
    assert _poll(github, work_item_id, tenant_id, ref="sha-a") == CI_NO_CI
    github.set_check_runs("acme/repo", "sha-a", [{"name": "tests", "status": "in_progress"}])
    assert _poll(github, work_item_id, tenant_id, ref="sha-a") == "pending"
    github.set_check_runs(
        "acme/repo", "sha-a", [{"name": "tests", "status": "completed", "conclusion": "failure"}]
    )
    assert _poll(github, work_item_id, tenant_id, ref="sha-a") == "red"
    github.set_check_runs(
        "acme/repo", "sha-b", [{"name": "tests", "status": "completed", "conclusion": "success"}]
    )
    assert _poll(github, work_item_id, tenant_id, ref="sha-b") == "green"

    rows = _consumed(work_item_id)
    assert [r["status"] for r in rows] == [CI_NO_CI, "pending", "red", "green"]
    assert [r["from_status"] for r in rows] == [None, CI_NO_CI, "pending", "red"]


def test_save_ci_status_returns_the_status_it_replaced(work_item_id):
    """The edge detector is the write itself. This is what makes a retry of an
    already-applied write indistinguishable from "nothing changed" — the property
    the audit rule above rests on."""
    assert db.save_ci_status(work_item_id, 7, "pending", {}) is None
    assert db.save_ci_status(work_item_id, 7, "pending", {}) == "pending"
    assert db.save_ci_status(work_item_id, 7, "green", {}) == "pending"
    assert db.save_ci_status(work_item_id, 7, "green", {}) == "green"
    assert db.get_ci_status(work_item_id)["status"] == "green"


# --------------------------------------------------------------------------
# A evidência viaja no contrato (rc.130).
#
# Medido no wi_f1f27266 (2026-08-31): o laço de CI vermelho gastou 8 rodadas
# (~US$ 19) com `files_changed: []` porque a instrução ao modelo era o literal
# "ci red: fix the pipeline" — o `CiStatusResult` carregava só a string "red".
# Os nomes dos checks e as conclusões existiam em `wse_ci_status` e no audit,
# a um JOIN que nenhum turno faz. O card de escalação dizia
# `ci_red_after_retry_cap_exhausted` e nada mais.
# --------------------------------------------------------------------------

def test_a_red_result_names_the_failing_checks_with_their_urls(work_item_id, tenant_id):
    github = FakeGitHubClient()
    github.set_check_runs(
        "acme/repo", "def456",
        [
            {"name": "lint", "status": "completed", "conclusion": "success",
             "html_url": "https://github.com/acme/repo/runs/1"},
            {"name": "unit (API)", "status": "completed", "conclusion": "failure",
             "html_url": "https://github.com/acme/repo/runs/2"},
            {"name": "leak gate", "status": "completed", "conclusion": "timed_out",
             "details_url": "https://ci.example/leak"},
        ],
    )
    result = consume_ci_status_core(
        github_client=github, work_item_id=work_item_id, tenant_id=tenant_id,
        repo="acme/repo", pr_number=56, ref="def456",
    )
    assert result.status == "red"
    nomes = [c.name for c in result.failing_checks]
    assert nomes == ["unit (API)", "leak gate"], "só os que reprovaram, na ordem do CI"
    assert result.failing_checks[0].conclusion == "failure"
    assert result.failing_checks[0].url == "https://github.com/acme/repo/runs/2"
    assert result.failing_checks[1].url == "https://ci.example/leak", (
        "sem html_url o link é o details_url — nunca None quando o CI deu um"
    )


def test_a_green_result_carries_no_failing_checks(work_item_id, tenant_id):
    github = FakeGitHubClient()
    github.set_check_runs(
        "acme/repo", "abc123",
        [{"name": "lint", "status": "completed", "conclusion": "success"}],
    )
    result = consume_ci_status_core(
        github_client=github, work_item_id=work_item_id, tenant_id=tenant_id,
        repo="acme/repo", pr_number=57, ref="abc123",
    )
    assert result.status == "green" and result.failing_checks == []
