"""Feed Maven privado: o sandbox precisa autenticar, sem vazar o token.

Medido 2026-08-18 no `fintexinc/calculation-engine-service`: o `pom.xml`
declara um segundo repositório (`fintexincorporated`, Azure Artifacts) e o
módulo `domain` depende de um artefato que só existe lá. No sandbox a
resolução volta **403 Forbidden** e NENHUM comando de build passa — nem
`compile`. O DSE gastou US$ 7 em 8 turnos de Coder tentando consertar com
código um problema que não é de código.

O `settings.xml` que o driver escreve hoje só tem `<proxies>`. Falta o
`<servers>`, que é o único canal que o resolver do Maven respeita para
credencial — a mesma razão pela qual o `<proxies>` teve de existir (variável
de ambiente e `-D` são ignorados pelo resolver).

Duas exigências que este teste fixa:
  - o `id` do `<server>` tem que casar com o `id` do `<repository>` do POM do
    cliente; qualquer outro nome é ignorado em silêncio pelo Maven;
  - a credencial NÃO pode aparecer no manifesto do Pod. `kubectl get pod -o
    yaml` é legível por qualquer um com acesso de leitura ao namespace, e o
    token ficaria gravado no objeto. Ele viaja por stdin do `exec`, como o
    settings.xml já viaja.
"""
from __future__ import annotations

import json

from sandbox_runtime.driver import SandboxProvisionRequest
from sandbox_runtime.k8s_driver import build_pod_manifest, maven_proxy_settings_xml

_PROXY = "http://dse-egress-proxy:8806"


def test_without_a_credential_the_settings_are_exactly_what_they_were():
    """Rede de segurança: tenant sem feed privado não muda nada."""
    xml = maven_proxy_settings_xml(_PROXY)
    assert "<proxies>" in xml and "dse-egress-https" in xml
    assert "<servers>" not in xml, "sem credencial não se inventa bloco de servers"


def test_the_credential_lands_under_the_repository_id_from_the_pom():
    xml = maven_proxy_settings_xml(
        _PROXY, feed_id="fintexincorporated", feed_username="dse", feed_token="segredo",
    )
    assert "<servers>" in xml
    assert "<id>fintexincorporated</id>" in xml, (
        "o id do <server> tem que ser o mesmo do <repository> do POM do cliente "
        "— com outro nome o Maven ignora a credencial sem avisar"
    )
    assert "<username>dse</username>" in xml
    assert "<password>segredo</password>" in xml
    # o bloco antigo continua inteiro
    assert "<proxies>" in xml and "dse-egress-https" in xml


def test_a_token_with_xml_characters_does_not_break_the_document():
    """PAT é gerado pelo Azure e pode conter qualquer coisa; um `&` cru
    quebraria o settings.xml e o Maven falharia com erro de parse — que seria
    diagnosticado como outra coisa completamente."""
    xml = maven_proxy_settings_xml(
        _PROXY, feed_id="f", feed_username="u", feed_token="a&b<c>d",
    )
    assert "a&amp;b&lt;c&gt;d" in xml
    assert "a&b<c>d" not in xml


def test_the_token_never_reaches_the_pod_manifest():
    """`kubectl get pod -o yaml` é leitura banal no namespace; credencial no
    manifesto é credencial publicada."""
    request = SandboxProvisionRequest(
        work_item_id="wi-feed", tenant_id="fintex-poc", branch="dse/wi-feed",
        workspace_path="/workspace", checkpoint_path="/checkpoint.git",
        repo="acme/repo", base_branch="main",
    )
    manifesto = json.dumps(build_pod_manifest(request))
    assert "MAVEN_FEED_TOKEN" not in manifesto, (
        "o token viaja por stdin do exec, nunca pelo spec do Pod"
    )
