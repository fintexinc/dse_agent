"""WS-A Microsoft Teams adapter (PROVISIONED, Phase 4) — inbound (Activity ->
ConversationEvent through the 4 defenses) and outbound (a single status message,
edited in-place, via `MutableCommentWriter` with the Teams backend).

100% stateless adapter — same convention as Slack/GitHub/Jira. All
admission/correlation/defense logic comes from `ingest_gateway`; Teams
normalization lives in `adapter_teams.events`.

Inbound pipeline, in order (the "4 defenses" of WSA-E2):
  1. verify_teams_signature (outgoing webhook HMAC)           -> 401 on failure
  2. content_snapshot frozen from the payload itself (TOCTOU) -> automatic in events
  3. sanitize_content (invisible unicode + secret redaction)
  4. idempotency: deterministic event_id -> dedup in admit/record_signal

ACTIVATION guard: while the foundation does not expose `Platform.teams`, the
endpoint verifies the signature (a real defense) and then returns 501
`teams_not_activated` BEFORE any write (avoids violating the platform CHECKs on
work_items/identity_links). Turning Teams on is 1 line in the enum +
activation.sql — with no changes here.
"""
from __future__ import annotations

import json
import logging

from dse_audit import emit as audit_emit
from dse_contracts import mutable_comment
from dse_identity import resolve_principal
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from ingest_gateway import (
    AdmissionBlocked,
    NonTaskAdmissionRefused,
    admit_work_item,
    correlate,
    get_connection,
    is_authorized_to_steer,
    record_signal_event,
    resolve_repo,
    resolve_tenant,
    sanitize_content,
    verify_teams_signature,
)
from pydantic import BaseModel

from . import events
from .backend import TeamsCommentBackend, build_real_teams_client
from .card import plan_details_dialog, refusal_dialog, status_card
from .comment_store import SURFACE, PgCommentStateStore
from .config import (
    get_default_service_url,
    get_teams_app_id,
    get_teams_app_password,
    get_teams_bot_tenant_id,
    get_teams_shared_secret,
    get_tenant_id,
)
from .jwt_auth import BotFrameworkJwtVerifier
from .platform_compat import TeamsNotActivated, is_activated

logger = logging.getLogger("adapter_teams")

#: Verificador de JWT com estado (cache do JWKS) — um por processo. Construir
#: a cada requisição jogaria fora o cache e faria uma busca de chaves por
#: mensagem, que é justamente o que o refetch-por-kid existe para evitar.
_JWT_VERIFIER: BotFrameworkJwtVerifier | None = None


def get_jwt_verifier() -> BotFrameworkJwtVerifier:
    global _JWT_VERIFIER
    if _JWT_VERIFIER is None:
        _JWT_VERIFIER = BotFrameworkJwtVerifier(app_id=get_teams_app_id())
    return _JWT_VERIFIER

app = FastAPI(title="dse-adapter-teams")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "adapter-teams", "activated": is_activated()}


def _reject(reason: str) -> None:
    audit_emit(
        actor="system:adapter-teams",
        action="signature_rejected",
        tenant_id=get_tenant_id(),
        details={"reason": reason, "surface": "teams_webhook"},
    )
    raise HTTPException(status_code=401, detail=f"signature_verification_failed:{reason}")


def _resolve_tenant_for(activity: dict) -> str:
    conn = get_connection()
    try:
        rt = resolve_tenant(conn, platform="teams", binding_key=events.aad_tenant_id(activity))
        conn.commit()
        return rt.tenant_id
    finally:
        conn.close()


def _plan_dialog_for(activity: dict) -> dict:
    """O diálogo do plano — leitura pura, gateada.

    O MESMO gate dos botões (`is_authorized_to_steer`): não é integridade, é
    confidencialidade. O diálogo mostra caminhos reais do repositório do
    cliente e o risco efetivo; entregar isso a um convidado da conversa é
    reconhecimento por um clique."""
    work_item_id = events.task_fetch_work_item(activity)
    if not work_item_id:
        return refusal_dialog("I could not find the task for this message.")

    _user_id, _display = events.actor_of(activity)
    try:
        principal = resolve_principal(platform="teams", platform_user_id=_user_id)
    except Exception:  # noqa: BLE001 — sem identidade não há leitura
        logger.warning("teams: principal unresolved for a plan dialog", exc_info=True)
        return refusal_dialog("I could not identify you for this request.")

    conn = get_connection()
    try:
        tenant_id = resolve_tenant(
            conn, platform="teams", binding_key=events.aad_tenant_id(activity)).tenant_id
        if not is_authorized_to_steer(tenant_id, principal):
            audit_emit(actor=principal, action="plan_details_refused_unauthorized",
                       tenant_id=tenant_id, work_item_id=work_item_id,
                       details={"surface": "teams"})
            conn.commit()
            return refusal_dialog("You are not allowed to read this task's plan.")
        with conn.cursor() as cur:
            # Escopado por tenant E por source, como TODA leitura de
            # `work_items` neste repositório.
            cur.execute(
                "SELECT plan, risk_class, repo FROM work_items "
                "WHERE id = %s AND tenant_id = %s AND source = %s",
                (work_item_id, tenant_id, "teams"),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    if not row:
        return refusal_dialog("I could not find the task for this message.")
    plan, risk, repo = row
    return plan_details_dialog(work_item_id, plan if isinstance(plan, dict) else None,
                               risk=risk, repo=repo)


def _handle_conversation_event(conv_event, *, principal: str, tenant_id: str,
                               extra_payload: dict | None = None) -> dict:
    conv_id = conv_event.source_ref["conversation_id"]
    sanitized = sanitize_content(conv_event.content_snapshot)

    conn = get_connection()
    try:
        result = correlate(conn, tenant_id=tenant_id, event=conv_event, requester_principal=principal)

        if result.kind == "unauthorized":
            conn.commit()
            return {"ok": True, "path": "unauthorized"}

        if result.kind == "signal":
            record_signal_event(
                conv_event,
                tenant_id=tenant_id,
                channel=conv_id,
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
                extra_payload=extra_payload,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        # A cascata de repositório (override no texto → binding do canal →
        # default do tenant), como Slack (app.py:408) e Jira fazem. Sem ela o
        # `repo_bindings` do canal é decorativo e o item nasce sem repo, para
        # um modelo adivinhar depois o que uma linha de banco já respondia. O
        # texto usado é o SANITIZADO, nunca o cru.
        repo, base_branch, repo_candidates = resolve_repo(
            conn, tenant_id=tenant_id, platform="teams",
            signals={"text": sanitized, "channel": conv_id},
        )
        try:
            work_item_id = admit_work_item(
                conv_event,
                tenant_id=tenant_id,
                source="teams",
                channel=conv_id,
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
            # Resposta numa conversa que a correlação desconhece NUNCA vira
            # tarefa — e também não pode virar 500: o connector reentrega erro
            # de servidor, então a mesma mensagem voltaria em loop. Recusa
            # explícita e auditada, como no Slack (app.py:427).
            audit_emit(
                actor=principal,
                action="non_task_admission_refused",
                tenant_id=tenant_id,
                details={"kind": refusal.kind, "channel": conv_id,
                         "event_id": conv_event.event_id},
                conn=conn,
            )
            conn.commit()
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


@app.post("/teams/messages")
async def teams_messages(request: Request):
    body = await request.body()
    authorization = request.headers.get("Authorization")

    # Defesa #1: assinatura. Duas portas, escolhidas pelo PREFIXO do header —
    # `Bearer` é o bot registrado (Bot Connector), `HMAC` é o outgoing webhook
    # da Fase 4. Esquema desconhecido não entra.
    scheme = (authorization or "").split(None, 1)[0].lower() if authorization else ""
    if scheme == "bearer":
        # O JWT exige a Activity decodificada: a claim `serviceUrl` é conferida
        # contra o corpo (é o que impede um forjado redirecionar a NOSSA saída).
        # Parsear não é confiar: nada é gravado nem lido antes do veredito.
        try:
            activity = json.loads(body)
        except ValueError:
            _reject("malformed_json_body")
        check = get_jwt_verifier().verify(
            authorization_header=authorization, activity=activity
        )
        if not check.verified:
            _reject(check.reason)
    else:
        check = verify_teams_signature(
            shared_secret=get_teams_shared_secret(), body=body,
            authorization_header=authorization,
        )
        if not check.verified:
            _reject(check.reason)
        activity = json.loads(body)
    # --- Diálogo (task module) -------------------------------------------
    # Vem DEPOIS da porta de assinatura e ANTES da recusa de não-mensagem: um
    # `task/fetch` é `invoke`, e a recusa abaixo o matava. A resposta é
    # SÍNCRONA — o Teams lê o corpo desta resposta HTTP, e um 200 vazio abre o
    # diálogo em branco.
    if events.is_task_fetch(activity):
        return JSONResponse(status_code=200, content=_plan_dialog_for(activity))

    if activity.get("type") != "message":
        return {"ok": True, "path": "ignored_non_message"}

    # Activation guard BEFORE any write (avoids violating the platform CHECKs).
    # Clean, explanatory failure (P6), with audit (P8).
    if not is_activated():
        audit_emit(
            actor="system:adapter-teams",
            action="teams_inbound_not_activated",
            tenant_id=get_tenant_id(),
            details={"conversation_id": events.conversation_id(activity), "event_id": events.compute_event_id(activity)},
        )
        return JSONResponse(status_code=501, content={"ok": False, "error": str(TeamsNotActivated())})

    # --- Post-activation path (exercised once the foundation exposes Platform.teams) ---
    # Clique de card: vira `approval` com os marcadores DETERMINÍSTICOS que o
    # dispatcher lê. Sem eles o padrão é `approved` — um "Reject" aprovaria o
    # plano em silêncio, que é defeito de segurança do gate, não de UI.
    veredito = events.card_verdict(activity)
    extra_payload: dict | None = None
    if veredito is not None:
        if events.is_details_click(activity):
            # Details existe em TODA mensagem, inclusive fora do gate: se caísse
            # no fallthrough de veredito, um clique curioso aprovaria o plano.
            audit_emit(
                actor="system:adapter-teams",
                action="teams_details_clicked",
                tenant_id=get_tenant_id(),
                details={"conversation_id": events.conversation_id(activity)},
            )
            return {"ok": True, "path": "details"}
        verdict, route = veredito
        extra_payload = {"approval_verdict": verdict}
        if route:
            extra_payload["approval_route"] = route

    user_id, display = events.actor_of(activity)
    principal = resolve_principal("teams", user_id, display)
    tenant_id = _resolve_tenant_for(activity)
    conv_event = events.build_conversation_event(activity, resolved_principal=principal)
    return _handle_conversation_event(conv_event, principal=principal, tenant_id=tenant_id,
                                      extra_payload=extra_payload)


class StatusCommentRequest(BaseModel):
    work_item_id: str
    conversation_id: str
    service_url: str
    body: str
    actor: str  # resolved principal of who/what triggered it (e.g. "system:orchestrator")
    #: Decide a barra de etapas e a existência dos botões do gate. Opcional
    #: porque chamador antigo (ou outro adapter reusando o modelo) continua
    #: mandando só o corpo — e aí a mensagem é a de texto de sempre.
    status: str | None = None


@app.post("/internal/status-comment")
def upsert_status_comment(req: StatusCommentRequest) -> dict:
    """Exactly 1 status message per WorkItem, edited in-place — called by the
    orchestrator (WS-B) on every relevant transition. Uses the SAME
    `MutableCommentWriter` as the other adapters (Teams backend).

    Outbound does NOT depend on the `Platform.teams` enum (the surface is just the
    string "teams") — so it is already fully functional/testable, with
    `FakeTeamsClient`."""
    service_url = req.service_url or get_default_service_url()
    client = build_real_teams_client(get_teams_app_id(), get_teams_app_password(),
                                     get_teams_bot_tenant_id())
    backend = TeamsCommentBackend(client)
    store = PgCommentStateStore()
    writer = mutable_comment.MutableCommentWriter(backend, store, SURFACE)

    # O repo vem do item, como no Slack: é o texto pequeno que faz dois irmãos
    # de fan-out serem legíveis na MESMA conversa.
    item_repo = None
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT repo FROM work_items WHERE id = %s", (req.work_item_id,))
                row = cur.fetchone()
            item_repo = row[0] if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — o card sem repo é pior que status nenhum
        logger.warning("teams: the item's repo is unavailable; the card goes without it",
                       exc_info=True)

    comment_ref = writer.upsert(
        req.work_item_id,
        {"conversation_id": req.conversation_id, "service_url": service_url,
         "card": status_card(req.body, status=req.status or "", repo=item_repo,
                             work_item_id=req.work_item_id)},
        req.body,
    )

    audit_emit(
        actor=req.actor,
        action="status_comment_upserted",
        tenant_id=get_tenant_id(),
        work_item_id=req.work_item_id,
        details={"surface": SURFACE, "conversation_id": req.conversation_id},
    )
    return {"ok": True, "comment_ref": comment_ref}
