# DSE — phase 1

Python monorepo: `packages/` (libraries), `services/` (13 components), Temporal + Postgres + Vault.

## Conventions

Branch names, commit messages, PR titles and bodies, and this file are in English.
The history before 2026-09-10 is in Portuguese; it is not being rewritten.

## Commands

Nothing activates the venv for you, and `ruff`/`mypy`/`pytest` only exist inside it.
**Always prefix, or export the PATH once per session:**

```
export PATH="$PWD/.venv/bin:$PATH"
```

- lint (compile + ruff + mypy ratchet): `make lint` — 1.1s, this is CI's `quality` job
- suites without docker: `python scripts/test_matrix.py --group contracts --group tooling --group packages` — 5.2s
- a single suite: `python scripts/test_matrix.py --suite services/orchestrator --reports-dir test-results`
- the full suite (needs `make up`): `make test`
- list the suites: `python scripts/test_matrix.py --list`

Every suite runs in its own process and writes `test-results/<suite-with-hyphens>.xml`.
Never call `pytest` directly at the root: each component has its own `tests/conftest.py`
and a single process resolves the first one for all of them.

## Invariants

- mypy gates only `packages/contracts`, `dse_audit`, `dse_identity`. `services/orchestrator`
  carries a baseline of 426 errors; do not treat an error inherited from it as yours.
- A DSE git command never executes code from the client's repository. Every new call site
  has to pin `core.hooksPath` to an empty directory — this has already been fixed in three
  separate call sites (#46, hygiene, #52) because the rule lived in each one of them.
- Sibling work item ids are hashed (`sha256(event_id:repo)`), never suffixed:
  `pod_name_for` truncates at 63 and the id is already 67 chars.
- `awaiting_human_review` is success, not a hang. The DSE never approves its own work.
- **There is no test ownership** (operator decision, 2026-08-10). The DSE changes any
  test — including the specs the loop itself wrote — and the supervision is the PR diff.
  Removed along with it: the post-turn revert, the authorship oracle (`-dse`, commit
  subject), the rename guard, the `spec_conflict` park and the reauthor. ONE authorship
  survives: gate 5, where the Tester fixes the spec it just wrote and that does not even
  load. A loop that does not converge ends only one way: `escalated`, through the usual
  brakes (attempt cap, `coder_not_converging`, double no-op, empty diff, spend cap).
- `repo_bindings` is not "the tenant's repositories" — it has one row per binding.
  The candidate set is `repo_bindings UNION repo_profiles`.

## Traps

- `tests (control-plane)` is sensitive to contention: it uses a time-skipping Temporal
  whose clock only advances in idle windows. It fails on a different test every time.
  A rerun with an empty queue passes. This does **not** license treating another failure
  as environmental — reproduce it on the previous commit.
- A work item started by `temporal workflow start` creates no row in `work_items`.
  A wait keyed on `work_items.status` hangs forever; wait on the WORKFLOW's status.
- `_tail(stdout or stderr)` discards stderr whenever stdout is non-empty — that is how
  every L1 gate published the wrong evidence for two days (#60). Watch out for that `or`.
- `--suite services/orchestrator` **does not run the whole suite**: the `SUITE_SHARDS`
  files (`test_plan_approval_timeout.py`, `test_phase4_merge_base_and_learning.py`,
  `test_iteration_caps_debounce.py`) only come out under `--group control-plane-slow`.
  Green on `--suite` with CI red on those three has already happened. When you touch the
  orchestrator, run BOTH groups: `--group control-plane --group control-plane-slow`.

## Definition of done

`make lint` green and the suite for the area you touched green, with
`test-results/<suite>.xml` on disk. Bug fix: the regression test is written, runs **red**,
and is **committed red** before the fix — from then on any change to it shows up in the diff.
