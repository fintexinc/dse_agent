"""Rate-limit backoff of the outbound Slack calls (`adapter_slack.ratelimit`) and
the wait budgets the endpoints hand it.

Transport-level only: the Slack client is a fake that raises/returns the exact
shapes the Web API produces, and the `clock` fixture replaces the `time` module
for both `ratelimit` (which sleeps) and `app` (which sets the deadlines), so no
Postgres, no network and no real waiting are involved.

The budgets live here rather than in test_outbound.py because that suite needs a
live database and this one does not — and "how long will this endpoint make its
caller wait" is a rate-limit question.
"""
from __future__ import annotations

import sys
import time

import pytest

import adapter_slack.app as app_module
import adapter_slack.backend as backend_module
import adapter_slack.ratelimit as ratelimit_module
from adapter_slack.app import STATUS_COMMENT_BUDGET_S, StatusCommentRequest
from adapter_slack.backend import SlackCommentBackend, build_real_slack_client
from adapter_slack.ratelimit import (
    RateLimitedSlackClient,
    SlackRateLimited,
    call_with_backoff,
    is_rate_limited,
    retry_delay_s,
)

from .helpers import Clock


@pytest.fixture(autouse=True)
def _cleanup():
    """Shadows the Postgres cleanup fixture from conftest: nothing here touches
    the database, and the teardown connection would be the only reason these
    tests needed one."""
    yield


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """Replaces the `time` module for the code that sleeps AND for the endpoints
    that compute the deadline.

    Both, because a deadline built from the real `time.monotonic()` and compared
    against a fake clock is two unrelated numbers: the comparison then says
    nothing about the budget and the assertion holds no matter what the code does.
    And `sleep` has to advance the clock (see `helpers.Clock`) or the budget never
    depletes.
    """
    fake = Clock()
    monkeypatch.setattr(ratelimit_module, "time", fake)
    monkeypatch.setattr(app_module, "time", fake)
    return fake


class FakeSlackResponse:
    """Shaped like `slack_sdk.web.SlackResponse`: an HTTP status, the raw headers
    and the decoded body under `.data`."""

    def __init__(self, *, status_code: int = 200, headers: dict | None = None, data: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self.data = data or {"ok": True}

    def __getitem__(self, key):
        return self.data[key]


class FakeSlackApiError(Exception):
    """`slack_sdk.errors.SlackApiError` in miniature — the only thing the
    classifier looks at is the attached `response`."""

    def __init__(self, response):
        super().__init__("slack api error")
        self.response = response


def _throttled(retry_after: int | None = 1) -> FakeSlackResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return FakeSlackResponse(status_code=429, headers=headers, data={"ok": False, "error": "ratelimited"})


class ScriptedSlackClient:
    """Replays a script per method: an exception instance is raised, anything
    else is returned. Records every call so a retry can be told from a
    duplicate post. A script down to its last entry keeps replaying it, so a test
    can say "throttled from here on" without counting attempts."""

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[dict] = []

    def _next(self, name: str, kwargs: dict):
        self.calls.append({"method": name, **kwargs})
        outcome = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def chat_postMessage(self, **kwargs):
        return self._next("chat_postMessage", kwargs)

    def chat_update(self, **kwargs):
        return self._next("chat_update", kwargs)

    def chat_postEphemeral(self, **kwargs):
        return self._next("chat_postEphemeral", kwargs)

    def conversations_replies(self, **kwargs):
        return self._next("conversations_replies", kwargs)

    def views_open(self, **kwargs):
        return self._next("views_open", kwargs)


def _raise_or_return(outcome):
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


class _MemoryCommentStore:
    """`CommentStateStore` in memory. Stands in for the Postgres store only so the
    status-comment endpoint can be called without a database; what these tests
    assert is the waiting, not the persistence."""

    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str] = {}

    def get_ref(self, work_item_id: str, surface: str) -> str | None:
        return self.refs.get((work_item_id, surface))

    def save_ref(self, work_item_id: str, surface: str, comment_ref: str) -> None:
        self.refs[(work_item_id, surface)] = comment_ref


# ---------------------------------------------------------------------------
# Classification and delay
# ---------------------------------------------------------------------------
def test_only_slacks_throttle_shapes_count_as_rate_limited():
    assert is_rate_limited(_throttled())
    assert is_rate_limited({"ok": False, "error": "ratelimited"})  # body-only shape
    assert not is_rate_limited(FakeSlackResponse(status_code=200))
    assert not is_rate_limited(FakeSlackResponse(status_code=403, data={"ok": False, "error": "invalid_auth"}))
    assert not is_rate_limited(None)


def test_retry_after_is_honoured_verbatim_and_never_truncated():
    """Truncating a hint is not honouring it: `min(600, 30)` sleeps 30s and re-asks
    a quota Slack just said needs 600s, which is another 429 on a limit that counts
    our requests."""
    assert retry_delay_s(_throttled(retry_after=12), attempt=0) == 12.0
    assert retry_delay_s(_throttled(retry_after=600), attempt=0) == 600.0


def test_backoff_is_bounded_and_jittered_without_a_header():
    body_only = {"ok": False, "error": "ratelimited"}
    first = [retry_delay_s(body_only, attempt=0) for _ in range(20)]
    later = [retry_delay_s(body_only, attempt=3) for _ in range(20)]
    assert all(0.5 <= d <= 1.0 for d in first)
    assert all(4.0 <= d <= 8.0 for d in later)
    assert len(set(first)) > 1


# ---------------------------------------------------------------------------
# The retry loop
# ---------------------------------------------------------------------------
def test_throttled_error_is_retried_until_it_succeeds(clock):
    outcomes = [FakeSlackApiError(_throttled(2)), FakeSlackApiError(_throttled(3)), FakeSlackResponse()]
    resp = call_with_backoff(
        lambda: _raise_or_return(outcomes.pop(0)),
        what="chat.postMessage",
        deadline=clock.monotonic() + 60.0,
    )
    assert resp.status_code == 200
    assert clock.slept == [2.0, 3.0]


def test_non_rate_limit_error_is_re_raised_without_sleeping(clock):
    # `channel_not_found`/`invalid_auth` are permanent for this call; routing
    # them through the retry path would just delay the failure.
    err = FakeSlackApiError(FakeSlackResponse(status_code=400, data={"ok": False, "error": "channel_not_found"}))
    with pytest.raises(FakeSlackApiError):
        call_with_backoff(
            lambda: _raise_or_return(err), what="chat.postMessage",
            deadline=clock.monotonic() + 60.0,
        )
    assert clock.slept == []


def test_a_timeout_is_never_retried(clock):
    # A timed-out chat.postMessage may already have posted; retrying it would
    # break "exactly 1 status message per task".
    with pytest.raises(TimeoutError):
        call_with_backoff(
            lambda: _raise_or_return(TimeoutError("read timed out")),
            what="chat.postMessage",
            deadline=clock.monotonic() + 60.0,
        )
    assert clock.slept == []


def test_a_hint_bigger_than_the_budget_gives_up_without_poking_slack_again(clock):
    """The hint does not fit, so the call ends after ONE request. Sleeping the
    part we can afford and asking anyway is an early poke: it cannot succeed, and
    on a request-counting limit it makes the throttle worse."""
    inner = ScriptedSlackClient([FakeSlackApiError(_throttled(2400))])

    with pytest.raises(SlackRateLimited):
        call_with_backoff(
            lambda: inner.chat_postMessage(channel="C1", text="hi"),
            what="chat.postMessage",
            deadline=clock.monotonic() + 60.0,
        )

    assert clock.slept == []
    assert len(inner.calls) == 1


def test_attempts_are_bounded_even_with_a_one_second_hint(clock):
    with pytest.raises(SlackRateLimited):
        call_with_backoff(
            lambda: _raise_or_return(FakeSlackApiError(_throttled(1))),
            what="chat.update",
            deadline=clock.monotonic() + 60.0,
        )
    assert len(clock.slept) == 3  # MAX_ATTEMPTS calls -> MAX_ATTEMPTS - 1 sleeps


def test_the_budget_left_is_reported_when_it_runs_out(clock):
    """The reconciler logs this exception; without the remaining budget in the
    message there is no telling "we ran out of time" from "Slack is down" in the
    CronJob output."""
    with pytest.raises(SlackRateLimited) as excinfo:
        call_with_backoff(
            lambda: _raise_or_return(FakeSlackApiError(_throttled(25))),
            what="conversations.replies",
            deadline=clock.monotonic() + 60.0,
        )
    assert clock.slept == [25.0, 25.0]  # a third 25s wait would exceed the budget
    assert "10.0s of budget left" in str(excinfo.value)
    assert isinstance(excinfo.value.last_error, FakeSlackApiError)


# ---------------------------------------------------------------------------
# One budget per caller, not per call
# ---------------------------------------------------------------------------
def test_one_client_spends_one_budget_across_every_call_of_a_sweep(clock):
    """BLOCKER: `/internal/reconcile` calls `conversations.replies` once per work
    item, up to 50 of them, inside ONE HTTP request. With a budget that restarted
    per call, 50 throttled items were ~50 minutes of sleeping in a request the
    CronJob abandons after 120s. One client, one deadline: the sweep as a whole
    never sleeps past its budget."""
    inner = ScriptedSlackClient([FakeSlackApiError(_throttled(25))])
    client = RateLimitedSlackClient(inner, deadline=clock.monotonic() + 60.0)

    for _ in range(5):  # five work items of one sweep, all throttled
        with pytest.raises(SlackRateLimited):
            client.conversations_replies(channel="C1", ts="1.0")

    assert clock.slept == [25.0, 25.0]  # the whole sweep, not per item
    assert sum(clock.slept) <= 60.0


# ---------------------------------------------------------------------------
# The client wrapper and its one production builder
# ---------------------------------------------------------------------------
def test_wrapper_posts_exactly_once_through_a_throttle(clock):
    inner = ScriptedSlackClient(
        [FakeSlackApiError(_throttled(1)), FakeSlackResponse(data={"ok": True, "ts": "1700.0001"})]
    )
    client = RateLimitedSlackClient(inner, deadline=clock.monotonic() + 60.0)

    # Through the real comment backend, so the retry is exercised on the same
    # call path the orchestrator uses for the status message.
    backend = SlackCommentBackend(client)
    comment_ref = backend.post({"channel": "C1"}, "working on it")

    assert "1700.0001" in comment_ref
    assert [c["method"] for c in inner.calls] == ["chat_postMessage", "chat_postMessage"]
    assert {c["text"] for c in inner.calls} == {"working on it"}  # verbatim repeat


def test_wrapper_forwards_every_client_method(clock):
    """Inclui `views_open` desde 2026-08-11, e a razão é um incidente: o
    `FakeSlackClient` tinha o método e ESTE wrapper não. Todos os testes usavam
    o fake, a suíte ficava verde, e o primeiro clique real do canal morria em
    `AttributeError` — medido em produção em 2026-08-10. Um método que só o
    fake conhece é um método que não existe."""
    inner = ScriptedSlackClient([FakeSlackResponse() for _ in range(5)])
    client = RateLimitedSlackClient(inner, deadline=clock.monotonic() + 60.0)

    client.chat_postMessage(channel="C1", text="a")
    client.chat_update(channel="C1", ts="1.0", text="b")
    client.chat_postEphemeral(channel="C1", user="U1", text="c")
    client.conversations_replies(channel="C1", ts="1.0")
    client.views_open(trigger_id="t.1", view={"type": "modal"})

    assert [c["method"] for c in inner.calls] == [
        "chat_postMessage", "chat_update", "chat_postEphemeral",
        "conversations_replies", "views_open",
    ]


def test_the_production_builder_actually_wraps_the_web_client(monkeypatch):
    """`build_real_slack_client` is the ONE line that makes Slack backoff exist in
    production. Untested, the whole feature could be reverted to a bare
    `WebClient` with a green suite — so this asserts the BEHAVIOUR (a throttle is
    absorbed) rather than the type, and pins the bounded socket timeout that keeps
    `_notify_ephemeral` from parking the event loop for slack_sdk's default 30s.

    No `clock` fixture here on purpose: the builder is what wires the client to
    the real `time`, and that wiring is part of what is under test. `Retry-After:
    0` keeps the real retry instantaneous."""
    built: list[dict] = []
    inner = ScriptedSlackClient(
        [FakeSlackApiError(_throttled(0)), FakeSlackResponse(data={"ok": True, "ts": "1700.0002"})]
    )

    class FakeWebClient:
        def __init__(self, *, token: str, timeout: int | None = None):
            built.append({"token": token, "timeout": timeout})

        def __getattr__(self, name):
            return getattr(inner, name)

    monkeypatch.setitem(
        sys.modules, "slack_sdk", type("slack_sdk", (), {"WebClient": FakeWebClient})
    )

    client = build_real_slack_client("xoxb-token", deadline=time.monotonic() + 60.0)
    resp = client.chat_postMessage(channel="C1", text="hi")

    assert built == [{"token": "xoxb-token", "timeout": backend_module.HTTP_TIMEOUT_S}]
    assert backend_module.HTTP_TIMEOUT_S < 30  # slack_sdk's default, too long for the event loop
    # Two calls for one post: the wrapper absorbed the 429. A bare WebClient
    # would have raised the first FakeSlackApiError straight out.
    assert [c["method"] for c in inner.calls] == ["chat_postMessage", "chat_postMessage"]
    assert resp["ts"] == "1700.0002"


# ---------------------------------------------------------------------------
# The budgets the endpoints choose
# ---------------------------------------------------------------------------
def _status_comment_request() -> StatusCommentRequest:
    return StatusCommentRequest(
        work_item_id="wi_budget", channel="C_STATUS", body="queued",
        actor="system:orchestrator",
    )


def _wire_status_comment(monkeypatch, inner: ScriptedSlackClient) -> None:
    """Runs the real endpoint function with the real backoff wrapper — only the
    Postgres store, the audit write and the token read are stubbed."""
    monkeypatch.setattr(app_module, "PgCommentStateStore", _MemoryCommentStore)
    monkeypatch.setattr(app_module, "audit_emit", lambda **kw: None)
    monkeypatch.setattr(app_module, "get_slack_bot_token", lambda: "xoxb-test")
    monkeypatch.setattr(
        app_module, "build_real_slack_client",
        lambda token, *, deadline: RateLimitedSlackClient(inner, deadline=deadline),
    )


def test_status_comment_gives_up_before_the_orchestrator_stops_waiting(clock, monkeypatch):
    """HIGH: the orchestrator posts status comments best-effort with an 8s
    timeout. A retry that outlives it is worse than none: the orchestrator walks
    away, `MutableCommentWriter` never saves the ref, and the NEXT transition
    finds no ref and posts a SECOND status message — breaking the one invariant
    the mutable comment exists to keep. So a 5s hint must not be slept on."""
    inner = ScriptedSlackClient([FakeSlackApiError(_throttled(5))])
    _wire_status_comment(monkeypatch, inner)

    with pytest.raises(SlackRateLimited):
        app_module.upsert_status_comment(_status_comment_request())

    assert clock.slept == []
    assert len(inner.calls) == 1  # nothing was posted, so nothing to duplicate
    assert STATUS_COMMENT_BUDGET_S < 8.0


def test_status_comment_still_absorbs_a_short_throttle(clock, monkeypatch):
    """The other half of the bound: the budget is small, not zero. Slack's
    per-channel hints are routinely a second or two, and those must still be
    waited out — otherwise every mildly throttled transition loses its status
    update."""
    inner = ScriptedSlackClient(
        [FakeSlackApiError(_throttled(1)), FakeSlackResponse(data={"ok": True, "ts": "1700.0003"})]
    )
    _wire_status_comment(monkeypatch, inner)

    result = app_module.upsert_status_comment(_status_comment_request())

    assert result["ok"] is True
    assert clock.slept == [1.0]
    assert [c["method"] for c in inner.calls] == ["chat_postMessage", "chat_postMessage"]


def test_the_ephemeral_notice_never_sleeps(clock, monkeypatch):
    """BLOCKER: `_notify_ephemeral` is reached from `slack_interactions`, a
    COROUTINE, and `RateLimitedSlackClient` sleeps with a blocking `time.sleep`.
    `chat.postEphemeral` is limited to about one call per second per channel, so a
    burst of Confirm clicks throttles routinely — and one such sleep parks the
    uvicorn event loop: `/slack/events` stalls past Slack's 3s timeout and
    `/health` stops answering until the liveness probe restarts the pod. The
    deadline for this path is `now`, so no throttle can sleep on it."""
    inner = ScriptedSlackClient([FakeSlackApiError(_throttled(30))])
    monkeypatch.setattr(app_module, "get_slack_bot_token", lambda: "xoxb-test")
    monkeypatch.setattr(
        app_module, "build_real_slack_client",
        lambda token, *, deadline: RateLimitedSlackClient(inner, deadline=deadline),
    )

    # Best-effort: it swallows the failure rather than breaking the interaction.
    app_module._notify_ephemeral("C1", "U1", "Pick a repository, then hit Confirm.")

    assert clock.slept == []
    assert len(inner.calls) == 1
