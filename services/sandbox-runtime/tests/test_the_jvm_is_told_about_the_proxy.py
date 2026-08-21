"""Plugin Maven que baixa a PRÓPRIA dependência não passa pelo proxy.

A rede do sandbox é default-deny: a única saída é o egress proxy. O RESOLVEDOR
do Maven é apontado para lá por `<proxies>` no settings.xml (única forma que ele
respeita — ver `build_credentials.maven_proxy_settings_xml`), e por isso baixar
artefato sempre funcionou.

Mas um plugin que faz o próprio HTTP não usa o resolvedor. O spotless não
empacota o formatter do Eclipse: ele abre uma conexão e baixa o jar do jdt. Essa
conexão é `URLConnection` da JVM, que ignora settings.xml e obedece
`-Dhttps.proxyHost`/`-Dhttps.proxyPort` — propriedades que o `MAVEN_OPTS` do Pod
nunca teve (só `-Duser.home=/tmp`). A conexão saía DIRETA, batia na
NetworkPolicy e morria:

    java.io.IOException: Failed to load eclipse jdt formatter:
    java.net.ConnectException: Failed to connect to download.eclipse.org

Medido em wi_2f3b8332 e wi_56aab6b0 (2026-08-21) — o segundo já DEPOIS de o
host entrar na allowlist e o proxy reiniciar, que é o que prova que a allowlist
não era a camada que faltava.

Isto não é sobre o spotless: vale para todo plugin que busca configuração ou
ferramenta em runtime (checkstyle, license, pmd). Dizer à JVM onde fica a saída
é o mesmo gesto do `NODE_OPTIONS` para o V8 — informar ao runtime qual é o
mundo dele.
"""
from __future__ import annotations

from sandbox_runtime.k8s_driver import jvm_proxy_opts


def _opts(url="http://egress-proxy.dse.svc.cluster.local:8806") -> str:
    return jvm_proxy_opts(url)


def test_the_jvm_gets_the_proxy_host_and_port_for_both_schemes():
    o = _opts()
    assert "-Dhttps.proxyHost=egress-proxy.dse.svc.cluster.local" in o
    assert "-Dhttps.proxyPort=8806" in o
    assert "-Dhttp.proxyHost=egress-proxy.dse.svc.cluster.local" in o
    assert "-Dhttp.proxyPort=8806" in o


def test_the_cluster_is_not_reached_through_the_proxy():
    """`nonProxyHosts` da JVM usa `|` e `*`, não a vírgula do NO_PROXY. Mandar o
    tráfego para o próprio cluster pelo proxy é um laço."""
    o = _opts()
    assert "-Dhttp.nonProxyHosts=" in o
    valor = [p for p in o.split() if p.startswith("-Dhttp.nonProxyHosts=")][0]
    assert "|" in valor and "," not in valor
    assert "localhost" in valor and "*.svc" in valor


def test_a_proxy_url_without_a_port_still_yields_a_host():
    o = _opts("http://egress-proxy")
    assert "-Dhttps.proxyHost=egress-proxy" in o
    assert "proxyPort" not in o, "porta inventada é pior que porta ausente"


def test_no_proxy_configured_means_no_properties():
    assert jvm_proxy_opts("") == ""
    assert jvm_proxy_opts(None) == ""


def test_the_pod_carries_the_properties_in_maven_opts():
    """A propriedade só serve se chegar ao processo. `MAVEN_OPTS` é o canal, e
    o `-Duser.home=/tmp` que já estava lá não pode ser perdido — sem ele o
    Maven morre criando /home/dse/.m2 num rootfs read-only."""
    from sandbox_runtime import k8s_driver

    fonte = k8s_driver.__file__
    with open(fonte, encoding="utf-8") as fh:
        corpo = fh.read()
    assert "jvm_proxy_opts(cfg.egress_proxy_url)" in corpo, (
        "o MAVEN_OPTS do Pod tem de ser montado com as propriedades do proxy"
    )
    assert "-Duser.home=/tmp" in corpo
