"""The latch that turns the `dse` label into a gesture the DSE can act on twice.

A card's trigger event has always been `created:{issue id}`, and the issue id
never changes, so the event_id derived from it is a lifetime constant. Taking
the label off and putting it back produced the same id, `admit_work_item` found
it already recorded, and nothing happened — the escalation comment the DSE
itself posts was asking for a gesture the code could not honour. Restarting a
card meant deleting rows by hand.

What makes the gesture safe is that it is an EDGE, not a level: the generation
advances only when the label is observed present after having been observed
absent. A label that merely sits on a card — because nobody removed it, or
because the service account lacks *Edit Issues* — advances nothing, however many
sweeps go by. That distinction is the whole defence: the naive "label present
means go" is the shape that turned one stuck item into ~2,900 rows in an
append-only ledger.

Two rules carry the compatibility of the deploy, and each has a test:

  - **generation 0 is spelled without a suffix.** `created:{id}` is the event_id
    of every card already ingested in the fleet; any suffix here, `:0` included,
    changes all of them at once and the first sweep re-admits the fleet.
  - **the first sighting never advances.** Every card that carries the label on
    the sweep right after the deploy is seeded, not restarted.

Written by the poller's sweep alone. The webhook may seed and disarm but never
advances: Jira redelivers webhooks, and a delivery from before a removal still
carries the label in its payload — letting it advance would mint an attempt
nobody asked for, which is the same loop by another door.
"""
from __future__ import annotations

import logging

from ingest_gateway.db import get_connection

logger = logging.getLogger("adapter_jira.trigger_state")


def observe(
    *,
    tenant_id: str,
    issue_id: str,
    ticket_key: str,
    label_present: bool,
    conn=None,
) -> tuple[int, bool]:
    """Record what the label looks like now; return `(generation, advanced)`.

    Commits before returning, and deliberately before any admission runs: if the
    admission then fails, the next sweep recomputes the SAME generation (the
    latch is already armed) and retries it, landing on the same work item id
    instead of minting a second one. A gesture is never lost and never doubled.

    `SELECT ... FOR UPDATE` because two sweeps of the same tenant must not read
    the same generation and both advance it.
    """
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT generation, label_armed FROM jira_trigger_state "
                "WHERE tenant_id = %s AND issue_id = %s FOR UPDATE",
                (tenant_id, issue_id),
            )
            row = cur.fetchone()

            if row is None:
                # First sighting. Seed at the CURRENT generation — never an
                # advance — so a fleet of cards already carrying the label is
                # simply recorded, and `recorded_work_item_id` recognises each
                # of them from the event_id they already have.
                cur.execute(
                    "INSERT INTO jira_trigger_state "
                    "(tenant_id, issue_id, ticket_key, generation, label_armed) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (tenant_id, issue_id) DO NOTHING",
                    (tenant_id, issue_id, ticket_key, 0, label_present),
                )
                conn.commit()
                return 0, False

            generation, armed = int(row[0]), bool(row[1])

            if label_present and not armed:
                generation += 1
                cur.execute(
                    "UPDATE jira_trigger_state SET generation = %s, label_armed = %s "
                    "WHERE tenant_id = %s AND issue_id = %s",
                    (generation, True, tenant_id, issue_id),
                )
                conn.commit()
                return generation, True

            if not label_present and armed:
                # The label came off — by a human, or by us after admitting. This
                # is the only write that arms the next gesture.
                cur.execute(
                    "UPDATE jira_trigger_state SET generation = %s, label_armed = %s "
                    "WHERE tenant_id = %s AND issue_id = %s",
                    (generation, False, tenant_id, issue_id),
                )
                conn.commit()
                return generation, False

        conn.commit()
        return generation, False
    finally:
        if own_conn:
            conn.close()
