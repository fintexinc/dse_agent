"""O JWT do connector é validado inteiro — os sete pontos, não alguns.

O adapter nasceu com o HMAC de *outgoing webhook*, que é o análogo do Slack.
Mas outgoing webhook não tem identidade de app: não posta depois nem edita a
mensagem que não criou, então não sustenta o modelo assíncrono do DSE. Bot
registrado autentica com `Authorization: Bearer <jwt>` emitido pelo Bot
Connector, e a doc da Microsoft é explícita: implementar PARTE das verificações
deixa o bot aberto a ataques que fazem ele entregar o próprio token.

Este corpus é a prova executável de cada verificação. Cada teste falha por um
motivo diferente, com nome, porque "rejeitou" não é evidência de "rejeitou pelo
motivo certo" — um verificador que devolve False sempre passaria num teste
frouxo e recusaria produção inteira.

Sem rede: o JWKS é injetado. O refetch por `kid` desconhecido tem teste
próprio, com um fetcher falso.
"""
from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

try:
    from adapter_teams.jwt_auth import BOT_FRAMEWORK_ISSUER, BotFrameworkJwtVerifier
except ImportError:  # vermelho: o módulo ainda não existe — os testes FALHAM,
    # mas a coleção da suite não é interrompida (um ImportError no topo levaria
    # os testes VIZINHOS junto, e aí o vermelho não diz nada).
    BotFrameworkJwtVerifier = None  # type: ignore[assignment]
    BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"

_APP_ID = "753d623e-8212-4c58-9e30-f0601c29e508"
_SERVICE_URL = "https://smba.trafficmanager.net/emea/"


def _key(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256",
                "endorsements": ["msteams"]})
    return private, jwk


def _token(private, kid: str, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": BOT_FRAMEWORK_ISSUER,
        "aud": _APP_ID,
        "nbf": now - 10,
        "exp": now + 600,
        "serviceUrl": _SERVICE_URL,
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid})


def _activity(**overrides) -> dict:
    activity = {"type": "message", "channelId": "msteams",
                "serviceUrl": _SERVICE_URL, "text": "oi"}
    activity.update(overrides)
    return activity


def _build(**kwargs):
    assert BotFrameworkJwtVerifier is not None, (
        "adapter_teams.jwt_auth.BotFrameworkJwtVerifier não existe — o endpoint "
        "só sabe validar o HMAC de outgoing webhook, que não é o esquema de um bot"
    )
    return BotFrameworkJwtVerifier(**kwargs)


@pytest.fixture
def verifier():
    private, jwk = _key("k1")
    return _build(app_id=_APP_ID, jwks={"keys": [jwk]}), private


def _check(verifier, token: str, activity: dict | None = None):
    v, _ = verifier
    return v.verify(authorization_header=f"Bearer {token}",
                    activity=activity if activity is not None else _activity())


def test_a_well_formed_token_from_the_connector_is_accepted(verifier):
    _, private = verifier
    check = _check(verifier, _token(private, "k1"))
    assert check.verified is True, check.reason
    assert check.reason == "ok"


def test_no_authorization_header_is_denied(verifier):
    v, _ = verifier
    check = v.verify(authorization_header=None, activity=_activity())
    assert check.verified is False
    assert check.reason == "missing_authorization_header"


def test_a_non_bearer_scheme_is_denied(verifier):
    v, _ = verifier
    check = v.verify(authorization_header="HMAC abc123", activity=_activity())
    assert check.verified is False
    assert check.reason == "malformed_authorization_header"


def test_a_token_signed_by_another_key_is_denied(verifier):
    """Mesmo `kid`, chave outra: é a forja que um atacante consegue montar
    sozinho, e a única defesa é conferir a assinatura contra o JWKS."""
    other_private, _ = _key("k1")
    check = _check(verifier, _token(other_private, "k1"))
    assert check.verified is False
    assert check.reason == "signature_invalid"


def test_an_unsigned_token_is_denied(verifier):
    """`alg=none` — o ataque clássico contra verificador que confia no header."""
    now = int(time.time())
    unsigned = jwt.encode(
        {"iss": BOT_FRAMEWORK_ISSUER, "aud": _APP_ID, "exp": now + 600,
         "serviceUrl": _SERVICE_URL},
        key="", algorithm="none", headers={"kid": "k1"},
    )
    check = _check(verifier, unsigned)
    assert check.verified is False, "token sem assinatura foi aceito"


def test_a_token_for_another_app_is_denied(verifier):
    """`aud` é o que amarra o token A ESTE bot: sem isso, o token válido de
    qualquer outro bot do Bot Framework abriria o nosso endpoint."""
    _, private = verifier
    check = _check(verifier, _token(private, "k1", aud="outro-app-id"))
    assert check.verified is False
    assert check.reason == "wrong_audience"


def test_a_token_from_another_issuer_is_denied(verifier):
    _, private = verifier
    check = _check(verifier, _token(private, "k1", iss="https://evil.example"))
    assert check.verified is False
    assert check.reason == "wrong_issuer"


def test_an_expired_token_is_denied(verifier):
    _, private = verifier
    now = int(time.time())
    check = _check(verifier, _token(private, "k1", exp=now - 3600, nbf=now - 7200))
    assert check.verified is False
    assert check.reason == "token_expired"


def test_a_token_that_is_not_valid_yet_is_denied(verifier):
    """`nbf` no futuro além da tolerância de 5 min do padrão."""
    _, private = verifier
    now = int(time.time())
    check = _check(verifier, _token(private, "k1", nbf=now + 3600, exp=now + 7200))
    assert check.verified is False
    assert check.reason == "token_not_yet_valid"


def test_a_service_url_that_does_not_match_the_activity_is_denied(verifier):
    """A claim `serviceUrl` amarra o token ao destino: sem conferir, um
    atacante manda uma Activity apontando a NOSSA saída para o servidor dele —
    e é para lá que o status (e o bearer de saída) iria."""
    _, private = verifier
    check = _check(verifier, _token(private, "k1"),
                   _activity(serviceUrl="https://evil.example/"))
    assert check.verified is False
    assert check.reason == "service_url_mismatch"


def test_a_trailing_slash_is_not_a_mismatch(verifier):
    """Normalização deliberada: mesma origem e mesmo caminho, barra a mais."""
    _, private = verifier
    check = _check(verifier, _token(private, "k1", serviceUrl=_SERVICE_URL.rstrip("/")))
    assert check.verified is True, check.reason


def test_a_channel_the_signing_key_does_not_endorse_is_denied():
    """Endosso: a chave que assinou tem que cobrir o canal da Activity. Sem
    isso, um canal comprometido fala pelo `msteams`."""
    private, jwk = _key("k1")
    jwk["endorsements"] = ["webchat"]
    v = _build(app_id=_APP_ID, jwks={"keys": [jwk]})
    check = v.verify(authorization_header=f"Bearer {_token(private, 'k1')}",
                     activity=_activity())
    assert check.verified is False
    assert check.reason == "channel_not_endorsed"


def test_without_an_app_id_everything_is_denied():
    """Fail-closed: adapter sem credencial configurada NEGA. O contrário —
    'sem app id, aceita' — é o modo de falha que abre o endpoint em produção
    exatamente quando alguém erra o Secret."""
    private, jwk = _key("k1")
    v = _build(app_id="", jwks={"keys": [jwk]})
    check = v.verify(authorization_header=f"Bearer {_token(private, 'k1')}",
                     activity=_activity())
    assert check.verified is False
    assert check.reason == "missing_app_id"


def test_an_unknown_kid_refetches_the_jwks_once():
    """Rotação de chave: `kid` desconhecido dispara UM refetch e o token passa.
    Sem isso a falha é total e silenciosa — todo mundo é recusado até alguém
    reiniciar o pod (a lição já documentada em dse_platform/sso.py:123)."""
    private, jwk = _key("k2")
    fetches: list[int] = []

    def fetch_jwks() -> dict:
        fetches.append(1)
        return {"keys": [jwk]}

    v = _build(app_id=_APP_ID, jwks={"keys": []},
                                jwks_fetcher=fetch_jwks)
    check = v.verify(authorization_header=f"Bearer {_token(private, 'k2')}",
                     activity=_activity())
    assert check.verified is True, check.reason
    assert len(fetches) == 1, f"refetch deveria acontecer uma vez: {fetches}"


def test_a_still_unknown_kid_after_the_refetch_is_denied():
    private, _ = _key("k3")
    v = _build(app_id=_APP_ID, jwks={"keys": []},
                                jwks_fetcher=lambda: {"keys": []})
    check = v.verify(authorization_header=f"Bearer {_token(private, 'k3')}",
                     activity=_activity())
    assert check.verified is False
    assert check.reason == "unknown_signing_key"
