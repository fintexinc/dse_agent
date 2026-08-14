"""`/teams/messages` escolhe o esquema pelo header — e nega o que não conhece.

Duas portas convivem: `HMAC …` é o outgoing webhook (o que o adapter sabia
fazer desde a Fase 4) e `Bearer …` é o bot registrado (o único que sustenta o
DSE, porque só ele posta e edita depois). A escolha é pelo prefixo, e o que
não casa com nenhum dos dois não entra.

O ponto delicado: o JWT precisa da Activity JÁ decodificada (a claim
`serviceUrl` é conferida contra o corpo), então o Bearer parseia antes de
verificar. Parsear não é confiar — nada é gravado, nem lido do banco, antes do
veredito da assinatura. Este teste existe para pinar essa ordem.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

import adapter_teams.app as app_module
from adapter_teams.app import app

from .helpers import activity_body

client = TestClient(app)


class _Verifier:
    """Verificador de mentira: registra o que recebeu e devolve o roteiro."""

    def __init__(self, verified: bool, reason: str = "ok"):
        self.calls: list[dict] = []
        self._verified, self._reason = verified, reason

    def verify(self, *, authorization_header, activity):
        self.calls.append({"header": authorization_header, "activity": activity})
        from ingest_gateway.security import SignatureCheck

        return SignatureCheck(self._verified, self._reason)


def test_a_bearer_request_is_verified_as_a_jwt(monkeypatch):
    verifier = _Verifier(False, "wrong_audience")
    monkeypatch.setattr(app_module, "get_jwt_verifier", lambda: verifier)

    resp = client.post("/teams/messages", content=activity_body(),
                       headers={"Authorization": "Bearer forjado.aqui.agora"})

    assert resp.status_code == 401, "Bearer inválido tem que ser recusado"
    assert verifier.calls, "o Bearer não passou pelo verificador de JWT"
    assert verifier.calls[0]["header"].startswith("Bearer ")
    assert verifier.calls[0]["activity"].get("serviceUrl"), (
        "o verificador precisa da Activity decodificada para conferir o serviceUrl"
    )


def test_an_hmac_request_never_touches_the_jwt_verifier(monkeypatch):
    """Compatibilidade: quem usa outgoing webhook continua no caminho antigo."""
    verifier = _Verifier(True)
    monkeypatch.setattr(app_module, "get_jwt_verifier", lambda: verifier)

    resp = client.post("/teams/messages", content=activity_body(),
                       headers={"Authorization": "HMAC deadbeef"})

    assert resp.status_code == 401, "HMAC errado continua recusado pelo verificador antigo"
    assert verifier.calls == [], "requisição HMAC não pode ser validada como JWT"


def test_an_unknown_scheme_is_denied(monkeypatch):
    monkeypatch.setattr(app_module, "get_jwt_verifier", lambda: _Verifier(True))
    resp = client.post("/teams/messages", content=activity_body(),
                       headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 401


def test_a_bearer_over_malformed_json_is_denied_before_anything_else(monkeypatch):
    """Corpo que não é JSON não pode virar 500 nem chegar ao verificador."""
    verifier = _Verifier(True)
    monkeypatch.setattr(app_module, "get_jwt_verifier", lambda: verifier)
    resp = client.post("/teams/messages", content=b"{nao sou json",
                       headers={"Authorization": "Bearer x.y.z"})
    assert resp.status_code == 401
    assert verifier.calls == []
