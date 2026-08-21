"""Teams outbound — `CommentBackend` (dse_contracts.mutable_comment): the fourth
backend of the SAME `MutableCommentWriter` already used by Slack/GitHub/Jira — so
"exactly 1 status message per WorkItem, edited in-place" comes for free on Teams
(the common logic lives in the shared writer).

Real transport: Bot Framework Connector REST (the same one Azure Bot Service
uses), authenticated with an AAD client_credentials bearer token:
  - post   -> POST {serviceUrl}/v3/conversations/{conversationId}/activities
              (creates the status message; returns the `activity id`)
  - edit   -> PUT  {serviceUrl}/v3/conversations/{conversationId}/activities/{activityId}
              (edits the message in-place)

PROVISIONED: with no app registration / real Teams tenant in this session,
`FakeTeamsClient` replaces the transport in the tests — the `TeamsCommentBackend`
logic (comment_ref serialization, post-vs-edit choice via the writer) is 100% real.
`build_real_teams_client` builds the real HTTP client (via `requests`), exercised
in production after activation.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Protocol


class TeamsClientLike(Protocol):
    def send_activity(self, *, service_url: str, conversation_id: str, text: str,
                      attachments: list[dict] | None = None) -> str:
        """Creates a message activity in the conversation. Returns the `activity id`."""
        ...

    def update_activity(
        self, *, service_url: str, conversation_id: str, activity_id: str, text: str,
        attachments: list[dict] | None = None,
    ) -> None:
        """Edits the existing activity in-place."""
        ...


def _activity_body(text: str, attachments: list[dict] | None) -> dict:
    """O corpo da activity — e a regra que custou três cards para descobrir.

    Medido no connector, mesma conversa: `{"text": ...}` responde 201 com id,
    `{"attachments": [...]}` responde 201 com id, e os DOIS JUNTOS respondem
    202 com corpo vazio. Sem id não há edição, e sem edição a mensagem mutável
    vira uma mensagem nova por transição.

    Com card, o texto vai em `summary`: é o que a notificação do celular lê, e
    não vira uma segunda bolha repetindo o que o card já diz."""
    if attachments:
        return {"type": "message", "summary": text, "attachments": attachments}
    return {"type": "message", "text": text}


def is_editable_ref(comment_ref: str) -> bool:
    """A referência aponta uma activity que dá para EDITAR?

    Referência sem `activity_id` (o 202 do connector não devolve id) não vai
    para o store: guardá-la faria a próxima transição tentar editar uma
    activity que não existe, e o PUT responde 404 — a mensagem pararia de
    atualizar, que é o oposto do que o writer existe para garantir."""
    if not comment_ref:
        return False
    try:
        return bool(json.loads(comment_ref).get("activity_id"))
    except (ValueError, AttributeError):
        return False


def _attachments(surface_ref: dict) -> list[dict] | None:
    """O Adaptive Card que o CHAMADOR renderizou, no mesmo desenho do Slack
    (lá `surface_ref["blocks"]`, aqui `["card"]`): o backend entrega, não monta.
    Ausente = mensagem de texto de sempre."""
    card = surface_ref.get("card")
    return [card] if card else None


class TeamsCommentBackend:
    """Implements `dse_contracts.mutable_comment.CommentBackend`.

    Expected `surface_ref`: `{"conversation_id": ..., "service_url": ...}`.
    The opaque `comment_ref` serializes enough for a future edit
    (conversation_id + activity_id + service_url), same as the other backends.
    """

    def __init__(self, client: TeamsClientLike):
        self._client = client

    def post(self, surface_ref: dict, body: str) -> str:
        service_url = surface_ref["service_url"]
        conversation_id = surface_ref["conversation_id"]
        activity_id = self._client.send_activity(
            service_url=service_url, conversation_id=conversation_id, text=body,
            attachments=_attachments(surface_ref),
        )
        return json.dumps(
            {
                "service_url": service_url,
                "conversation_id": conversation_id,
                "activity_id": activity_id,
            }
        )

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        ref = json.loads(comment_ref)
        self._client.update_activity(
            service_url=ref["service_url"],
            conversation_id=ref["conversation_id"],
            activity_id=ref["activity_id"],
            text=body,
            # O card vem do `surface_ref` da CHAMADA, não do `comment_ref`
            # gravado no post: o gate nasce num edit, e o card dele não existia
            # quando a mensagem foi criada.
            attachments=_attachments(surface_ref),
        )


@dataclass
class FakeTeamsClient:
    """In-memory fixture used in the tests (documented — this is NOT the real API).
    Records every send/update so the tests can assert 'exactly 1 send + N updates,
    never N sends' (the one-message-per-task invariant)."""

    _next_id: int = 1000
    activities: dict[str, str] = field(default_factory=dict)  # activity_id -> current text
    send_calls: list[dict] = field(default_factory=list)
    update_calls: list[dict] = field(default_factory=list)

    def send_activity(self, *, service_url: str, conversation_id: str, text: str,
                      attachments: list[dict] | None = None) -> str:
        self._next_id += 1
        activity_id = f"1{self._next_id}"
        self.activities[activity_id] = text
        self.send_calls.append(
            {"service_url": service_url, "conversation_id": conversation_id, "text": text,
             "activity_id": activity_id, "attachments": attachments}
        )
        return activity_id

    def update_activity(
        self, *, service_url: str, conversation_id: str, activity_id: str, text: str,
        attachments: list[dict] | None = None,
    ) -> None:
        if activity_id not in self.activities:
            raise KeyError(f"update_activity on a nonexistent activity: {activity_id}")
        self.activities[activity_id] = text
        self.update_calls.append(
            {"service_url": service_url, "conversation_id": conversation_id,
             "activity_id": activity_id, "text": text, "attachments": attachments}
        )


class RealTeamsClient:
    """Real Bot Framework Connector client (`requests` transport). Fetches and
    caches the AAD client_credentials bearer token and POSTs/PUTs activities.
    Exercised in production after activation; in the tests it is replaced by
    `FakeTeamsClient`."""

    #: Emissor do fluxo MULTI-tenant. Continua sendo o default só por
    #: compatibilidade: a Microsoft encerrou a criação de bots multi-tenant em
    #: 2025-07-31, e um bot single-tenant pedindo token aqui leva 401.
    _TOKEN_URL = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
    #: O público do token é o connector nos DOIS modos — o que muda é o emissor.
    _SCOPE = "https://api.botframework.com/.default"

    def __init__(self, app_id: str, app_password: str, tenant_id: str = ""):
        self._app_id = app_id
        self._app_password = app_password
        self._tenant_id = tenant_id
        self._token: str | None = None
        self._token_exp: float = 0.0

    @property
    def token_url(self) -> str:
        if self._tenant_id:
            return f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        return self._TOKEN_URL

    def _bearer(self) -> str:
        import requests

        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        resp = requests.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._app_id,
                "client_secret": self._app_password,
                "scope": self._SCOPE,
            },
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_exp = now + int(payload.get("expires_in", 3600))
        return self._token

    def send_activity(self, *, service_url: str, conversation_id: str, text: str,
                      attachments: list[dict] | None = None) -> str:
        import requests

        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"
        corpo = _activity_body(text, attachments)
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {self._bearer()}"},
            json=corpo,
            timeout=15,
        )
        resp.raise_for_status()
        # 202 SEM corpo é a resposta normal quando a activity leva
        # `attachments`: o connector aceita e entrega de forma assíncrona, sem
        # devolver id. Só a mensagem de texto puro responde 200 com {"id":...}.
        # Pedir o id direto estourava DEPOIS de o card já ter saído — e como a
        # referência nunca era gravada, toda transição postava um card novo.
        # Sem id não há edição possível, e mentir um aqui seria pior: o PUT de
        # uma activity inexistente é 404, e a mensagem pararia de atualizar.
        if resp.status_code == 202 or not (resp.text or "").strip():
            return ""
        try:
            return str(resp.json().get("id") or "")
        except ValueError:
            return ""

    def update_activity(
        self, *, service_url: str, conversation_id: str, activity_id: str, text: str,
        attachments: list[dict] | None = None,
    ) -> None:
        import requests

        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities/{activity_id}"
        # `attachments` vai SEMPRE (lista vazia quando não há card): o PUT do
        # connector substitui a activity, então omitir o campo deixaria os
        # botões de um gate já decidido vivos e clicáveis para sempre — o
        # mesmo defeito que o Slack corrigiu mandando `blocks` sempre.
        # `attachments` vai SEMPRE (lista vazia quando não há card) — ver a nota
        # acima; o resto do corpo segue a mesma regra do POST.
        corpo = _activity_body(text, attachments)
        corpo["attachments"] = attachments or []
        resp = requests.put(
            url,
            headers={"Authorization": f"Bearer {self._bearer()}"},
            json=corpo,
            timeout=15,
        )
        resp.raise_for_status()


def build_real_teams_client(app_id: str, app_password: str,
                            tenant_id: str = "") -> RealTeamsClient:
    """`tenant_id` vazio mantém o emissor compartilhado (registro multi-tenant
    preexistente); preenchido, o token vem do tenant — que é o único modo em
    que um bot criado hoje funciona."""
    return RealTeamsClient(app_id, app_password, tenant_id=tenant_id)
