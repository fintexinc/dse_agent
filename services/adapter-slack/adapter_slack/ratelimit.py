"""Rate-limit backoff for the outbound Slack Web API calls.

Slack's protocol is NOT GitHub's: there is no quota-remaining header to read.
A throttled call answers `429` with `Retry-After` in seconds, and the body says
`{"ok": false, "error": "ratelimited"}`. `slack_sdk.WebClient` surfaces that as a
`SlackApiError` carrying the `SlackResponse` (`.status_code`, `.headers`,
`.data`), but some methods hand back an `ok: false` payload instead of raising,
so both shapes are classified here.

Only those two shapes enter the retry path. `invalid_auth`, `channel_not_found`
and friends are permanent for the call that produced them, and a
`chat.postMessage` that timed out may already have posted — retrying either one
would turn "exactly 1 status message per task" into two.

THE BUDGET BELONGS TO THE CALLER, NOT TO THE CALL
-------------------------------------------------
`call_with_backoff` takes an absolute `deadline` and has NO default, because a
per-call budget bounds nothing that anybody is actually waiting on:
`/internal/reconcile` walks up to 50 threads inside ONE HTTP request, so 50
calls each entitled to their own 60s budget is up to 50 minutes of sleeping in a
request the CronJob abandons after 120s. Whoever calls Slack knows when its own
caller gives up — the reconciler CronJob, the orchestrator's 8s status-comment
timeout, a human who just clicked a button — and passes that instant in. One
deadline shared by every call of one sweep is the only scope in which "we will
not overrun our caller" can be a true statement.

A HINT WE CANNOT AFFORD IS NOT SHORTENED
----------------------------------------
`Retry-After` says when the quota is back. Sleeping less than that and asking
again is not a partial retry, it is an early poke: the answer is another 429,
and GitHub (same pattern, see `adapter_github.ratelimit`) documents that it can
extend an abuse block. So a hint that does not fit in the remaining budget makes
the call give up on the spot and leave the retry to whoever calls us next.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

logger = logging.getLogger("adapter_slack.ratelimit")

MAX_ATTEMPTS = 4
BASE_BACKOFF_S = 1.0


class SlackRateLimited(RuntimeError):
    """The retry budget ran out while Slack was still returning `ratelimited`.

    Its own type so the best-effort callers (`_notify_ephemeral`, the reconciler
    sweep) can log "Slack throttled us" without having to unpack a
    `SlackApiError` to find out whether the failure was even transient.
    """

    def __init__(self, message: str, *, last_error: BaseException | None = None):
        super().__init__(message)
        self.last_error = last_error


def _header(response: Any, name: str) -> str | None:
    """Case-insensitive header read that survives fake transports.

    `SlackResponse.headers` is a plain dict built from the HTTP response, so
    `"Retry-After"` and `"retry-after"` are distinct keys depending on who built
    it — the fakes in the tests included.
    """
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get(name)
    if value is None:
        lowered = {str(k).lower(): v for k, v in dict(headers).items()}
        value = lowered.get(name.lower())
    return None if value is None else str(value)


def _body_error(response: Any) -> str | None:
    """The `error` field of the Slack payload, for both `SlackResponse` (`.data`)
    and a raw dict."""
    data = getattr(response, "data", None)
    if data is None and isinstance(response, dict):
        data = response
    if isinstance(data, dict):
        error = data.get("error")
        return None if error is None else str(error)
    return None


def is_rate_limited(response: Any) -> bool:
    """True for Slack's throttle answer, in either of the shapes it arrives in:
    HTTP 429, or an `ok: false` body whose error is `ratelimited`."""
    if response is None:
        return False
    if getattr(response, "status_code", None) == 429:
        return True
    return _body_error(response) == "ratelimited"


def retry_delay_s(response: Any, *, attempt: int) -> float:
    """Wait before repeating the call, as Slack asked for it.

    `Retry-After` is returned VERBATIM — never trimmed to something we would
    rather wait (see the module docstring). Without it (an `ok: false` body with
    no HTTP headers) we fall back to exponential backoff with jitter, so several
    replicas sharing one workspace token do not all wake up together and
    re-trigger the limit.
    """
    raw = _header(response, "retry-after")
    if raw is not None:
        try:
            return max(float(raw.strip()), 0.0)
        except ValueError:
            pass  # unparseable hint -> treat it as no hint at all
    return BASE_BACKOFF_S * (2 ** attempt) * random.uniform(0.5, 1.0)


def call_with_backoff(
    send: Callable[[], Any],
    *,
    what: str,
    deadline: float,
    max_attempts: int = MAX_ATTEMPTS,
) -> Any:
    """Calls `send()` and repeats it verbatim while Slack answers `ratelimited`,
    for as long as `deadline` allows.

    `deadline` is an absolute `time.monotonic()` reading, required and meant to be
    SHARED: pass the same value to every call of one sweep, or the budget quietly
    becomes per-call again and bounds nothing.

    The clock and the sleep are read off the module-level `time` here rather than
    injected. Injectable defaults are bound once at def time, so a test that
    patched `adapter_slack.ratelimit.time.sleep` and forgot to also pass
    `sleep=...` patched nothing and slept for real — which is the same reason a
    budget assertion could pass against code that ignored the budget. Tests swap
    the whole module with `monkeypatch.setattr(ratelimit, "time", clock)`.

    Repeating a non-idempotent write (`chat.postMessage`) is only sound because
    the retry path is entered exclusively on a throttle answer: Slack rejected
    the call before posting anything, so there is no message to duplicate. Any
    other exception — including a timeout, where the message may have landed —
    is re-raised on the spot.
    """
    attempts = 0
    last_error: BaseException | None = None
    for attempt in range(max_attempts):
        attempts += 1
        throttled: Any
        try:
            response = send()
        except Exception as err:  # noqa: BLE001 — re-raised below unless throttled
            throttled = getattr(err, "response", None)
            if not is_rate_limited(throttled):
                raise
            last_error = err
        else:
            if not is_rate_limited(response):
                return response
            throttled = response
            last_error = None

        if attempt == max_attempts - 1:
            break
        delay = retry_delay_s(throttled, attempt=attempt)
        if time.monotonic() + delay > deadline:
            # Out of budget. Waiting less than Slack asked for and calling again
            # would only collect another 429 while our caller is already gone.
            break
        logger.warning(
            "slack rate limited on %s, sleeping %.1fs (attempt %d/%d)",
            what, delay, attempt + 1, max_attempts,
        )
        time.sleep(delay)

    raise SlackRateLimited(
        f"slack still rate limiting {what} after {attempts} attempt(s), "
        f"{max(deadline - time.monotonic(), 0.0):.1f}s of budget left",
        last_error=last_error,
    )


class RateLimitedSlackClient:
    """Wraps a Slack client and routes every call through `call_with_backoff`.

    A wrapper rather than retry code inside `SlackCommentBackend`: the adapter
    calls Slack from three places (the comment backend, the ephemeral notice,
    the reply reconciler), and only a wrapper at the client boundary covers all
    three. It implements the same `_SlackClientLike` surface, so neither the
    backend nor `app.py` can tell it apart from a bare `WebClient`.

    The `deadline` is the CALLER's and is handed in at construction, so every
    call this client makes draws on the same budget: one client per request means
    one budget per request. There is no default — a client built without a
    deadline would sleep on Slack's schedule instead of its caller's.
    """

    def __init__(self, inner: Any, *, deadline: float):
        self._inner = inner
        self._deadline = deadline

    def _call(self, what: str, fn: Callable[[], Any]) -> Any:
        return call_with_backoff(fn, what=what, deadline=self._deadline)

    def chat_postMessage(self, **kwargs) -> Any:
        # Not idempotent in general; safe here only because a throttled post
        # never reached the channel (see `call_with_backoff`).
        return self._call("chat.postMessage", lambda: self._inner.chat_postMessage(**kwargs))

    def chat_update(self, **kwargs) -> Any:
        # Idempotent: the update sets the message to an absolute text/blocks
        # value, so repeating it converges on the same message.
        return self._call("chat.update", lambda: self._inner.chat_update(**kwargs))

    def chat_postEphemeral(self, **kwargs) -> Any:
        # Same argument as chat.postMessage; a duplicate would be a repeated
        # notice to one user, and a throttle guarantees there was none.
        return self._call("chat.postEphemeral", lambda: self._inner.chat_postEphemeral(**kwargs))

    def conversations_replies(self, **kwargs) -> Any:
        # Read-only: repeatable by definition.
        return self._call("conversations.replies", lambda: self._inner.conversations_replies(**kwargs))
