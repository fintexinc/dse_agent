"""WSA-E3-T1/T2 — Slack adapter: inbound (Events API + Interactivity) and
outbound (a single status message, edited in-place). 100% stateless adapter:
no state lives in the process — everything (comment_ref, kill switch,
allowlist, work_items) is read from/written to the shared Postgres on every
request.

Inbound pipeline, in order (the WSA-E2 "4 defenses"):
  1. verify_slack_signature (HMAC + replay window)          -> 401 on failure
  2. content_snapshot frozen from the payload itself (TOCTOU) -> automatic
  3. sanitize_content (invisible unicode + secret redaction)
  4. idempotency: deterministic event_id -> dedup in admit_work_item/
     record_signal_event via a UNIQUE constraint
after that: correlate() decides Path A (new_task) vs Path B (signal) vs
unauthorized (steering allowlist).
"""
from __future__ import annotations

import json
import logging
import time

from dse_audit import emit as audit_emit
from dse_contracts import mutable_comment
from dse_contracts.repos import TENANT_REPOS_SQL
from dse_identity import resolve_principal
from fastapi import FastAPI, HTTPException, Request
from ingest_gateway import (
    AdmissionBlocked,
    NonTaskAdmissionRefused,
    admit_work_item,
    recorded_work_item_id,
    correlate,
    get_connection,
    pending_reply_work_items,
    record_signal_event,
    resolve_tenant,
    resolve_repo,
    sanitize_content,
    verify_slack_signature,
)
from pydantic import BaseModel

from .backend import (
    SlackCommentBackend,
    status_blocks,
    plan_details_view,
    how_to_test_view,
    build_real_slack_client,
    repo_select_blocks,
)
from .comment_store import SURFACE, PgCommentStateStore
from .config import get_slack_bot_token, get_slack_signing_secret, get_tenant_id
from .events import (
    build_event_from_app_mention,
    build_event_from_block_action,
    build_event_from_thread_message,
    build_repo_select_signal_event,
    parse_slack_approval,
)
from .ratelimit import SlackRateLimited

logger = logging.getLogger("adapter_slack")

app = FastAPI(title="dse-adapter-slack")

# How long each endpoint may keep ITS OWN caller waiting on Slack, counted from
# the start of the request. These are the callers' limits, not Slack's — the
# whole point of `adapter_slack.ratelimit` taking a deadline is that only the
# call site knows them.
#
# `/internal/reconcile` is invoked by the reply-reconciler CronJob, which abandons
# the request after 120s (infra/helm/dse/templates/reply-reconciler.yaml). The
# deadline stops the sweep from STARTING another thread, not from finishing the one
# it is in, and one thread is a single `conversations.replies` bounded by
# `backend.HTTP_TIMEOUT_S`. So the worst case is 60s of budget (the listing query
# and the per-item ingest writes included) plus that last call, which lands inside
# the 120s.
RECONCILE_BUDGET_S = 60.0

# `/internal/status-comment` is called by the orchestrator best-effort with an 8s
# HTTP timeout (services/orchestrator .../local_activities.py). Waiting LONGER
# than the caller will is how the "exactly 1 status message per task" invariant
# broke: the orchestrator gave up at 8s, `MutableCommentWriter` never reached
# `save_ref`, and the next transition found no ref and posted a SECOND message.
# Small enough to fit inside the 8s together with the post itself; large enough
# that Slack's short per-channel hints are still absorbed.
STATUS_COMMENT_BUDGET_S = 3.0


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "adapter-slack"}


def _reject(reason: str, *, surface: str) -> None:
    audit_emit(
        actor="system:adapter-slack",
        action="signature_rejected",
        tenant_id=get_tenant_id(),
        details={"reason": reason, "surface": surface},
    )
    raise HTTPException(status_code=401, detail=f"signature_verification_failed:{reason}")


def _resolve_tenant_for(team_id: str | None) -> str:
    """WSA-E1-T5 — resolves the tenant from the Slack workspace (`team_id`)
    via `tenant_platform_bindings`. A missing binding falls back to
    `DSE_TENANT_ID` with a warning audit row (documented single-tenant
    fallback)."""
    conn = get_connection()
    try:
        rt = resolve_tenant(conn, platform="slack", binding_key=team_id)
        conn.commit()
        return rt.tenant_id
    finally:
        conn.close()


def _distinct_repos_for_tenant(conn, tenant_id: str) -> list[str]:
    """Distinct repos of the tenant, for the human picker.

    This used to say it "mirrors the source that resolve_repo Rung 4/5 deemed
    ambiguous" and carried its own copy of the query. It stopped mirroring
    anything the day the router's copy was fixed to include `repo_profiles`,
    and nothing could have noticed. It now asks the shared question."""
    with conn.cursor() as cur:
        cur.execute(TENANT_REPOS_SQL, {"t": tenant_id})
        return [r[0] for r in cur.fetchall()]


def _base_branch_for_repo(conn, tenant_id: str, repo: str) -> str:
    """base_branch from the binding of the chosen repo (the ambiguous repo did
    not carry one). Defaults to 'main' (resolve_repo Rung 1 convention)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT base_branch FROM repo_bindings "
            "WHERE tenant_id = %s AND repo = %s AND base_branch IS NOT NULL LIMIT 1",
            (tenant_id, repo),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else "main"


def _stage_for_action(action_id: str) -> str | None:
    """O stage da decisão one-shot, derivado do PRÓPRIO botão (determinístico).
    A mensagem de status é UMA por item (ts eterno), então a chave do consumo
    é (work_item_id, stage) — nunca o ts."""
    # ALLOWLIST, não prefixo. Com `startswith("dse_plan_")` qualquer botão novo
    # da família herdava o consumo one-shot do veredito — foi o que quase
    # aconteceu com o Details, e um `dse_plan_explain` futuro cairia igual sem
    # nenhum teste falhar. Botão que DECIDE se declara aqui.
    if action_id in ("dse_plan_approve", "dse_plan_reject"):
        return "awaiting_plan_approval"
    return None


def _consume_verdict(conn, work_item_id: str, stage: str,
                     principal: str) -> tuple[bool, str | None, str | None]:
    """Consumo one-shot ATÔMICO da decisão (item 3). True = este clique é o
    primeiro e a decisão é dele. False = já consumida — devolve (por_quem,
    às_que_horas) para o ephemeral do clicador atrasado. O INSERT vive na
    MESMA transação do record_signal_event: ou o veredito entra com o consumo,
    ou nada entra."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO verdict_consumptions (work_item_id, stage, consumed_by) "
            "VALUES (%s,%s,%s) ON CONFLICT (work_item_id, stage) DO NOTHING "
            "RETURNING consumed_by",
            (work_item_id, stage, principal),
        )
        if cur.fetchone():
            return True, None, None
        cur.execute(
            "SELECT consumed_by, to_char(consumed_at AT TIME ZONE 'UTC', 'HH24:MI') "
            "FROM verdict_consumptions WHERE work_item_id=%s AND stage=%s",
            (work_item_id, stage),
        )
        prev = cur.fetchone()
    return False, (prev[0] if prev else None), (prev[1] if prev else None)


#: Item terminal não é candidato. `correlate` recusa match terminal e trata a
#: mensagem como tarefa nova (`correlate.py`); esta busca não recusava, e o
#: fallback por thread pegava o item MAIS RECENTE — que numa thread com um item
#: já concluído ao lado de um no gate mostraria o plano ERRADO na tela de
#: decisão. Leitura errada num gate é pior que leitura nenhuma.
_TERMINAL_STATUSES = ("done", "failed", "cancelled", "escalated")


def _test_guide_for_item(work_item_id: str) -> tuple[dict | None, str | None, str | None]:
    """(guia, url, deep_path) do preview do item — o guia nasce no turno do
    deep link e persiste em `wse_previews.test_guide`; {} = sem guia."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT test_guide, url, deep_path FROM wse_previews "
                "WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        return None, None, None
    guia = row[0] if isinstance(row[0], dict) else None
    if not guia or not (guia.get("steps") or guia.get("login")):
        return None, row[1], row[2]
    return guia, row[1], row[2]


def _plan_for_message(
    tenant_id: str, channel: str, message: dict
) -> tuple[str | None, dict | None, str | None, str | None, list[dict]]:
    """O item, o plano e o risco EFETIVO da mensagem clicada — leitura pura.

    Escopada por `tenant_id` e `source`, como TODA leitura de `work_items`
    neste repositório (`correlate.py`, `_distinct_repos_for_tenant`). O tenant
    já está resolvido no handler; não usá-lo seria abrir a única exceção não
    documentada da regra.

    `source_ref @> %s::jsonb` e não `jsonb_exists(...)`: o índice de
    `source_ref` é GIN `jsonb_ops` (0001_foundation.sql), que serve `@>` e NÃO
    serve `source_ref->>'channel' = %s`. Com o operador errado cada clique
    varria a tabela inteira — duas vezes, quando o primeiro ramo não casava.

    A chave é a MESMA do clique de veredito (`{channel, bot_ts}`): o prompt
    pertence a UM item, e a thread não desambigua quando irmãos de um fan-out a
    compartilham. Fallback por `thread_ts` para prompts anteriores ao registro
    do `bot_ts`, com o mais recente vencendo — o mesmo desempate do resto do
    adapter."""
    ts = message.get("ts")
    thread_ts = message.get("thread_ts") or ts
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # O filtro de terminal vale só no ramo AMBÍGUO. `bot_ts` endereça
            # UMA mensagem — o item dela é o item dela, terminado ou não, e
            # recusar aí é o que produzia "I could not find the task" num
            # clique perfeitamente correlacionado (medido 2026-08-18). Já o
            # `thread_ts` pode casar vários itens e desempata pelo mais
            # recente: ali um item concluído ao lado de um no gate mostraria o
            # plano ERRADO na tela de decisão, que é o risco original.
            for ref, exclui_terminal in (
                ({"channel": channel, "bot_ts": [ts]} if ts else None, False),
                ({"channel": channel, "thread_ts": thread_ts} if thread_ts else None, True),
            ):
                if ref is None:
                    continue
                cur.execute(
                    "SELECT id, plan, risk_class, repo, group_id, status, last_error "
                    "FROM work_items "
                    "WHERE tenant_id = %s AND source = 'slack' "
                    "AND source_ref @> %s::jsonb "
                    "AND (NOT %s OR status NOT IN %s) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (tenant_id, json.dumps(ref), exclui_terminal, _TERMINAL_STATUSES),
                )
                row = cur.fetchone()
                if row:
                    # rc.90: irmãos do grupo (plano é POR repo — o aprovador
                    # precisa ver o conjunto). Padrão COALESCE(group_id, id)
                    # de live_sibling_preview_namespace.
                    siblings: list[dict] = []
                    if row[4]:
                        cur.execute(
                            "SELECT sib.id, sib.repo, sib.status, sib.risk_class "
                            "FROM work_items me JOIN work_items sib "
                            "ON COALESCE(sib.group_id, sib.id) = COALESCE(me.group_id, me.id) "
                            "AND sib.id <> me.id WHERE me.id = %s "
                            "ORDER BY sib.created_at",
                            (row[0],),
                        )
                        siblings = [
                            {"id": s[0], "repo": s[1], "status": s[2], "risk_class": s[3]}
                            for s in cur.fetchall()
                        ]
                    # `status`/`last_error` viajam junto porque o modal de um
                    # item terminal mostra o DESFECHO, e o erro completo só
                    # existe aqui: o canal o publica truncado.
                    outcome = (
                        {"status": row[5], "last_error": row[6]}
                        if row[5] in _TERMINAL_STATUSES else None
                    )
                    return row[0], row[1], row[2], row[3], siblings, outcome
    finally:
        conn.close()
    return None, None, None, None, [], None


def _rearm_verdict(work_item_id: str, stage: str) -> None:
    """Re-arma a decisão quando os botões daquele stage são RENDERIZADOS de
    novo — um re-parque do mesmo item é uma decisão nova (pin do re-arm)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM verdict_consumptions WHERE work_item_id=%s AND stage=%s",
                (work_item_id, stage),
            )
        conn.commit()
    finally:
        conn.close()


def _ack_text_for(action_id: str, user_id: str) -> str:
    hhmm = time.strftime("%H:%M", time.gmtime())
    who = f"<@{user_id}>"
    if action_id == "dse_plan_approve":
        return f"✅ Approved by {who} at {hhmm} (UTC)"
    if action_id == "dse_plan_reject":
        return f"🚫 Rejected by {who} at {hhmm} (UTC) — replanning"
    return f"✔️ Recorded by {who} at {hhmm} (UTC)"


def _ack_update(channel: str, message_ts: str, text: str) -> None:
    """Item 3(a): a decisão fica VISÍVEL na própria mensagem — botões fora,
    decisor e hora dentro. Best-effort e sem espera de throttle (mesma regra
    do _notify_ephemeral: isto roda na coroutine de /slack/interactions). A
    próxima transição do workflow reescreve a mensagem por cima — o ack é a
    ponte honesta até lá."""
    try:
        build_real_slack_client(
            get_slack_bot_token(), deadline=time.monotonic()
        ).chat_update(
            channel=channel, ts=message_ts, text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
    except Exception:  # noqa: BLE001 — o signal já está gravado; o ack nunca o desfaz
        logger.warning("ack chat_update failed", exc_info=True)


def _finish_verdict_click(result: dict, *, channel: str, user_id: str,
                          message_ts: str | None, ack_text: str) -> None:
    """O fecho de todo clique de veredito: ack in-place no sucesso, ephemeral
    'já resolvido' no duplicado. Nada aqui altera o resultado — só o torna
    visível."""
    if result.get("path") == "already_resolved":
        by, at = result.get("by") or "?", result.get("at") or "?"
        _notify_ephemeral(
            channel, user_id, f"⏳ Already resolved by {by} at {at} (UTC)."
        )
    elif result.get("path") == "signal" and message_ts:
        _ack_update(channel, message_ts, ack_text)
    elif result.get("path") in ("refused_non_task", "not_correlated") and message_ts:
        # Item 4 (síncrono): clique num item que JÁ TERMINOU. A falha aparece
        # NA MENSAGEM clicada — o ephemeral genérico ("não encontrei a
        # tarefa") aponta para o lado errado: ela existe, ela acabou. Zero
        # signal nasceu (o caminho de refusal não grava nada).
        _ack_update(
            channel, message_ts,
            "⚠️ Could not apply: the task in this conversation is no longer "
            "active (it finished or was cancelled).",
        )


def _handle_conversation_event(conv_event, *, principal: str, tenant_id: str,
                               extra_payload: dict | None = None,
                               signal_only: bool = False,
                               bot_message_ts: str | None = None,
                               consume_stage: str | None = None) -> dict:
    """`bot_message_ts`: para cliques de botão — o ts da mensagem do bot onde
    o botão vive. Correlaciona PRIMEIRO por `{channel, bot_ts}` (F1(b): o
    prompt pertence a UM item, registrado no source_ref na hora do post);
    thread compartilhada entre irmãos não desambigua e o mais novo não pode
    roubar o Approve do mais velho. Miss (prompt pré-fix) cai no caminho
    normal por thread — comportamento antigo preservado."""
    channel = conv_event.source_ref["channel"]
    sanitized = sanitize_content(conv_event.content_snapshot)

    conn = get_connection()
    try:
        # Recovery sweeps re-read whole threads, so a task that is genuinely
        # waiting meets the same messages on every cycle. Recording dedupes on
        # `event_id`, but only after correlating and auditing — on Jira that
        # turned one stuck ticket into thousands of `signal_duplicate_ignored`
        # rows. Nothing below can change an outcome already reached.
        prior = recorded_work_item_id(conn, conv_event.event_id)
        if prior is not None:
            return {"ok": True, "path": "already_ingested", "work_item_id": prior}

        result = None
        if bot_message_ts:
            result = correlate(
                conn, tenant_id=tenant_id, event=conv_event,
                requester_principal=principal,
                correlation_ref={"channel": channel, "bot_ts": [bot_message_ts]},
            )
            if result.kind == "new_task":
                result = None  # prompt pré-fix sem bot_ts registrado → thread
        if result is None:
            result = correlate(conn, tenant_id=tenant_id, event=conv_event, requester_principal=principal)


        # `signal_only` is the reconciler's leash (/internal/reconcile): that
        # caller recovers REPLIES to an existing task and must never manufacture
        # work. Without it, a thread that stopped correlating — the item raced
        # into a terminal status, or its source_ref does not match — would fall
        # into the Path A branch below and admit ONE NEW TASK PER MESSAGE in the
        # thread, turning a recovery sweep into a task storm. The webhook path
        # leaves this off: there, a message that correlates to nothing genuinely
        # is a new task.
        if signal_only and result.kind != "signal":
            conn.commit()
            return {"ok": True, "path": "not_correlated"}

        if result.kind == "signal":
            # Item 3(b): consumo one-shot NA BORDA. O workflow consome a flag
            # do lado dele, mas um segundo signal re-arma a flag DEPOIS do
            # consumo — e o próximo parque do mesmo item se auto-resolveria
            # com o veredito velho (classe wi_8edaef39). Mesma transação do
            # record: ou os dois entram, ou nenhum.
            if consume_stage:
                fresh, by, at = _consume_verdict(
                    conn, result.work_item_id, consume_stage, principal
                )
                if not fresh:
                    conn.commit()
                    return {"ok": True, "path": "already_resolved",
                            "work_item_id": result.work_item_id, "by": by, "at": at}
            # `recorded` is False when the event_id already existed (dedup). The
            # reconciler needs the distinction to count/audit only what it truly
            # recovered — re-reading a thread every cycle must not inflate the
            # trail with replies that arrived normally.
            recorded = record_signal_event(
                conv_event,
                tenant_id=tenant_id,
                channel=channel,
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                extra_payload=extra_payload,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id,
                    "recorded": recorded}

        # Path A: new_task — C2 (report 07): resolves the repo through the
        # cascade (explicit override in the text → channel binding → tenant
        # default). With no resolution, repo=None and the clarification gate
        # asks (it never guesses). The text used is the SANITIZED one (never
        # the raw one).
        repo, base_branch, repo_candidates = resolve_repo(
            conn, tenant_id=tenant_id, platform="slack",
            signals={"text": sanitized, "channel": channel},
        )
        try:
            work_item_id = admit_work_item(
                conv_event,
                tenant_id=tenant_id,
                source="slack",
                channel=channel,
                repo=repo,
                base_branch=base_branch,
                repo_candidates=repo_candidates,
                requester_principal=principal,
                sanitized_content=sanitized,
                conn=conn,
            )
        except AdmissionBlocked:
            return {"ok": True, "path": "blocked_kill_switch"}
        except NonTaskAdmissionRefused as refusal:
            # F2 (fantasmas 1611/1612): resposta/clique numa conversa que a
            # correlação desconhece NUNCA vira tarefa. Orienta na hora, no
            # canal, com o candidato mais recente do mesmo canal se houver.
            hint = ""
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source_ref->>'thread_ts' FROM work_items "
                    "WHERE tenant_id = %s AND source = 'slack' "
                    "AND source_ref->>'channel' = %s "
                    "AND status NOT IN ('done','failed','escalated','blocked','cancelled') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (tenant_id, channel),
                )
                cand = cur.fetchone()
            if cand:
                hint = f" The most recent task here is `{cand[0][:15]}…` (thread {cand[1]})."
            audit_emit(
                actor=principal,
                action="non_task_admission_refused",
                tenant_id=tenant_id,
                details={"kind": refusal.kind, "channel": channel,
                         "event_id": conv_event.event_id},
                conn=conn,
            )
            conn.commit()
            _notify_ephemeral(
                channel, conv_event.actor.platform_user_id,
                "I could not find the task for this conversation — reply in "
                "the original task's thread (where it was created)." + hint,
            )
            return {"ok": True, "path": "refused_non_task"}

        if result.provenance_work_item_id:
            audit_emit(
                actor=principal,
                action="work_item_provenance_link",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"previous_work_item_id": result.provenance_work_item_id},
            )

        return {"ok": True, "path": "new_task", "work_item_id": work_item_id}
    finally:
        conn.close()


@app.post("/slack/events")
async def slack_events(request: Request) -> dict:
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    check = verify_slack_signature(
        signing_secret=get_slack_signing_secret(),
        timestamp_header=timestamp,
        body=body,
        signature_header=signature,
    )
    if not check.verified:
        _reject(check.reason, surface="slack_events")

    payload = json.loads(body)

    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    if payload.get("type") != "event_callback":
        return {"ok": True}

    event = payload["event"]
    event_type = event.get("type")
    user_id = event.get("user")
    if not user_id:
        return {"ok": True}  # events with no user (e.g. bot_message) are ignored in Phase 1

    principal = resolve_principal("slack", user_id)
    tenant_id = _resolve_tenant_for(payload.get("team_id"))

    if event_type == "app_mention":
        conv_event = build_event_from_app_mention(event, resolved_principal=principal)
    elif event_type == "message" and not event.get("subtype") and event.get("thread_ts"):
        conv_event = build_event_from_thread_message(event, resolved_principal=principal)
    else:
        return {"ok": True}  # event type not covered in Phase 1

    return _handle_conversation_event(conv_event, principal=principal, tenant_id=tenant_id)


def _selected_repo_from_state(payload: dict, block_id: str, work_item_id: str) -> str | None:
    """Repo chosen in the static_select, read from the message `state`.

    On the confirm-button click the `action` describes the BUTTON — it does not
    carry `selected_option`. Slack ships along the current state of the
    message's stateful elements in `state.values`, indexed by
    block_id -> action_id (documented for MESSAGE `block_actions` since 2020,
    not just for modals). That is where the choice comes from — which is what
    makes the select+confirm pair possible without keeping a pending selection
    server-side. A button is stateless: it does not show up here and therefore
    does not pollute the read.

    `state` is opportunistic, not durable: it may arrive absent, without the
    block, or with `selected_option: null` (deselection, or a message re-render
    that wipes the choice). All three cases return None — it never guesses a
    repo; the caller warns the human instead of failing mute.

    The safety net for a click with no `block_id` matches on the
    `:<work_item_id>` SUFFIX instead of scanning everything: if the message
    ever carries two selectors, the choice is never paired with the wrong work
    item."""
    values = (payload.get("state") or {}).get("values") or {}
    if block_id in values:
        blocks = [values[block_id]]
    else:
        blocks = [v for k, v in values.items() if work_item_id and k.endswith(f":{work_item_id}")]
    for block in blocks:
        selected = ((block or {}).get("dse_repo_select") or {}).get("selected_option") or {}
        if selected.get("value"):
            return selected["value"]
    return None


def _notify_ephemeral(channel: str, user_id: str, text: str) -> None:
    """Notice visible only to whoever clicked (`chat.postEphemeral`).

    Without this, a Confirm with no selection — or with the selection lost
    because the message was re-rendered — fails in ABSOLUTE silence: the human
    clicks, nothing happens, and there is no hint whatsoever as to why.
    Best-effort on purpose: a Slack failure here must not take down the
    interaction nor undo the signal that was already recorded.

    It NEVER WAITS on a throttle. The deadline is `now`, so a `ratelimited`
    answer raises immediately instead of sleeping. Two reasons, and either alone
    would be enough: `chat.postEphemeral` allows roughly one call per second per
    channel, so a burst of Confirm clicks throttles routinely; and this runs
    inside the `/slack/interactions` COROUTINE, where the blocking `time.sleep`
    of a retry parks the uvicorn event loop — `/slack/events` deliveries stall
    (Slack times out at 3s and redelivers), `/internal/status-comment` stalls,
    and `/health` stops answering until the liveness probe restarts the pod.
    Retrying a notice for a human who already moved on is not worth any of
    that."""
    try:
        build_real_slack_client(
            get_slack_bot_token(), deadline=time.monotonic()
        ).chat_postEphemeral(channel=channel, user=user_id, text=text)
    except Exception:  # noqa: BLE001 — feedback is ancillary, never fatal
        logger.warning("chat_postEphemeral failed (repo selector feedback)", exc_info=True)


@app.post("/slack/interactions")
async def slack_interactions(request: Request) -> dict:
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    check = verify_slack_signature(
        signing_secret=get_slack_signing_secret(),
        timestamp_header=timestamp,
        body=body,
        signature_header=signature,
    )
    if not check.verified:
        _reject(check.reason, surface="slack_interactions")

    form = await request.form()
    payload = json.loads(form["payload"])

    if payload.get("type") != "block_actions":
        return {"ok": True}

    user_id = payload["user"]["id"]
    principal = resolve_principal("slack", user_id)
    team_id = (payload.get("team") or {}).get("id") or payload.get("user", {}).get("team_id")
    tenant_id = _resolve_tenant_for(team_id)
    action = payload["actions"][0]

    # TWO-step repo selection. Slack fires `block_actions` as soon as the
    # static_select is picked; treating that as the decision would make the
    # first click irreversible — getting the repo wrong would fire an agent turn
    # against the wrong repo. So the select merely stages (the choice sits in
    # the message `state`) and only the button promotes it to a signal.
    # DETAILS do plano — leitura, e por isso ANTES do fallthrough de veredito.
    #
    # O desvio explícito não é estilo: sem ele o clique passaria por três
    # máquinas que transformam "quero ler" em "aprovei" — `parse_slack_approval`
    # devolve `approved` para qualquer action_id fora dos tokens de rejeição, o
    # veredito one-shot é CONSUMIDO, e o ack reescreve a mensagem sumindo com os
    # botões. Escolher outro nome de action_id resolveria as duas últimas e não
    # a primeira. As três estão pinadas em `test_plan_details_modal.py`.
    if action.get("action_id") == "dse_plan_details":
        channel = payload["channel"]["id"]
        message = payload.get("message") or {}
        work_item_id, plan, risk, item_repo, siblings, outcome = _plan_for_message(
            tenant_id, channel, message)
        if not work_item_id:
            _notify_ephemeral(channel, user_id,
                              "I could not find the task for this message.")
            return {"ok": True, "path": "plan_details_no_item"}
        try:
            # deadline = AGORA, igual ao `_notify_ephemeral` logo acima e pelo
            # mesmo motivo, que está escrito lá: isto roda DENTRO da corrotina
            # de `/slack/interactions`, e o `time.sleep` de um retry paralisa o
            # event loop do uvicorn — `/slack/events` para de responder, o Slack
            # expira em 3s e REDISTRIBUI, e o `/health` cala até a liveness
            # reiniciar o pod. Um budget de 2.5s "para caber na validade do
            # trigger_id" comprava a janela do modal com a disponibilidade do
            # adapter inteiro. Sem absorção de throttle o modal simplesmente não
            # abre, e o humano clica de novo — nada foi gravado.
            build_real_slack_client(
                get_slack_bot_token(), deadline=time.monotonic()
            ).views_open(
                trigger_id=payload.get("trigger_id", ""),
                view=plan_details_view(work_item_id, plan, effective_risk=risk,
                                       repo=item_repo, siblings=siblings,
                                       outcome=outcome),
            )
        except Exception:  # noqa: BLE001 — sem modal, o gate continua clicável
            logger.warning("views_open failed (plan details)", exc_info=True)
            _notify_ephemeral(channel, user_id,
                              "I could not open the plan — please try again.")
            return {"ok": True, "path": "plan_details_failed"}
        return {"ok": True, "path": "plan_details_opened"}

    if action.get("action_id") == "dse_how_to_test":
        # Leitura, como o Details — e desviada ANTES do fallthrough pelas
        # MESMAS três razões (parse_slack_approval aprovaria, o veredito
        # one-shot seria consumido, o ack apagaria os botões).
        channel = payload["channel"]["id"]
        message = payload.get("message") or {}
        work_item_id, _plan, _risk, item_repo, _siblings, _outcome = _plan_for_message(
            tenant_id, channel, message)
        if not work_item_id:
            _notify_ephemeral(channel, user_id,
                              "I could not find the task for this message.")
            return {"ok": True, "path": "how_to_test_no_item"}
        guia, url, deep_path = _test_guide_for_item(work_item_id)
        if not guia:
            _notify_ephemeral(
                channel, user_id,
                "There is no test guide for this preview (it may have expired, "
                "or the change needed none).")
            return {"ok": True, "path": "how_to_test_no_guide"}
        try:
            build_real_slack_client(
                get_slack_bot_token(), deadline=time.monotonic()
            ).views_open(
                trigger_id=payload.get("trigger_id", ""),
                view=how_to_test_view(work_item_id, guia, url=url,
                                      deep_path=deep_path, repo=item_repo),
            )
        except Exception:  # noqa: BLE001 — sem modal, a mensagem continua clicável
            logger.warning("views_open failed (how to test)", exc_info=True)
            _notify_ephemeral(channel, user_id,
                              "I could not open the guide — please try again.")
            return {"ok": True, "path": "how_to_test_failed"}
        return {"ok": True, "path": "how_to_test_opened"}

    if action.get("action_id") == "dse_repo_select":
        # No-op ack: an empty 200 keeps the Slack client from flagging the
        # interaction as failed, and nothing is recorded until the Confirm.
        return {"ok": True, "path": "repo_select_staged"}

    # Repo confirmation (ambiguous-repo clarification): this is NOT an approval.
    # Addressed by the work_item_id in the block_id (not by correlation — the
    # status-comment is posted OUTSIDE the thread). The repo+base_branch become
    # the `repo=X branch=Y` marker in the content -> the dispatcher extracts it
    # (C4 regex) -> clarification_answer SIGNAL -> the workflow refills
    # input.repo/base_branch. Identical effect to typing
    # `repo=org/x branch=main` in the thread.
    if action.get("action_id") == "dse_repo_confirm":
        block_id = action.get("block_id", "")
        work_item_id = block_id.split(":", 1)[1] if ":" in block_id else action.get("value", "")
        channel = payload["channel"]["id"]
        repo = _selected_repo_from_state(payload, block_id, work_item_id)
        if not work_item_id or not repo:
            # Confirm with no valid choice: either nothing was selected, or the
            # message `state` was lost. Nothing is recorded — but the human
            # NEEDS to know, otherwise they keep clicking a mute button with no
            # idea why.
            _notify_ephemeral(
                channel, user_id,
                "Pick a repository from the menu, then hit *Confirm*.",
            )
            return {"ok": True, "path": "repo_select_noop"}
        conn = get_connection()
        try:
            # The repo arrives via the message `state`; confining it to the
            # tenant's list guarantees that only a repo WE offered can become the
            # target of an agent turn — the handler never accepts a repo it did
            # not offer.
            if repo not in _distinct_repos_for_tenant(conn, tenant_id):
                audit_emit(actor=principal, action="repo_select_rejected_unknown_repo",
                           tenant_id=tenant_id,
                           details={"work_item_id": work_item_id, "repo": repo})
                _notify_ephemeral(
                    channel, user_id,
                    f"`{repo}` isn't a repository registered in this workspace.",
                )
                return {"ok": True, "path": "unknown_repo"}
            content = f"repo={repo} branch={_base_branch_for_repo(conn, tenant_id, repo)}"
            conv_event = build_repo_select_signal_event(
                payload, action, resolved_principal=principal, content=content
            )
            record_signal_event(
                conv_event, tenant_id=tenant_id, channel=channel,
                work_item_id=work_item_id, sanitized_content=content, conn=conn,
            )
            conn.commit()  # persists the ingest_event for the dispatcher to drain
        except AdmissionBlocked:
            return {"ok": True, "path": "blocked_kill_switch"}
        finally:
            conn.close()
        # Acknowledges receipt to whoever clicked. Beyond courtesy, this is what
        # prevents repeated clicks from someone who got no visual feedback —
        # every extra Confirm would become another clarification_answer on the
        # same work item.
        _notify_ephemeral(channel, user_id, f"✅ Using *{repo}* — starting work now.")
        return {"ok": True, "path": "repo_selected", "work_item_id": work_item_id, "repo": repo}

    conv_event = build_event_from_block_action(payload, resolved_principal=principal)

    # C1 (report 07): derives the button's verdict/route into DETERMINISTIC
    # markers — without this the dispatcher defaults to `approved` and a
    # "reject" would silently approve the plan (gate security bug).
    verdict, route = parse_slack_approval(action.get("action_id", ""), action.get("value", ""))
    extra_payload: dict = {"approval_verdict": verdict}
    if route:
        extra_payload["approval_route"] = route

    result = _handle_conversation_event(
        conv_event, principal=principal, tenant_id=tenant_id, extra_payload=extra_payload,
        bot_message_ts=(payload.get("message") or {}).get("ts"),
        consume_stage=_stage_for_action(action.get("action_id", "")),
    )
    _finish_verdict_click(
        result, channel=payload["channel"]["id"], user_id=user_id,
        message_ts=(payload.get("message") or {}).get("ts"),
        ack_text=_ack_text_for(action.get("action_id", ""), user_id),
    )
    return result


class StatusCommentRequest(BaseModel):
    work_item_id: str
    channel: str
    body: str
    actor: str  # resolved principal of who/what triggered the update (e.g. "system:orchestrator")
    status: str | None = None

@app.post("/internal/status-comment")
def upsert_status_comment(req: StatusCommentRequest) -> dict:
    """WSA-E3-T2: exactly 1 status message per WorkItem, edited in-place —
    called by the orchestrator (WS-B) on every relevant state transition.
    Uses the shared `MutableCommentWriter` (dse_contracts).

    Phase B (report 07): on status `awaiting_plan_approval` the message goes
    out with Block Kit (Approve/Reject buttons) — the same mutable message,
    only interactive. The clicks come back via /slack/interactions (verdict via
    C1).

    The Slack client is bounded by `STATUS_COMMENT_BUDGET_S`, which is what keeps
    a throttled post from outliving the orchestrator's 8s timeout and reappearing
    as a SECOND status message on the next transition."""
    client = build_real_slack_client(
        get_slack_bot_token(), deadline=time.monotonic() + STATUS_COMMENT_BUDGET_S
    )
    backend = SlackCommentBackend(client)
    store = PgCommentStateStore()
    writer = mutable_comment.MutableCommentWriter(backend, store, SURFACE)

    surface_ref = {"channel": req.channel}
    # F1(a): resolve a thread ORIGINAL do item — a mensagem vai como reply
    # nela. O source_ref é a fonte (o irmão do fan-out compartilha o do
    # primário, então a conversa é uma só, como o UX sempre pediu).
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # rc.90: `repo` entra no mesmo SELECT — é o texto pequeno que faz
            # dois irmãos de fan-out serem legíveis na MESMA thread (o
            # "implementing" do FE foi lido como o BE parqueado tendo começado
            # sem aprovação, porque nada dizia de quem era a mensagem).
            cur.execute("SELECT source_ref, repo FROM work_items WHERE id = %s",
                        (req.work_item_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    item_source_ref = row[0] if row and isinstance(row[0], dict) else {}
    item_repo = row[1] if row else None
    if item_source_ref.get("thread_ts"):
        surface_ref["thread_ts"] = item_source_ref["thread_ts"]
    # rc.90: TODA mensagem ganha o layout (repo pequeno + corpo + barra de
    # etapas + Details); Approve/Reject continuam exclusivos do gate, dentro
    # de status_blocks.
    surface_ref["blocks"] = status_blocks(req.body, status=req.status or "",
                                          repo=item_repo)
    if req.status == "awaiting_plan_approval":
        # Item 3: renderizar os botões RE-ARMA a decisão deste stage — um
        # re-render legítimo (novo gate no mesmo item) é uma decisão nova.
        _rearm_verdict(req.work_item_id, "awaiting_plan_approval")
    elif req.status == "awaiting_repo_selection":
        # Ambiguous repo: offer a static_select with the tenant's repos. With < 2
        # repos it degrades to plain text (nothing to pick -> just the text
        # question).
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT tenant_id FROM work_items WHERE id = %s", (req.work_item_id,))
                row = cur.fetchone()
            tenant_id = row[0] if row else get_tenant_id()
            repos = _distinct_repos_for_tenant(conn, tenant_id)
            conn.commit()
        finally:
            conn.close()
        if len(repos) >= 2:
            surface_ref["blocks"] = repo_select_blocks(req.work_item_id, repos, req.body)
    comment_ref = writer.upsert(req.work_item_id, surface_ref, req.body)

    # F1(b): todo identificador de conversa que o bot cria é REGISTRADO no
    # source_ref do item (append em `bot_ts`), para o containment do
    # correlate casar. É o que endereça o clique de botão ao item CERTO
    # mesmo com irmãos compartilhando a thread — a thread não desambigua,
    # o ts da mensagem do prompt sim. Guard de containment torna o append
    # idempotente (upsert edita a mesma mensagem em toda transição).
    try:
        posted_ts = json.loads(comment_ref).get("ts")
    except (ValueError, AttributeError):
        posted_ts = None
    if posted_ts:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_items SET source_ref = jsonb_set(source_ref, '{bot_ts}', "
                    "COALESCE(source_ref->'bot_ts', '[]'::jsonb) || to_jsonb(%s::text)) "
                    "WHERE id = %s AND NOT "
                    "(COALESCE(source_ref->'bot_ts', '[]'::jsonb) @> to_jsonb(ARRAY[%s::text]))",
                    (posted_ts, req.work_item_id, posted_ts),
                )
            conn.commit()
        finally:
            conn.close()

    audit_emit(
        actor=req.actor,
        action="status_comment_upserted",
        tenant_id=get_tenant_id(),
        work_item_id=req.work_item_id,
        details={"surface": SURFACE, "channel": req.channel},
    )
    return {"ok": True, "comment_ref": comment_ref}


_RECONCILER_ACTOR = "system:adapter-slack-reconciler"


def _is_recoverable_reply(message: dict, thread_ts: str) -> bool:
    """True for a thread message the WEBHOOK path would have ingested.

    Parity with /slack/events is the entire rule: the reconciler exists to make
    up for a lost delivery, not to widen what counts as an answer. So it drops
    exactly what the webhook drops — messages from the bot itself (`bot_id`, or
    no `user` at all, which is how Slack shapes bot/system messages) and
    anything carrying a `subtype` (channel joins, file shares, message-changed).

    The thread ROOT is dropped on top of that: it is the original task_request,
    already ingested when the task was created. Re-reading it as a reply would
    feed the task its own opening line back as an answer."""
    if message.get("bot_id") or not message.get("user"):
        return False
    if message.get("subtype"):
        return False
    ts = message.get("ts")
    return bool(ts) and ts != thread_ts


def _recover_missed_replies(client, item: dict, *, tenant_id: str) -> int:
    """Re-reads ONE blocked thread and ingests whatever never arrived.

    Every recovered message goes through `_handle_conversation_event` — the very
    function the webhook calls — so sanitization, correlation, the steering gate
    and the `record_signal_event` outbox write are the same code, not a parallel
    copy that will drift.

    Idempotency is free and deliberate: `event_id` is derived from
    platform+thread+message ts, identical to what the webhook would have
    produced, so a reply that DID arrive collides on the UNIQUE constraint and is
    dropped by the existing dedup. That is why re-reading the whole thread on
    every cycle is correct rather than merely tolerable — and why only
    `recorded=True` counts as a recovery.

    Re-reading is the operation the TOCTOU defense (WSA-E2-T2) forbids for
    APPROVALS, and this path never touches one: the caller only ever gets work
    items blocked on a clarification reply, and the events built here are
    `clarification_answer` by construction (`build_event_from_thread_message`)."""
    source_ref = item.get("source_ref") or {}
    channel, thread_ts = source_ref.get("channel"), source_ref.get("thread_ts")
    if not channel or not thread_ts:
        return 0  # nothing to re-read: this item was never anchored to a thread

    messages = client.conversations_replies(channel=channel, ts=thread_ts).get("messages") or []
    recovered = 0
    for message in messages:
        if not _is_recoverable_reply(message, thread_ts):
            continue
        principal = resolve_principal("slack", message["user"])
        conv_event = build_event_from_thread_message(
            {
                "channel": channel,
                "ts": message["ts"],
                "thread_ts": thread_ts,
                "user": message["user"],
                "text": message.get("text", ""),
            },
            resolved_principal=principal,
        )
        result = _handle_conversation_event(
            conv_event, principal=principal, tenant_id=tenant_id, signal_only=True
        )
        if not result.get("recorded"):
            continue  # already ingested, refused by the steering gate, or no longer correlated
        recovered += 1
        # The trail has to say "this came in through recovery, not through a
        # signed webhook" — an event ingested from a re-read message is a
        # different provenance claim, and an auditor must be able to tell them
        # apart without inferring it from timestamps. The ACTOR is the
        # reconciler, not the human: they wrote the reply, they did not trigger
        # the sweep. Who wrote it stays in `author` (and in the event itself).
        audit_emit(
            actor=_RECONCILER_ACTOR,
            action="reply_recovered",
            tenant_id=tenant_id,
            work_item_id=result["work_item_id"],
            details={
                "surface": "slack",
                "channel": channel,
                "thread_ts": thread_ts,
                "message_ts": message["ts"],
                "event_id": conv_event.event_id,
                "author": principal,
                "blocked_status": item.get("status"),
            },
        )
    return recovered


class ReconcileRequest(BaseModel):
    tenant_id: str | None = None  # defaults to the adapter's tenant (Phase 1: single tenant)
    limit: int = 50  # blast-radius guard: crawl instead of stampeding the Slack API


@app.post("/internal/reconcile")
def reconcile_missed_replies(req: ReconcileRequest | None = None) -> dict:
    """Recovers clarification replies whose delivery to this adapter was lost.

    Observed twice in one afternoon (BD-40, BD-41): a human answers the
    question, the webhook never lands (adapter down, delivery failed), and the
    task sits in `needs_clarification` FOREVER in complete silence — both times
    unblocked by hand with an UPDATE on the database. Delivery is not something
    to keep betting on: for the handful of items blocked waiting on a human,
    this re-reads the thread and ingests what was missed.

    Deliberately NOT recovered: plan approvals. `pending_reply_work_items` only
    returns reply-blocked statuses and excludes `awaiting_plan_approval`,
    because a recovered approval is a decision manufactured from text nobody
    signed — the exact attack the TOCTOU defense exists to stop (post something
    benign, get it approved, edit afterwards). A lost approval stays lost and a
    human re-approves; that is the correct failure mode.

    Best-effort end to end: an unreadable thread (Slack error, deleted channel,
    malformed row) is logged and skipped so it cannot blind the rest of the
    sweep. Nothing here answers 5xx either — a caller on a timer would only
    retry into the same failure, and `ok: False` says more than a stack trace at
    the other end. Same contract as the GitHub adapter's reconciler, so one
    scheduled caller can read both the same way.

    ONE budget for the WHOLE sweep (`RECONCILE_BUDGET_S`), not one per item: the
    Slack client is built once with a single deadline, and the loop below refuses
    to start another thread once that deadline has passed. Without both halves,
    a workspace being throttled turned this endpoint into ~50 minutes of sleeping
    inside a request the CronJob abandons after 120s. Stopping early loses
    nothing: `pending_reply_work_items` rotates through the pending set, so the
    next cycle continues from where this one gave up."""
    tenant_id = (req.tenant_id if req else None) or get_tenant_id()
    limit = req.limit if req else 50
    deadline = time.monotonic() + RECONCILE_BUDGET_S

    try:
        conn = get_connection()
        try:
            items = pending_reply_work_items(
                conn, tenant_id=tenant_id, source="slack", limit=limit
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — a broken sweep must not become a 5xx loop
        logger.exception("reconcile: could not list the work items awaiting a reply")
        return {"ok": False, "checked": 0, "recovered": 0}

    if not items:  # nothing blocked -> no reason to hold a Slack token, let alone call the API
        return {"ok": True, "checked": 0, "recovered": 0}

    try:
        client = build_real_slack_client(get_slack_bot_token(), deadline=deadline)
    except Exception:  # noqa: BLE001
        logger.exception("reconcile: could not build the Slack client")
        return {"ok": False, "checked": 0, "recovered": 0}

    recovered = 0
    checked = 0
    for item in items:
        if time.monotonic() >= deadline:
            # The client would no longer sleep on a throttle, but it would still
            # spend a request per remaining item. Starting no new thread past the
            # deadline is what makes the sweep fit inside the CronJob's 120s.
            logger.warning(
                "reconcile: out of budget after %d/%d items; the next cycle continues",
                checked, len(items),
            )
            break
        checked += 1
        try:
            recovered += _recover_missed_replies(client, item, tenant_id=tenant_id)
        except SlackRateLimited:
            # Slack throttles per method per workspace, so every remaining item
            # faces the same limit on the same `conversations.replies`. Marching
            # on would spend the rest of the request failing identically — and
            # this used to be swallowed by the `except Exception` below, which is
            # how the sweep kept going for 50 throttled items in a row.
            logger.warning(
                "reconcile: slack is throttling this workspace; stopping after %d/%d items",
                checked, len(items),
            )
            break
        except Exception:  # noqa: BLE001 — one bad item must not abort the sweep
            logger.exception(
                "reconcile: could not recover %s; continuing the sweep",
                item.get("work_item_id"),
            )

    return {"ok": True, "checked": checked, "recovered": recovered}
