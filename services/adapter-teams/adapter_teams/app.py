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
    admit_work_item,
    correlate,
    get_connection,
    record_signal_event,
    resolve_tenant,
    sanitize_content,
    verify_teams_signature,
)
from pydantic import BaseModel

from . import events
from .backend import TeamsCommentBackend, build_real_teams_client
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


def _handle_conversation_event(conv_event, *, principal: str, tenant_id: str) -> dict:
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
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        try:
            work_item_id = admit_work_item(
                conv_event,
                tenant_id=tenant_id,
                source="teams",
                channel=conv_id,
                requester_principal=principal,
                sanitized_content=sanitized,
                conn=conn,
            )
        except AdmissionBlocked:
            return {"ok": True, "path": "blocked_kill_switch"}

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
    user_id, display = events.actor_of(activity)
    principal = resolve_principal("teams", user_id, display)
    tenant_id = _resolve_tenant_for(activity)
    conv_event = events.build_conversation_event(activity, resolved_principal=principal)
    return _handle_conversation_event(conv_event, principal=principal, tenant_id=tenant_id)


class StatusCommentRequest(BaseModel):
    work_item_id: str
    conversation_id: str
    service_url: str
    body: str
    actor: str  # resolved principal of who/what triggered it (e.g. "system:orchestrator")


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

    comment_ref = writer.upsert(
        req.work_item_id,
        {"conversation_id": req.conversation_id, "service_url": service_url},
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
