"""A credencial dos serviços de apoio — gerada, traduzida, nunca guardada.

UMA senha por sandbox/preview (`$DSE_SERVICE_PASSWORD`): substituída nos env
dos sidecars, exportada ao container principal, e morta junto com o Pod (o
dado vive em emptyDir — girar a senha no rebuild é coerente porque o banco
renasce vazio). Nunca no repositório, nunca na PR, e no preview nunca no
manifest set (lá ela viaja por Secret + secretKeyRef).

A TRADUÇÃO é o detalhe que importa: `$DSE_SERVICE_PASSWORD` vira
`$(DSE_SERVICE_PASSWORD)` — a expansão de env dependente do KUBELET — e a
variável é definida antes de quem a referencia. É o que resolve o caso
substring (`postgresql://u:$DSE_SERVICE_PASSWORD@localhost:5432/db`), que um
`valueFrom.secretKeyRef` sozinho não resolve: secretKeyRef só substitui o
valor INTEIRO de uma variável.

Compartilhado por sandbox (k8s_driver) e preview (argocd) — uma implementação,
dois consumidores, como `build_credentials`.
"""
from __future__ import annotations

import secrets
import string

from dse_validation.config import SERVICE_PASSWORD_TOKEN

#: Alfanumérico puro: dispensa URL-encode (a senha entra em URLs de conexão
#: escritas pelo repo) e dispensa escape em YAML e em shell.
_ALPHABET = string.ascii_letters + string.digits
_LENGTH = 32

#: A forma que o kubelet expande. Referência: dependent env vars — `$(VAR)` é
#: substituído pelo valor de uma variável definida ANTES na lista.
KUBELET_EXPANSION = "$(DSE_SERVICE_PASSWORD)"


def generate_service_password() -> str:
    """Uma senha nova, por provisão. Efêmera por desenho."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def translate_service_password(value: str) -> str:
    """Traduz o token do manifesto para a expansão do kubelet."""
    return value.replace(SERVICE_PASSWORD_TOKEN, KUBELET_EXPANSION)


def references_service_password(value: str) -> bool:
    return SERVICE_PASSWORD_TOKEN in value
