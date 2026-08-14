"""Bot single-tenant pega o token no endpoint DO TENANT, não no compartilhado.

`RealTeamsClient` nasceu na Fase 4, quando bot multi-tenant era o default: o
token vinha de `login.microsoftonline.com/botframework.com/...`, que é o
endpoint do fluxo multi-tenant. A Microsoft encerrou a criação de bots
multi-tenant em 2025-07-31, então o `dse-teams-bot` (criado em 2026-08-14) é
**single-tenant** — e nesse modo o token só é emitido pelo endpoint do próprio
tenant. Mantido o comportamento antigo quando não há tenant configurado, para
não quebrar um registro multi-tenant preexistente.
"""
from __future__ import annotations

from adapter_teams.backend import RealTeamsClient

_TENANT = "cf573d45-5d1d-4bb0-8e1d-332a21c59a3d"


def test_a_tenant_scoped_client_asks_the_tenant_endpoint():
    client = RealTeamsClient("app-id", "app-password", tenant_id=_TENANT)
    assert client.token_url == (
        f"https://login.microsoftonline.com/{_TENANT}/oauth2/v2.0/token"
    ), "bot single-tenant no endpoint compartilhado recebe 401 no primeiro POST"


def test_without_a_tenant_it_keeps_the_shared_endpoint():
    client = RealTeamsClient("app-id", "app-password")
    assert client.token_url == (
        "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
    )


def test_the_scope_is_the_connector_audience_in_both_modes():
    """O escopo NÃO muda com o tipo do app: o público do token continua sendo a
    API do connector; o que muda é quem emite."""
    for client in (RealTeamsClient("a", "b"), RealTeamsClient("a", "b", tenant_id=_TENANT)):
        assert client._SCOPE == "https://api.botframework.com/.default"
