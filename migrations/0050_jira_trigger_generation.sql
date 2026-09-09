-- Fintex DSE — the latch that makes "take the label off and put it back" a
-- gesture the DSE can act on more than once.
--
-- The problem it solves. A Jira card's trigger event has always been
-- `created:{issue id}`, and the issue id never changes, so the event_id derived
-- from it is a lifetime constant of the card. Re-applying the `dse` label
-- produces the same id, `admit_work_item` finds it already there, and nothing
-- happens — 51 such sweeps on 2026-09-09 alone. The escalation comment the DSE
-- itself posts tells the human to re-apply the label, which the code knew did
-- nothing. Restarting a card meant deleting rows by hand, which is exactly what
-- rc.130 set out to abolish.
--
-- What this table holds. One row per card, carrying two facts: how many times
-- the gesture has been made (`generation`, which enters the event id from 1
-- onwards — generation 0 keeps the historic `created:{id}` form, so every card
-- already ingested resolves to exactly the id it resolves to today), and
-- whether the label was ON the card the last time we looked (`label_armed`).
--
-- The gesture is an EDGE, not a level: the generation only advances when the
-- label is seen present after having been seen absent. A label that simply sits
-- on a card advances nothing, however many sweeps go by — that is what keeps a
-- sticky label (say, one the service account lacks permission to remove) from
-- restarting the same work every minute. The first sighting of any card never
-- advances either, which is what makes the deploy a no-op for the cards that
-- already carry the label.
--
-- Keyed by `issue_id` and not by `ticket_key`: moving a card between projects
-- changes its key (and with it the thread key, and the event id) but never its
-- id, and a key-keyed latch would reset on an event no human performed.
-- `ticket_key` rides along so an operator reading this table sees BFA-1132
-- rather than 10123.

CREATE TABLE IF NOT EXISTS jira_trigger_state (
    tenant_id   TEXT NOT NULL,
    issue_id    TEXT NOT NULL,
    ticket_key  TEXT NOT NULL,
    generation  INT NOT NULL DEFAULT 0,
    label_armed BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, issue_id)
);

DROP TRIGGER IF EXISTS set_updated_at_jira_trigger_state ON jira_trigger_state;
CREATE TRIGGER set_updated_at_jira_trigger_state
    BEFORE UPDATE ON jira_trigger_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- No DELETE: the latch is the memory of how many attempts a card has had, and
-- losing it silently re-arms a gesture the human did not make.
GRANT SELECT, INSERT, UPDATE ON jira_trigger_state TO dse_app;
