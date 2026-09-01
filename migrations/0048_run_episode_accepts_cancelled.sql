-- rc.130 — `cancelled` is a terminal status of its own.
--
-- 0036 CHECKed four outcomes and said, in its comment, that listing
-- 'cancelled' would "put a value in the schema that nothing can ever write":
-- `_finish_cancelled` resolved to `failed`. That changed. A human decision is
-- not a failure; 33 rows in production already carried `cancelled` (written by
-- operators, by hand, into a column with no CHECK) and the stranded sweep,
-- which did not know the value as terminal, re-escalated every one of them.
--
-- The journal is written from `_set_status` on every terminal transition, so
-- without this the very first cancelled run would fail its write at 3am — the
-- exact case `test_terminal_set_matches_what_the_schema_accepts` exists to
-- catch, and did.
--
-- Idempotent (same shape as 0033): drop-if-exists, then add. The constraint
-- name is the default Postgres derives for an inline CHECK on `outcome`.
ALTER TABLE run_episode DROP CONSTRAINT IF EXISTS run_episode_outcome_check;
ALTER TABLE run_episode ADD CONSTRAINT run_episode_outcome_check
    CHECK (outcome IN ('done', 'failed', 'escalated', 'blocked', 'cancelled'));
