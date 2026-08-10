"""WSA-E3-T2 — outbound: the real `CommentBackend` against the Slack Web API
(`chat.postMessage`/`chat.update`), used by the shared
`dse_contracts.mutable_comment.MutableCommentWriter` —
exactly 1 status message per task, edited in-place.

No real Slack App credential in this session: the LOGIC is real (it uses
`slack_sdk.WebClient`, the same methods that would run against the real
API), only the token (`SLACK_BOT_TOKEN`) is a local/fixture value.
`FakeSlackClient` below implements the same surface
(`chat_postMessage`/`chat_update`) and is injected in place of
`slack_sdk.WebClient` in the tests — `SlackCommentBackend` does not know
(and does not need to know) which of the two it got.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from .ratelimit import RateLimitedSlackClient


class _SlackClientLike(Protocol):
    def chat_postMessage(self, *, channel: str, text: str, blocks: list | None = ...) -> dict: ...
    def chat_update(self, *, channel: str, ts: str, text: str, blocks: list | None = ...) -> dict: ...
    def chat_postEphemeral(self, *, channel: str, user: str, text: str) -> dict: ...
    def conversations_replies(self, *, channel: str, ts: str) -> dict: ...


def approval_blocks(body: str) -> list[dict]:
    """Block Kit for plan approval (Phase B / report 07): the status text
    + Approve/Reject buttons. The `action_id`/`value` are the markers that
    `parse_slack_approval` reads (deterministic verdict/route — C1). Without
    posting these buttons, the human had no way to approve/reject from Slack."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {
            "type": "actions",
            "block_id": "dse_plan_approval",
            "elements": [
                {"type": "button", "action_id": "dse_plan_approve", "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve"}, "value": "approve"},
                {"type": "button", "action_id": "dse_plan_reject", "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject"}, "value": "reject:re_plan"},
            ],
        },
    ]


def repo_select_blocks(work_item_id: str, repos: list[str], body: str) -> list[dict]:
    """Block Kit for the repo selector (ambiguous-repo clarification — resolve_repo
    Rung 5). One section (the question text) + one actions block holding the
    static_select AND a confirm button.

    TWO STEPS on purpose: Slack fires a `block_actions` as soon as the
    static_select is picked, so a select on its own would make the selection
    irreversible on the first click — picking the wrong repo would fire an agent
    turn against the wrong repo. With the button, the choice only becomes a
    signal on `dse_repo_confirm`; until then the human can switch options freely
    (Slack keeps the selection in the message `state`, which is where the handler
    reads it from on the click).

    Both elements live in the SAME actions block because `state.values` is
    indexed by block_id -> action_id: with a single `block_id`, the button click
    finds the selection without having to guess the neighbouring block. The
    `block_id` carries the work_item_id — /slack/interactions pulls it from there
    and addresses the signal WITHOUT relying on source_ref correlation (the
    status-comment is posted outside the thread). Each option.value = the repo
    (owner/name). Confirming is equivalent to answering `repo=<choice>` to the
    clarification — the same dispatcher->workflow path as the text."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {
            "type": "actions",
            "block_id": f"dse_repo_select:{work_item_id}",
            "elements": [
                {
                    "type": "static_select",
                    "action_id": "dse_repo_select",
                    "placeholder": {"type": "plain_text", "text": "Select a repository"},
                    "options": [
                        {"text": {"type": "plain_text", "text": r[:75]}, "value": r}
                        for r in repos[:100]  # Slack: max. 100 options
                    ],
                },
                {
                    "type": "button",
                    "action_id": "dse_repo_confirm",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Confirm"},
                    # Redundant with the block_id, but survives a click whose
                    # `block_id` comes back empty; the handler accepts both sources.
                    "value": work_item_id,
                },
            ],
        },
    ]


class SlackCommentBackend:
    """Implements `dse_contracts.mutable_comment.CommentBackend`. `surface_ref`
    may carry `blocks` (Slack-specific) — when present, the message is
    posted/edited with Block Kit (e.g. approval buttons); otherwise, plain
    text. The shared contract (body: str) stays intact."""

    def __init__(self, client: _SlackClientLike):
        self._client = client

    def post(self, surface_ref: dict, body: str) -> str:
        channel = surface_ref["channel"]
        blocks = surface_ref.get("blocks")
        kwargs = {"channel": channel, "text": body}
        if blocks:
            kwargs["blocks"] = blocks
        # F1(a): a mensagem do bot vive DENTRO da conversa do item — reply na
        # thread, nunca raiz nova. Mensagem-raiz foi o que fantasmou o "main"
        # e o Approve (ingest 1611/1612): resposta humana em thread que a
        # correlação desconhece vira tarefa nova, por construção.
        if surface_ref.get("thread_ts"):
            kwargs["thread_ts"] = surface_ref["thread_ts"]
        resp = self._client.chat_postMessage(**kwargs)
        ts = resp["ts"]
        return json.dumps({"channel": channel, "ts": ts})

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        ref = json.loads(comment_ref)
        blocks = surface_ref.get("blocks")
        kwargs = {"channel": ref["channel"], "ts": ref["ts"], "text": body}
        if blocks:
            kwargs["blocks"] = blocks
        self._client.chat_update(**kwargs)


@dataclass
class FakeSlackClient:
    """In-memory fixture used in the tests (documented — this is not the real
    API). Records every post/update so the tests can assert
    'exactly 1 post + N updates, never N posts'."""

    _next_ts: float = 1000.0
    messages: dict[str, str] = field(default_factory=dict)  # ts -> text (current state)
    #: ts -> thread_ts com que a mensagem foi postada (None = raiz de canal).
    #: O harness precisa disto para simular a semântica REAL do Slack: responder
    #: a uma mensagem threaded carrega o thread_ts da raiz; responder a uma
    #: mensagem de canal cria thread nova com thread_ts = ts dela — exatamente a
    #: bifurcação que fantasmou o "main" e o Approve (ingest 1611/1612).
    message_threads: dict[str, str | None] = field(default_factory=dict)
    post_calls: list[dict] = field(default_factory=list)
    update_calls: list[dict] = field(default_factory=list)
    ephemeral_calls: list[dict] = field(default_factory=list)
    threads: dict[str, list[dict]] = field(default_factory=dict)  # "channel:ts" -> replies
    replies_calls: list[dict] = field(default_factory=list)
    views_open_calls: list[dict] = field(default_factory=list)

    def chat_postMessage(
        self, *, channel: str, text: str, blocks: list | None = None,
        thread_ts: str | None = None,
    ) -> dict:
        self._next_ts += 1
        ts = f"{self._next_ts:.6f}"
        self.messages[ts] = text
        self.message_threads[ts] = thread_ts
        self.post_calls.append({"channel": channel, "text": text, "ts": ts,
                                "blocks": blocks, "thread_ts": thread_ts})
        return {"ok": True, "channel": channel, "ts": ts}

    def chat_update(self, *, channel: str, ts: str, text: str, blocks: list | None = None) -> dict:
        if ts not in self.messages:
            raise KeyError(f"chat_update on a nonexistent ts: {ts}")
        self.messages[ts] = text
        self.update_calls.append({"channel": channel, "text": text, "ts": ts, "blocks": blocks})
        return {"ok": True, "channel": channel, "ts": ts}

    def chat_postEphemeral(self, *, channel: str, user: str, text: str) -> dict:
        """Notice visible to a single user only (repo selector feedback).
        Deliberately OUTSIDE `messages`/`post_calls`: it is not the mutable
        status message, so it must not count towards the 'exactly 1 post'
        invariant."""
        self.ephemeral_calls.append({"channel": channel, "user": user, "text": text})
        return {"ok": True}

    def views_open(self, *, trigger_id: str, view: dict) -> dict:
        """Modal de direcionamento do Retry (A6). O fake só registra — o que os
        testes pinam é O QUE foi aberto (callback_id/private_metadata), nunca a
        renderização do Slack."""
        self.views_open_calls.append({"trigger_id": trigger_id, "view": view})
        return {"ok": True}

    def conversations_replies(self, *, channel: str, ts: str) -> dict:
        """Reads a whole thread — the ONE call that re-reads messages, used by
        the reply reconciler (/internal/reconcile) and by nothing else. Tests
        seed `threads["<channel>:<ts>"]` with the message list Slack would
        return, root message first, exactly as the real API shapes it."""
        self.replies_calls.append({"channel": channel, "ts": ts})
        return {"ok": True, "messages": list(self.threads.get(f"{channel}:{ts}", []))}


#: Socket timeout handed to `WebClient` (slack_sdk's own default is 30s).
#: `_notify_ephemeral` runs inside the `/slack/interactions` COROUTINE, so every
#: second this call blocks is a second the uvicorn event loop answers nothing —
#: not `/slack/events` (Slack gives up at 3s and redelivers), not `/health` (the
#: liveness probe eventually restarts the pod). The retry sleeps are handled by
#: the deadline below; this bounds the wait that no budget can see.
HTTP_TIMEOUT_S = 10


def build_real_slack_client(bot_token: str, *, deadline: float):
    """Builds the real `slack_sdk.WebClient`, wrapped in
    `RateLimitedSlackClient` so a 429 backs off instead of failing the caller.

    `deadline` (an absolute `time.monotonic()` reading) is required: it is the
    moment the CALLER stops waiting — the reconciler CronJob's request timeout,
    the orchestrator's 8s status-comment timeout, or `now` for the ephemeral
    notice that nobody is waiting on. Every call made through the returned client
    shares it, which is what keeps a sweep of 50 threads from spending 50
    separate retry budgets.

    The import lives in here (not at module top level) so that
    `FakeSlackClient`/the tests do not require `slack_sdk` to be installed in
    environments that only run offline tests — even though, in practice,
    `slack_sdk` is a declared dependency in pyproject."""
    from slack_sdk import WebClient

    return RateLimitedSlackClient(
        WebClient(token=bot_token, timeout=HTTP_TIMEOUT_S), deadline=deadline
    )
