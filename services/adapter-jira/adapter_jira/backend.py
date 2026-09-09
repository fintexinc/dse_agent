"""WSA-E5-T3 — Jira client (real transport via `requests` + in-memory
`FakeJiraClient` fixture) and `JiraCommentBackend` (implements
`dse_contracts.mutable_comment.CommentBackend`, the third backend of the same
`MutableCommentWriter` already used by Slack and GitHub).

Authentication: Basic auth with the service account email + a scoped
(project-level) API token, read from Vault (`adapter_jira.config`). With no
real Jira site in this session, `FakeJiraClient` replaces the transport in the
tests — the `JiraCommentBackend`/transition-serialization/poller logic is 100%
real.

REST API v3 (the current one for Jira Cloud). Comment body in ADF (Atlassian
Document Format) — `_adf()` wraps plain text in the minimal accepted doc.

Rate limiting: Jira Cloud budgets requests per account, and every DSE component
that talks to Jira (poller, transition worker, status-comment writer) shares one
account. A 429 is therefore routine and means "come back later", not "this
failed" — `RealJiraClient._request` is the single place that knows the
difference (see its docstring).
"""
from __future__ import annotations

import base64
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import requests

logger = logging.getLogger("adapter_jira.backend")

# Retries apply to 429 ONLY (see `_request`). Four extra attempts per request,
# and — the number that actually bounds a sweep — at most `_RATE_LIMIT_TOTAL_BUDGET`
# seconds of 429 waiting per CLIENT inside any `_RATE_LIMIT_BUDGET_WINDOW`.
#
# The budget has to be shared across requests, not per request: a sweep issues
# one search plus several calls per ticket, so a per-request budget of 30s is
# really "30s times however many requests the sweep makes". Rate limiting is a
# property of the shared account, so the ceiling belongs to the client too.
_RATE_LIMIT_MAX_RETRIES = 4
_RATE_LIMIT_BASE_DELAY = 0.5
_RATE_LIMIT_MAX_DELAY = 8.0
_RATE_LIMIT_TOTAL_BUDGET = 30.0
# One poller interval: whatever the sweep spends waiting is repaid before the
# next sweep starts, so consecutive sweeps cannot inherit an exhausted budget.
_RATE_LIMIT_BUDGET_WINDOW = 60.0


class JiraRateLimited(RuntimeError):
    """Jira kept answering 429 and the bounded wait budget ran out.

    A distinct type on purpose: this is a TRANSIENT fact about timing, so the
    callers that classify failures (the transition worker separates transient
    failures from board misconfiguration) must not confuse it with anything
    permanent.
    """

    def __init__(self, method: str, path: str, *, retries: int, retry_after: float | None):
        super().__init__(f"jira rate limited: {method} {path} gave up after {retries} 429 response(s)")
        self.retry_after = retry_after
        self.retries = retries


def _parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` in seconds, or None when the header is absent/unreadable.

    Jira Cloud sends an integer count of seconds, but the header is also allowed
    to carry an HTTP-date, so both are accepted. Anything else returns None and
    the caller falls back to its own backoff rather than trusting a value it
    could not parse.
    """
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:  # HTTP-dates are GMT; a naive one would break the subtraction
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _backoff_delay(retry: int, *, rng=random.random) -> float:
    """Exponential backoff with equal jitter, capped per attempt.

    The jitter is not decoration. Poller, transition worker and comment writer
    share one rate-limited Jira account, so a fixed schedule makes all three
    come back in lockstep and collect the same 429 again. Half the delay stays
    fixed so a jittered wait can never collapse to ~0 and hammer the endpoint.
    """
    capped = min(_RATE_LIMIT_MAX_DELAY, _RATE_LIMIT_BASE_DELAY * (2 ** (retry - 1)))
    return capped / 2 + rng() * capped / 2


def _adf(text: str) -> dict[str, Any]:
    """Minimal ADF doc (a single paragraph) — the format required by the v3 API
    comment endpoint. `FakeJiraClient` ignores the format and stores the text
    exactly as it came in."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class JiraClientLike(Protocol):
    def add_comment(self, key: str, body: str) -> str: ...
    def update_comment(self, key: str, comment_id: str, body: str) -> None: ...
    def get_transitions(self, key: str) -> list[dict[str, Any]]: ...
    def transition_issue(self, key: str, transition_id: str) -> None: ...
    def search_updated(self, project_key: str, since_iso: str | None) -> list[dict[str, Any]]: ...
    def get_comments(self, key: str) -> list[dict[str, Any]]: ...
    def remove_label(self, key: str, label: str) -> None: ...
    def get_labels(self, key: str) -> list[str]: ...
    # None means "could not be asked" — never "the board has no such status".
    def project_statuses(self, project_key: str) -> set[str] | None: ...


class JiraCommentBackend:
    """`CommentBackend` for the `MutableCommentWriter` (surface='jira').
    `surface_ref` = `{"ticket_key": "DSE-123"}`; opaque `comment_ref` =
    JSON `{"ticket_key", "comment_id"}`."""

    def __init__(self, client: JiraClientLike):
        self._client = client

    def post(self, surface_ref: dict, body: str) -> str:
        key = surface_ref["ticket_key"]
        comment_id = self._client.add_comment(key, body)
        return json.dumps({"ticket_key": key, "comment_id": comment_id})

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        ref = json.loads(comment_ref)
        self._client.update_comment(ref["ticket_key"], ref["comment_id"], body)


class RealJiraClient:
    """Real transport against the Jira Cloud REST API v3."""

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        *,
        session: requests.Session | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self._base = base_url.rstrip("/")
        self._auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._http = session or requests.Session()
        # Injected so the rate-limit tests assert the schedule without spending
        # the wall-clock time it describes. `clock` is monotonic: the budget must
        # not be widened or closed by an NTP step.
        self._sleep = sleep
        self._clock = clock
        # (finished_at, seconds) of each 429 wait, pruned to the budget window.
        self._waits: deque[tuple[float, float]] = deque()
        self._account_id: str | None = None
        self._account_id_resolved = False

    def _wait_spent(self) -> float:
        """Seconds this client has already spent waiting on 429s inside the
        current budget window (older waits are forgotten)."""
        cutoff = self._clock() - _RATE_LIMIT_BUDGET_WINDOW
        while self._waits and self._waits[0][0] < cutoff:
            self._waits.popleft()
        return sum(seconds for _, seconds in self._waits)

    def _request(self, method: str, path: str, *, timeout: int = 10, **kwargs) -> requests.Response:
        """Single HTTP entry point, so 429 handling lives in exactly one place.

        Every call site used to end in `raise_for_status()`, which turned "come
        back in two seconds" into a hard failure: the transition worker spent one
        of its five attempts on it and the poller abandoned the issue for that
        sweep.

        Only 429 is retried. A 400/401/403/404 is a fact about the REQUEST, not
        about timing — retrying it just collects the same error more slowly — so
        those still raise on the first response.

        The wait is budgeted PER CLIENT AND PER WINDOW, not per request, because
        the thing that must not overrun is the sweep: the poller is
        single-threaded and issues many requests per cycle, so a per-request
        budget bounds nothing (five rate-limited calls at 30s each is 150s, and
        the sweep interval is 60s). Once the window's budget cannot cover the
        next wait we give up with `JiraRateLimited` instead of blocking — the
        caller records it like any other transient failure, and the budget
        refills as the window slides so the next sweep is not punished for this
        one.
        """
        url = f"{self._base}{path}"
        retries = 0
        while True:
            resp = self._http.request(method, url, headers=self._headers(), timeout=timeout, **kwargs)
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp

            retries += 1
            # The server's own number wins when it sends one — it knows when the
            # bucket refills and guessing shorter only burns another request.
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            delay = retry_after if retry_after is not None else _backoff_delay(retries)
            if retries > _RATE_LIMIT_MAX_RETRIES or self._wait_spent() + delay > _RATE_LIMIT_TOTAL_BUDGET:
                raise JiraRateLimited(method, path, retries=retries, retry_after=retry_after)
            logger.warning(
                "jira rate limited (429) on %s %s; waiting %.2fs (retry %d, retry_after_header=%s)",
                method,
                path,
                delay,
                retries,
                retry_after,
            )
            self._sleep(delay)
            self._waits.append((self._clock(), delay))

    def self_account_id(self) -> str | None:
        """accountId of the account the DSE posts as, resolved AT MOST ONCE.

        Needed to recognise the DSE's OWN comments: Jira attributes them to the
        token owner, so without this the poller reads the bot's question back as
        the human's answer (see `ingest.ingest_comment`). Best-effort — if the
        lookup fails, returning None only means the filter is inert, never that
        the flow breaks.

        The FAILURE is cached too, and that is the point of the extra flag. The
        poller calls this once per comment; while Jira was rate limiting, a
        cache that only remembered successes re-issued `/myself` for every
        comment and each one paid the 429 backoff — five comments, 150s of a 60s
        sweep, spent on an identity lookup whose answer never changes. An
        unresolved id costs nothing by comparison: the self-authored filter that
        matters keys on the comment id the writer recorded, not on the author.
        """
        if not self._account_id_resolved:
            try:
                self._account_id = self._request("GET", "/rest/api/3/myself").json().get("accountId")
            except Exception:  # noqa: BLE001 — identity lookup must never break ingestion
                logger.warning("could not resolve the DSE's own Jira accountId", exc_info=True)
            finally:
                self._account_id_resolved = True
        return self._account_id

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Basic {self._auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def add_comment(self, key: str, body: str) -> str:
        resp = self._request("POST", f"/rest/api/3/issue/{key}/comment", json={"body": _adf(body)})
        return str(resp.json()["id"])

    def update_comment(self, key: str, comment_id: str, body: str) -> None:
        self._request(
            "PUT",
            f"/rest/api/3/issue/{key}/comment/{comment_id}",
            json={"body": _adf(body)},
        )

    def remove_label(self, key: str, label: str) -> None:
        """Removes ONE label, leaving every other label on the issue alone.

        `update.labels.remove` is a field operation applied server-side. The
        obvious alternative — read the labels, drop one, PUT the whole list back —
        silently discards anything a human added in between.
        """
        self._request("PUT", f"/rest/api/3/issue/{key}", json={"update": {"labels": [{"remove": label}]}})

    def get_labels(self, key: str) -> list[str]:
        """The labels the issue carries RIGHT NOW.

        Read back after a removal, because the status code is not evidence: a
        PUT that answers 200 without applying would let the latch disarm, and a
        disarmed latch on a card that still shows the label is the one sequence
        that restarts the same work every sweep.
        """
        resp = self._request("GET", f"/rest/api/3/issue/{key}", params={"fields": "labels"})
        data = resp.json() or {}
        return list((data.get("fields") or {}).get("labels") or [])

    def project_statuses(self, project_key: str) -> set[str] | None:
        """Every status name the project's workflows contain, or None when the
        board could not be asked.

        This is a different question from `get_transitions`, and conflating the
        two discarded mirror updates that would have worked. `GET
        /issue/{key}/transitions` returns only the edges available FROM the
        issue's CURRENT status — so "no transition to Done" can mean the board has
        no Done column, or merely that this card is somewhere Done is not reachable
        from yet. `/project/{key}/statuses` answers the first question on its own:
        it lists the statuses of the project's workflow per issue type, which is a
        configuration fact and does not move when the card does.

        None (rather than an empty set) on failure, because "the board has no
        statuses at all" is not a thing and the caller must not settle a queue row
        on a lookup that simply did not answer.
        """
        try:
            resp = self._request("GET", f"/rest/api/3/project/{project_key}/statuses")
        except Exception:  # noqa: BLE001 — an unanswered lookup must not settle anything
            logger.warning("could not read the workflow statuses of project %s", project_key, exc_info=True)
            return None
        names: set[str] = set()
        for issue_type in resp.json() or []:
            for status in (issue_type or {}).get("statuses", []):
                name = (status or {}).get("name")
                if name:
                    names.add(name)
        return names or None

    def get_transitions(self, key: str) -> list[dict[str, Any]]:
        resp = self._request("GET", f"/rest/api/3/issue/{key}/transitions")
        out = []
        for t in resp.json().get("transitions", []):
            out.append({"id": str(t["id"]), "name": t.get("name", ""), "to_status": (t.get("to") or {}).get("name", "")})
        return out

    def transition_issue(self, key: str, transition_id: str) -> None:
        self._request(
            "POST",
            f"/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": transition_id}},
        )

    def search_updated(self, project_key: str, since_iso: str | None) -> list[dict[str, Any]]:
        jql = f'project = "{project_key}"'
        if since_iso:
            # The caller passes a RELATIVE bound (`-90m`), not a timestamp. An
            # absolute JQL literal is read in the Jira account's timezone, so a
            # UTC cursor queried into the future and matched nothing — see
            # `poller._relative_bound`. Relative bounds have no timezone.
            jql += f' AND updated >= "{since_iso}"'
        jql += " ORDER BY updated ASC"
        # New /search/jql endpoint: the old /rest/api/3/search was REMOVED by
        # Atlassian (410 Gone, 2025). Pagination by nextPageToken (no longer
        # startAt/total); iterates until isLast so no issues beyond the first
        # page are lost.
        # `reporter` is MANDATORY (BD-39 finding, 2026-07-23): without it the
        # poller creates the WorkItem with requester=system:adapter-jira-poller,
        # and then a clarification answer from the ticket's OWN author does not
        # authorize (the author's principal != the system principal) — the flow
        # gets stuck in needs_clarification forever.
        fields = ["summary", "description", "labels", "status", "project", "reporter"]
        issues: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": 100}
            if next_token:
                body["nextPageToken"] = next_token
            resp = self._request("POST", "/rest/api/3/search/jql", json=body, timeout=15)
            data = resp.json()
            issues.extend(data.get("issues", []))
            next_token = data.get("nextPageToken")
            if data.get("isLast") or not next_token:
                break
        return issues

    def get_comments(self, key: str) -> list[dict[str, Any]]:
        resp = self._request("GET", f"/rest/api/3/issue/{key}/comment")
        out = []
        for c in resp.json().get("comments", []):
            # `renderedBody`/`body` ADF -> text: best effort for the poller.
            body = c.get("body")
            text = body if isinstance(body, str) else _flatten_adf(body)
            out.append({"id": str(c["id"]), "body": text, "author": c.get("author") or {}})
        return out


def _flatten_adf(node: Any) -> str:
    """Extracts text from an ADF doc (best-effort) so the poller can rebuild
    the same content_snapshot as the webhook produced when it carried plain
    text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return "".join(_flatten_adf(ch) for ch in node.get("content", []))
    if isinstance(node, list):
        return "".join(_flatten_adf(ch) for ch in node)
    return ""


@dataclass
class FakeJiraClient:
    """In-memory fixture (documented — this is not the real API). Simulates the
    available transitions, comments and search. Used in the tests in place of
    the HTTP transport; the logic around it (backend/serialization/poller) is
    the real one."""

    # ticket_key -> list of {id, name, to_status}
    transitions_by_ticket: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # ticket_key -> current status (updated by transition_issue)
    status_by_ticket: dict[str, str] = field(default_factory=dict)
    # project_key -> the statuses of the project's workflow. Left None so a test
    # that does not configure it exercises the "the board did not answer" branch
    # rather than silently asserting a board shape it never described.
    statuses_by_project: dict[str, list[str]] | None = None
    # ticket_key -> {comment_id: body}
    comments: dict[str, dict[str, str]] = field(default_factory=dict)
    # issues indexable by the poller's search: project_key -> list[issue]
    issues_by_project: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    transition_calls: list[dict[str, Any]] = field(default_factory=list)
    label_removals: list[dict[str, str]] = field(default_factory=list)
    # accountId the fixture claims to post as; tests set it to exercise the
    # self-authored comment filter.
    account_id: str | None = None
    _next_comment_id: int = 9000

    def self_account_id(self) -> str | None:
        return self.account_id

    def get_labels(self, key: str) -> list[str]:
        """Reads back from the stored issue — the same place `remove_label`
        mutates. A fake that answered from the call log could not tell a removal
        that applied from one that only claimed to."""
        for issues in self.issues_by_project.values():
            for issue in issues:
                if issue.get("key") == key:
                    return list((issue.get("fields") or {}).get("labels") or [])
        return []

    def remove_label(self, key: str, label: str) -> None:
        """Mutates the stored issue, not just a call log: the retry label is
        single-shot precisely BECAUSE the next sweep no longer sees it, so a
        fixture that only recorded the call could not prove the property."""
        self.label_removals.append({"key": key, "label": label})
        for issues in self.issues_by_project.values():
            for issue in issues:
                if issue.get("key") != key:
                    continue
                labels = issue.setdefault("fields", {}).setdefault("labels", [])
                if label in labels:
                    labels.remove(label)

    def add_comment(self, key: str, body: str) -> str:
        self._next_comment_id += 1
        cid = str(self._next_comment_id)
        self.comments.setdefault(key, {})[cid] = body
        return cid

    def update_comment(self, key: str, comment_id: str, body: str) -> None:
        if comment_id not in self.comments.get(key, {}):
            raise KeyError(f"update_comment on nonexistent comment_id: {key}/{comment_id}")
        self.comments[key][comment_id] = body

    def get_transitions(self, key: str) -> list[dict[str, Any]]:
        return list(self.transitions_by_ticket.get(key, []))

    def project_statuses(self, project_key: str) -> set[str] | None:
        if self.statuses_by_project is None:
            return None
        names = self.statuses_by_project.get(project_key)
        return set(names) if names else None

    def transition_issue(self, key: str, transition_id: str) -> None:
        avail = self.transitions_by_ticket.get(key, [])
        match = next((t for t in avail if str(t["id"]) == str(transition_id)), None)
        if match is None:
            raise ValueError(f"nonexistent transition {transition_id} for {key}")
        self.status_by_ticket[key] = match["to_status"]
        self.transition_calls.append({"key": key, "transition_id": transition_id, "to_status": match["to_status"]})

    def search_updated(self, project_key: str, since_iso: str | None) -> list[dict[str, Any]]:
        return list(self.issues_by_project.get(project_key, []))

    def get_comments(self, key: str) -> list[dict[str, Any]]:
        return [{"id": cid, "body": body, "author": {}} for cid, body in self.comments.get(key, {}).items()]


def build_real_jira_client() -> RealJiraClient:
    from . import config

    return RealJiraClient(config.get_base_url(), config.get_service_account_email(), config.get_api_token())
