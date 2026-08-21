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

from dse_contracts.surface import STAGE_FOR_STATUS as _STAGE_FOR_STATUS  # noqa: F401
from dse_contracts.surface import STAGES as _STAGES  # noqa: F401
from dse_contracts.surface import progress_line as _progress_line

from .ratelimit import RateLimitedSlackClient

#: rc.90 — a barra de etapas; rc.111 — ela mora em `dse_contracts.surface`, com
#: o Teams lendo a MESMA fonte. Cópia por adapter é o defeito que a regra do
#: `core.hooksPath` pagou três vezes; aqui seria pior, porque duas superfícies
#: mostrando etapas diferentes para o mesmo item não dá erro em lugar nenhum.


class _SlackClientLike(Protocol):
    def chat_postMessage(self, *, channel: str, text: str, blocks: list | None = ...) -> dict: ...
    def chat_update(self, *, channel: str, ts: str, text: str, blocks: list | None = ...) -> dict: ...
    def chat_postEphemeral(self, *, channel: str, user: str, text: str) -> dict: ...
    def conversations_replies(self, *, channel: str, ts: str) -> dict: ...
    def views_open(self, *, trigger_id: str, view: dict) -> dict: ...


def status_blocks(body: str, *, status: str = "", repo: str | None = None) -> list[dict]:
    """rc.90 — o layout de TODA mensagem de status (pedido literal do operador
    ao ver dois irmãos de fan-out dividindo a thread sem se identificar):

        [context]  repo, em texto pequeno       (só quando conhecido)
        [section]  o corpo humanizado de sempre
        [context]  a barra de etapas            (só em status em voo)
        [actions]  Details sempre; Approve/Reject SÓ no gate

    Os action_id/value são os marcadores que `parse_slack_approval` lê (C1);
    o Details é desviado ANTES do fallthrough de veredito, então pode viver em
    qualquer mensagem sem risco de um clique virar aprovação."""
    blocks: list[dict] = []
    if repo:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": f"`{_escape(repo)}`"}]})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
    progress = _progress_line(status)
    if progress:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": progress}]})
    elements: list[dict] = []
    if status == "awaiting_plan_approval":
        elements += [
            {"type": "button", "action_id": "dse_plan_approve", "style": "primary",
             "text": {"type": "plain_text", "text": "Approve"}, "value": "approve"},
            {"type": "button", "action_id": "dse_plan_reject", "style": "danger",
             "text": {"type": "plain_text", "text": "Reject"}, "value": "reject:re_plan"},
        ]
    # Sem estilo, de propósito: quem decide tem cor, quem só mostra não —
    # cor igual convidaria o clique errado.
    elements.append({"type": "button", "action_id": "dse_plan_details",
                     "text": {"type": "plain_text", "text": "Details"}, "value": "details"})
    blocks.append({"type": "actions", "block_id": "dse_plan_approval",
                   "elements": elements})
    return blocks


def approval_blocks(body: str) -> list[dict]:
    """Compat (Phase B / report 07): o gate é hoje um caso do layout único."""
    return status_blocks(body, status="awaiting_plan_approval", repo=None)


#: Limites do Slack: 3.000 chars por bloco `section`, 100 blocos por view. Os
#: cortes abaixo ficam MUITO abaixo disso de propósito — um modal que rola sem
#: fim não é mais legível que a mensagem que ele veio substituir.
_MAX_ITEMS = 25
_SECTION_CHARS = 2900
_TRUNCATED = "\n• _…truncated_"


def _bullets(items: list) -> str:
    """Bullets, cortados — e SEMPRE dizendo que foram cortados.

    O corte ficava depois do marcador "…and N more", então um único item
    gigante empurrava o marcador para fora e o plano aparecia truncado sem
    nenhum sinal disso. Para uma tela cuja razão de existir é "o humano aprova
    o que não pode ler", truncar em silêncio é a versão discreta do mesmo
    problema."""
    if isinstance(items, str):  # linha antiga no banco: string onde se espera lista
        items = [items]
    shown = [_escape(str(i)) for i in items[:_MAX_ITEMS]]
    omitted = max(0, len(items) - len(shown))
    marker = f"\n• _…and {omitted} more_" if omitted else ""
    body = "\n".join(f"• {i}" for i in shown)
    if len(body) + len(marker) > _SECTION_CHARS:
        body = body[: _SECTION_CHARS - len(marker) - len(_TRUNCATED)] + _TRUNCATED
    return body + marker


def _escape(text: str) -> str:
    """Neutraliza a sintaxe de markup do Slack no texto que veio do MODELO.

    O plano é saída de LLM sobre a descrição do requester e o repositório do
    cliente. O Slack renderiza `<url|rótulo>` como link clicável dentro de uma
    `section` — inclusive em modal. Um passo do plano contendo
    `<https://evil.example/|Approve here>` viraria um link com rótulo
    arbitrário NA TELA que o aprovador abre para decidir. É o primeiro sink do
    repositório que renderiza saída de modelo como markup do Slack."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plan_details_view(
    work_item_id: str, plan: dict | None, *, effective_risk: str | None = None,
    repo: str | None = None, siblings: list[dict] | None = None,
    outcome: dict | None = None,
) -> dict:
    """O plano, legível, num modal.

    Existe porque a mensagem do gate é uma frase e dois botões: o humano
    aprovava um plano que não podia ler. O conteúdo vem do `work_items.plan`
    (JSONB), que já está gravado quando a mensagem é postada.

    É SÓ LEITURA — sem `submit`, sem `callback_id` de veredito. O botão Close do
    Slack fecha a view no cliente e nada chega ao servidor; não há submissão a
    rotear, e é essa ausência que impede o modal de virar mais um caminho para
    aprovar por acidente.

    `plan=None` é estado possível de verdade: o gate pode ser re-renderizado por
    um lembrete depois de um `continue_as_new`, e nada garante que a linha tenha
    plano. O modal diz o que sabe em vez de estourar — derrubar a única via de
    aprovação para mostrar um detalhe seria a pior troca possível."""
    blocks: list[dict] = []
    # rc.90: num fan-out multi-repo o plano é POR repo — o modal diz de qual
    # repo é ESTE plano, em texto pequeno, antes de qualquer outra coisa.
    if repo:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"`{_escape(str(repo))[:200]}`"}]})
    if not plan:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": "_The plan is not available for this item._"}})
    else:
        # O risco EFETIVO (coluna `work_items.risk_class`), não o declarado
        # pelo Planner dentro do plano. `policy.classify_risk` só escala PARA
        # CIMA: um plano que declara `low` e toca `.github/workflows/` é `high`
        # de verdade, e é por isso que o gate existe. Mostrar o declarado aqui
        # seria a tela que o humano abre PARA DECIDIR argumentando na direção
        # de aprovar — e no mesmo commit em que a mensagem passou a dizer o
        # efetivo.
        # Cortado como todo o resto: `risk_class` é `str` livre no contrato e
        # vem do Planner. Sem teto, um valor patológico estoura o limite de 3k
        # do bloco e o Slack recusa a view inteira — o modal simplesmente não
        # abre, e o retry nunca funciona.
        risk = str(effective_risk or plan.get("risk_class") or "unknown")[:80]
        # rc.89: o header mostra a ESTIMATIVA do Planner — nunca mais o
        # "Diff budget", que era a constante 400 do contrato (teto de um gate
        # desativado) sendo lida como previsão. Plano sem estimativa (todo o
        # histórico) não mostra nada sobre tamanho; `isinstance` porque o valor
        # vem de JSONB e tipo não é garantia.
        est = plan.get("estimated_lines")
        header = f"*Risk:* `{risk}`"
        if isinstance(est, int) and est > 0:
            header += f"    *Estimated diff:* ~`{est}` lines _(planner's estimate)_"
        if plan.get("no_code_change"):
            header += "\n_This plan declares NO code change._"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": header}})
        for title, key in (
            ("Steps", "steps"),
            ("Files it expects to touch", "expected_files"),
            ("Paths it must not touch", "forbidden_paths"),
        ):
            items = plan.get(key) or []
            if items:
                blocks.append({"type": "divider"})
                blocks.append({"type": "section", "text": {"type": "mrkdwn",
                               "text": f"*{title}*\n{_bullets(items)}"}})
        if plan.get("test_plan"):
            blocks.append({"type": "divider"})
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": f"*Test plan*\n{_escape(str(plan['test_plan']))[:_SECTION_CHARS]}"}})
    # rc.90: os irmãos do grupo — o aprovador vê o CONJUNTO (um plano por
    # repo) sem caçar mensagens na thread. Cortado como todo o resto.
    if siblings:
        linhas = []
        for s in siblings[:_MAX_ITEMS]:
            linhas.append(
                f"• `{_escape(str(s.get('repo') or '?'))[:120]}` — "
                f"`{_escape(str(s.get('id') or '?'))[:20]}…` · "
                f"{_escape(str(s.get('status') or '?'))[:40]} · "
                f"risk {_escape(str(s.get('risk_class') or '?'))[:20]}"
            )
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": ("*Sibling plans* _(this change spans multiple "
                                "repositories — each has its own plan)_\n"
                                + "\n".join(linhas))[:_SECTION_CHARS]}})
    # Item terminado: o DESFECHO, com o erro inteiro. O canal publica esse
    # texto truncado (o "Show less" corta no meio da frase que diagnostica), e
    # o modal é o único lugar onde ele cabe sem poluir a thread.
    if outcome:
        status = _escape(str(outcome.get("status") or "?"))[:40]
        erro = str(outcome.get("last_error") or "").strip()
        corpo = f"*Outcome*\nFinal status: `{status}`"
        if erro:
            corpo += f"\n```{_escape(erro)[:_SECTION_CHARS - 120]}```"
        blocks.append({"type": "divider"})
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": corpo[:_SECTION_CHARS]}})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"Work item `{work_item_id}`"}]})
    return {
        "type": "modal",
        "callback_id": "dse_plan_details",
        "title": {"type": "plain_text", "text": "Plan"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }


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
        # `blocks` SEMPRE presente (vazio quando o status não tem): o
        # chat.update do Slack PRESERVA os blocos antigos quando o argumento é
        # omitido — sem isto, os botões de um prompt decidido sobreviviam a
        # todas as transições seguintes, clicáveis para sempre.
        self._client.chat_update(
            channel=ref["channel"], ts=ref["ts"], text=body,
            blocks=surface_ref.get("blocks") or [],
        )


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
        """Modal. O fake só REGISTRA — o que os testes pinam é O QUE foi aberto
        (o conteúdo da view), nunca a renderização do Slack."""
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
