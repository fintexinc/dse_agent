"""Validação do JWT que o Bot Connector manda em cada requisição.

O adapter nasceu (Fase 4) falando o HMAC de *outgoing webhook*, que era o
análogo direto do Slack. Um outgoing webhook, porém, não tem identidade de
app: não posta depois nem edita mensagem, e o DSE é assíncrono do começo ao
fim (posta na entrada, edita pelos 40 minutos seguintes). Bot registrado —
que é o que o `dse-teams-bot` é — autentica com `Authorization: Bearer <jwt>`.

A doc da Microsoft é explícita: implementar PARTE das verificações deixa o bot
aberto a ataques que o fazem entregar o próprio token. As sete estão aqui, e o
corpus de forjados em `tests/test_jwt_auth.py` prova cada uma por nome.

Desenho copiado de `dse_platform.sso.OIDCVerifier` (mesmo problema, mesma
solução): JWKS em cache, refetch quando o `kid` é desconhecido — que é o sinal
de rotação de chave. Sem o refetch a falha é total e silenciosa: todo mundo é
recusado até alguém reiniciar o pod.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Callable

import jwt

from ingest_gateway.security import SignatureCheck

logger = logging.getLogger("adapter_teams.jwt_auth")

#: Documento estático (a doc autoriza fixar): dele sai o `jwks_uri`.
BOT_FRAMEWORK_OPENID_METADATA = (
    "https://login.botframework.com/v1/.well-known/openidconfiguration"
)
#: Emissor de TODO token que o connector manda para o bot.
BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
#: Tolerância de relógio do padrão.
CLOCK_SKEW_SECONDS = 300
#: A doc manda atualizar o cache pelo menos uma vez por dia — chaves novas
#: podem entrar a qualquer momento.
JWKS_MAX_AGE_SECONDS = 24 * 60 * 60


def _fetch_json(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - URL fixa
        return json.loads(resp.read().decode())


def default_jwks_fetcher() -> dict:
    """Metadata → `jwks_uri` → chaves. Duas requisições porque o `jwks_uri` é
    dado pelo metadata; fixar a segunda URL seria adivinhar."""
    metadata = _fetch_json(BOT_FRAMEWORK_OPENID_METADATA)
    return _fetch_json(metadata["jwks_uri"])


class BotFrameworkJwtVerifier:
    """Verifica o Bearer de entrada e devolve o mesmo `SignatureCheck` das
    verificações irmãs (`ingest_gateway.security`), para o adapter continuar
    traduzindo em 401 + auditoria num lugar só.

    `jwks` permite injetar as chaves (testes, e um primeiro carregamento
    síncrono se alguém quiser); `jwks_fetcher` é o que busca de verdade.
    """

    def __init__(
        self,
        *,
        app_id: str,
        jwks: dict | None = None,
        jwks_fetcher: Callable[[], dict] | None = None,
        clock_skew_seconds: int = CLOCK_SKEW_SECONDS,
    ) -> None:
        self._app_id = app_id
        self._fetcher = jwks_fetcher or default_jwks_fetcher
        self._skew = clock_skew_seconds
        self._keys: dict[str, Any] = {}
        self._endorsements: dict[str, list[str]] = {}
        self._loaded_at = 0.0
        if jwks is not None:
            self._load(jwks)

    # -- chaves ---------------------------------------------------------
    def _load(self, jwks: dict) -> None:
        keys, endorsements = {}, {}
        for entry in jwks.get("keys", []):
            kid = entry.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK.from_dict(entry)
            except Exception:  # noqa: BLE001 - uma chave torta não invalida o resto
                logger.warning("JWKS entry inválida (kid=%s) ignorada", kid)
                continue
            # `endorsements` é extensão do Bot Framework ao JWK padrão: diz
            # quais canais aquela chave está autorizada a assinar.
            endorsements[kid] = list(entry.get("endorsements") or [])
        self._keys, self._endorsements = keys, endorsements
        self._loaded_at = time.time()

    def _refresh(self) -> bool:
        """Best-effort: connector inalcançável não pode levantar daqui — o
        chamador recusa o token de qualquer forma."""
        try:
            self._load(self._fetcher())
            return True
        except Exception:  # noqa: BLE001
            logger.warning("não consegui atualizar o JWKS do Bot Framework", exc_info=True)
            return False

    def _signing_key(self, kid: str | None):
        if kid and kid in self._keys and (time.time() - self._loaded_at) < JWKS_MAX_AGE_SECONDS:
            return self._keys[kid]
        # `kid` desconhecido (ou cache velho) = sinal de rotação: refetch UMA vez.
        if self._refresh() and kid:
            return self._keys.get(kid)
        return None

    # -- verificação ----------------------------------------------------
    def verify(self, *, authorization_header: str | None, activity: dict) -> SignatureCheck:
        if not self._app_id:
            # Fail-closed: sem credencial configurada nada entra. O contrário
            # abriria o endpoint exatamente quando alguém erra o Secret.
            return SignatureCheck(False, "missing_app_id")
        if not authorization_header:
            return SignatureCheck(False, "missing_authorization_header")
        parts = authorization_header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            return SignatureCheck(False, "malformed_authorization_header")
        token = parts[1].strip()

        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            return SignatureCheck(False, "malformed_token")
        kid = header.get("kid")
        signing = self._signing_key(kid)
        if signing is None:
            return SignatureCheck(False, "unknown_signing_key")

        try:
            claims = jwt.decode(
                token,
                signing.key,
                # Lista fechada: sem isto, `alg: none` (ou HS256 com a chave
                # pública como segredo) passaria.
                algorithms=["RS256"],
                audience=self._app_id,
                issuer=BOT_FRAMEWORK_ISSUER,
                leeway=self._skew,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            return SignatureCheck(False, "token_expired")
        except jwt.ImmatureSignatureError:
            return SignatureCheck(False, "token_not_yet_valid")
        except jwt.InvalidAudienceError:
            return SignatureCheck(False, "wrong_audience")
        except jwt.InvalidIssuerError:
            return SignatureCheck(False, "wrong_issuer")
        except jwt.InvalidSignatureError:
            return SignatureCheck(False, "signature_invalid")
        except jwt.InvalidTokenError as exc:
            logger.info("token recusado: %s", exc)
            return SignatureCheck(False, "invalid_token")

        # A claim `serviceUrl` amarra o token ao DESTINO. Sem conferir, uma
        # Activity forjada aponta a nossa saída para o servidor do atacante —
        # levando junto o bearer que usamos para responder.
        claimed = str(claims.get("serviceUrl") or "").rstrip("/")
        actual = str(activity.get("serviceUrl") or "").rstrip("/")
        if claimed and claimed != actual:
            return SignatureCheck(False, "service_url_mismatch")

        # Endosso: a chave que assinou precisa cobrir o canal da Activity.
        channel_id = str(activity.get("channelId") or "")
        if channel_id:
            endorsed = self._endorsements.get(kid or "", [])
            if endorsed and channel_id not in endorsed:
                return SignatureCheck(False, "channel_not_endorsed")

        return SignatureCheck(True, "ok")
