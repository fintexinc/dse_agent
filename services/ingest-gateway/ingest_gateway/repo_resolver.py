"""Deterministic repository resolution cascade (Report 07 C2 / Phase B).

Slack/Jira tasks are not born in a repo (unlike GitHub, where the issue is
ALREADY in the repo). This cascade discovers the repo from the MOST
specific/explicit to the least and — crucially — refuses to resolve when there
is no trustworthy answer, so the workflow ASKS instead of guessing. The wrong
repo never happens silently (fail-safe; P1: config + regex only, no LLM).

Order:
  1. explicit override (`repo=owner/name` in the text / dedicated field)
  2. specific binding: channel (Slack) / component (Jira) / project (Jira)
  3. broad binding: workspace (Slack team / Jira site)
  4. the tenant's single repository, when it has exactly one
  5. nothing -> clarification

A binding may list SEVERAL repositories. One row resolves outright; two or
more resolve nothing and instead hand back that set as the origin's SCOPE —
the router then picks inside it and can never reach another channel's
repositories. That is why this function returns three values, not two.

Rung 4 used to be described here as "the tenant's single-repo default
(binding_value '*')". No code ever read a `'*'` row: the lookup above is an
equality match, so a literal `*` only matches a channel actually named `*`.
Production carried such a row for months, doing nothing, while every Slack task
silently took the LLM router instead — 47 of them. The line is corrected rather
than deleted because a convention that never existed is worth naming once.
"""
from __future__ import annotations

import re
from typing import Any

from dse_contracts.repos import TENANT_REPOS_SQL

# explicit override in free text (same format as C4).
_RE_REPO = re.compile(r"\brepo(?:sitory)?\s*[:=]\s*([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)", re.I)
_RE_BRANCH = re.compile(r"\b(?:base[_-]?)?branch\s*[:=]\s*([A-Za-z0-9._/-]+)", re.I)

# precedence: lower index = more specific.
_TYPE_PRECEDENCE = ("channel", "component", "project", "workspace")


def parse_explicit_repo(text: str) -> tuple[str | None, str | None]:
    """(repo, base_branch) explicitly given in the text, if any. Rung 1 of the cascade."""
    if not text:
        return None, None
    m = _RE_REPO.search(text)
    b = _RE_BRANCH.search(text)
    return (m.group(1) if m else None), (b.group(1) if b else None)


def resolve_repo(
    conn,
    *,
    tenant_id: str,
    platform: str,
    signals: dict[str, Any],
) -> tuple[str | None, str | None, list[str]]:
    """Runs the cascade. `signals` carries whatever the adapter managed to
    extract: {text, channel, component, project, workspace}.

    Returns `(repo, base_branch, candidates)`:

      - `(repo, branch, [])` — resolved, deterministically, no model involved;
      - `(None, None, [repoA, repoB, …])` — NOT resolved, but the origin
        delimited the universe: those are the only repositories this channel /
        project / component can reach, and the router chooses inside them;
      - `(None, None, [])` — nothing to go on; the workflow asks.

    The third element is what lets a binding say "these repositories" instead
    of only "this repository". A team with a frontend and a backend in one
    Slack channel used to have no way to express that: bind one and type
    `repo:` for the other every time (a forgotten prefix silently targets the
    wrong repository), or bind nothing and let the router see EVERY repository
    the tenant has, from every channel.

    GitHub does not use this (the repo comes from the webhook); kept generic
    for symmetry.
    """
    # Rung 1 — an explicit override beats everything.
    repo, branch = parse_explicit_repo(signals.get("text") or "")
    if repo:
        return repo, (branch or "main"), []

    # Rungs 2-3 — bindings, from the most specific to the broadest.
    candidates: list[tuple[str, str]] = []  # (binding_type, binding_value)
    for btype in _TYPE_PRECEDENCE:
        val = signals.get(btype)
        if val:
            candidates.append((btype, str(val)))
    if candidates:
        with conn.cursor() as cur:
            for btype, val in candidates:  # already in precedence order
                cur.execute(
                    "SELECT repo, base_branch FROM repo_bindings "
                    "WHERE tenant_id = %s AND platform = %s AND binding_type = %s "
                    "AND binding_value = %s ORDER BY repo",
                    (tenant_id, platform, btype, val),
                )
                rows = cur.fetchall()
                if len(rows) == 1:
                    return rows[0][0], rows[0][1], []
                if rows:
                    # The FIRST binding type that matches decides, even when it
                    # decides "I cannot pick one". Falling through to a broader
                    # type here would let a `project` set override a `component`
                    # set — inverting the precedence this cascade exists to
                    # enforce, and doing it only in the ambiguous case, which is
                    # exactly where a wrong repository is least likely to be
                    # noticed.
                    return None, None, [r[0] for r in rows]

    # Rung 4 — if the tenant has EXACTLY one repository, use it; there is
    # nothing to be ambiguous about.
    #
    # "Has" is the whole question, and this asked it of `repo_bindings` alone —
    # one row per BINDING, not per repository. A tenant with two repositories
    # and a single Jira binding looked like a tenant with one repository, so
    # "show a coloured badge on the reports dashboard" resolved to the BACKEND
    # and started implementing there. The router that would have caught it is
    # Rung 5's job and was never reached, so not even an audit row explains the
    # choice. `TENANT_REPOS_SQL` is the union both this and the router now ask.
    with conn.cursor() as cur:
        cur.execute(TENANT_REPOS_SQL, {"t": tenant_id})
        distinct_repos = [r[0] for r in cur.fetchall()]
    if len(distinct_repos) == 1:
        only = distinct_repos[0]
        cur_branch = None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT base_branch FROM repo_bindings "
                "WHERE tenant_id = %s AND repo = %s AND base_branch IS NOT NULL LIMIT 1",
                (tenant_id, only),
            )
            row = cur.fetchone()
            cur_branch = row[0] if row else None
        # A repo known only through `repo_profiles` has no binding to carry a
        # branch; 'main' is Rung 1's convention for exactly that case.
        return only, cur_branch or "main", []

    # Rung 5 — ambiguous or empty: no trustworthy resolution, so ask (never
    # guesses among multiple repos). No scope either: the origin said nothing,
    # so the router keeps the tenant-wide catalogue it has always had.
    return None, None, []
