"""The diário de bordo — one durable entry per run that reached a terminal state.

Three properties are pinned here, and the first one is not ceremony: the payload
this workflow sends is built by reading attributes off `WorkItemLifecycleInput`,
and two of the first draft's field names (`title`, `task_class`) did not exist on
that dataclass. Nothing would have caught it before production, because the write
only happens on a TERMINAL transition — the one path an in-memory unit test of a
mid-flight phase never reaches. An AttributeError there would have hit every work
item, at the very end of its life, after all the expensive work was done.

  1. every field the journal payload reads exists on the input dataclass;
  2. the digest is deterministic and bounded — no model call, no unbounded text
     reaching a 16.000-char Planner budget;
  3. failed runs are journalled too, and say what stopped them. A journal that
     only remembers successes is the one that misleads.
"""
from __future__ import annotations

import dataclasses

from dse_orchestrator import local_activities as la
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import _TERMINAL_STATUSES

# Mirrors the payload built by `WorkItemLifecycleWorkflow._record_run_episode`.
_PAYLOAD_READS_FROM_INPUT = {
    "tenant_id", "work_item_id", "repo", "base_branch", "base_sha",
    "risk_class", "data_class", "task_content", "plan_json",
    "terminal_detail", "fix_context", "plan_rounds", "pr_number",
}


def test_every_field_the_journal_reads_exists_on_the_input():
    declared = {f.name for f in dataclasses.fields(WorkItemLifecycleInput)}
    missing = _PAYLOAD_READS_FROM_INPUT - declared
    assert not missing, f"the journal payload reads fields that do not exist: {sorted(missing)}"


def test_terminal_set_matches_what_the_schema_accepts():
    """migrations/0036 CHECKed four; 0048 admits `cancelled` (rc.130). A
    terminal state added to the enum but not to `_TERMINAL_STATUSES` is a run
    that is silently never journalled; one added here but not to the CHECK is
    a write that fails at 3am — which is exactly what this pin caught when
    `cancelled` entered the enum before the migration existed."""
    assert {s.value for s in _TERMINAL_STATUSES} == {
        "done", "failed", "escalated", "blocked", "cancelled",
    }


def test_digest_is_deterministic():
    payload = {
        "outcome": "done", "work_item_id": "WI-1", "title": "Add the DELETE endpoint",
        "expected_files": ["src/api.py"], "risk_class": "low", "pr_number": 42,
    }
    assert la._render_run_digest(payload) == la._render_run_digest(payload)


def test_failed_run_records_what_stopped_it():
    digest = la._render_run_digest({
        "outcome": "failed",
        "work_item_id": "WI-2",
        "title": "Introduce JPA persistence",
        "terminal_detail": "activity_retries_exhausted:run_tester_turn:timeout",
        "fix_context": ["the suite needs a live DB; use the Zonky fixture"],
    })
    assert digest.startswith("[failed]")
    assert "stopped at: activity_retries_exhausted" in digest
    assert "Zonky" in digest


def test_successful_run_does_not_claim_a_failure():
    digest = la._render_run_digest({
        "outcome": "done", "work_item_id": "WI-3", "title": "Fix the filename bug",
        "terminal_detail": "merged", "pr_number": 7,
    })
    assert "stopped at" not in digest
    assert "merged as #7" in digest


def test_digest_is_bounded_however_large_the_run_was():
    """The entry is destined for a prompt. Bounding it at the far end, where
    nothing knows what it cut, is the mistake this avoids."""
    digest = la._render_run_digest({
        "outcome": "escalated",
        "work_item_id": "WI-4",
        "title": "x" * 5_000,
        "expected_files": [f"src/module_{i}/file.py" for i in range(500)],
        "terminal_detail": "y" * 5_000,
        "fix_context": ["z" * 5_000, "w" * 5_000, "v" * 5_000],
    })
    assert len(digest) <= la._RUN_DIGEST_MAX_CHARS


def test_digest_caps_the_lists_it_renders():
    """Bounded by construction, not only by the final slice — so the cap never
    eats the outcome line, which is the part a reader needs first."""
    digest = la._render_run_digest({
        "outcome": "failed", "work_item_id": "WI-5", "title": "big",
        "expected_files": [f"f{i}.py" for i in range(50)],
        "fix_context": [f"fix {i}" for i in range(50)],
    })
    assert digest.startswith("[failed] big")
    assert digest.count("had to fix:") <= 2
    assert digest.split("planned: ")[1].split("\n")[0].count(",") <= 5


def test_activity_is_registered_under_its_contract_name():
    assert la.record_run_episode in la.LOCAL_ACTIVITIES
    assert (
        la.record_run_episode.__temporal_activity_definition.name
        == la.LOCAL_ACTIVITY_RECORD_RUN_EPISODE
    )


def test_write_degrades_loudly_without_a_database(monkeypatch):
    """Best-effort, but never a silent nothing: the caller audits the skip. The
    three silent-empties this repository already shipped are the reason."""
    import asyncio

    def _boom():
        raise RuntimeError("no postgres")

    monkeypatch.setattr(la, "_get_connection", _boom, raising=True)
    result = asyncio.run(
        la.record_run_episode({"tenant_id": "t", "work_item_id": "WI-6", "outcome": "done"})
    )
    assert result == {"persisted": False, "reason": "no_database"}
