"""O preview é servido por HTTPS, porque apps reais exigem origem segura.

Medido no preview da PR #19 (fintexinc/bmo-fee-calculator-fe-dse, 2026-08-11),
depois que o pod finalmente ficou Ready e a URL respondeu `200`:

    tela em branco

O `<title>` era o certo, `main.js` baixava 4,6 MB, o ngrx inicializava — e o
console dizia:

    ERROR auth0-spa-js must run on a secure origin.

O ingress do preview não pedia certificado. Sobre `http://`, a plataforma web
desliga um bloco inteiro de recursos — `crypto.subtle`, service workers,
geolocalização, e todo SDK de autenticação que se recusa a rodar inseguro. O
preview não estava "quase funcionando": para qualquer app que autentica, ele
não funcionava.

Isso é diferente dos três defeitos da linha de start (que impediam o pod de
ficar Ready, e por isso eram visíveis). Este passa no `curl`, passa na sonda,
passa em qualquer verificação que olhe código de status — e entrega uma página
em branco para o humano que clicou no link da PR. É o pior formato possível de
defeito de preview, porque parece pronto.

O cluster já sabia fazer isso: os ingresses do próprio DSE (`dse.notas.api.br`
e irmãos) usam o resolver ACME do Traefik com duas anotações e um bloco `tls`.
O preview simplesmente não pedia. Este teste faz o pedido ser obrigatório.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from dse_validation.config import PreviewConfig

from dse_validation.preview.argocd import build_manifests


def _ingress(cfg: PreviewConfig, namespace: str = "preview-wi-x") -> str:
    exp = datetime.now(timezone.utc) + timedelta(seconds=600)
    manifests = build_manifests(
        namespace, "wi-x", "tenant_dev", exp, 600, cfg,
        repo="acme/fe", branch="dse/wi-x", kind="ui",
    )
    return manifests.get("ingress.yaml", "")


@pytest.fixture()
def cfg() -> PreviewConfig:
    c = PreviewConfig()
    c.mode = "source"
    c.external_host_template = "https://{namespace}.preview.notas.api.br"
    return c


def test_the_ingress_asks_for_a_certificate(cfg):
    """Sem o bloco `tls` o Traefik serve só `http://`, e o app fica em branco
    com `auth0-spa-js must run on a secure origin` no console."""
    ingress = _ingress(cfg)
    hostname = cfg.external_hostname_for("preview-wi-x")

    assert "tls:" in ingress, (
        "o ingress do preview não pede certificado — sobre http:// o SDK de "
        "auth do cliente se recusa a rodar e a página fica em branco"
    )
    assert hostname in ingress.split("tls:", 1)[1], (
        "o bloco `tls` não cobre o host que o ingress publica"
    )


def test_the_certificate_comes_from_the_same_resolver_the_cluster_already_uses(cfg):
    """As duas anotações do Traefik. Não são decoração: sem `certresolver` o
    bloco `tls` não gera certificado nenhum, e sem `entrypoints=websecure` o
    router não escuta na 443. Os ingresses do DSE já usam exatamente estas."""
    ingress = _ingress(cfg)

    assert "traefik.ingress.kubernetes.io/router.tls.certresolver" in ingress, (
        "sem certresolver o bloco `tls` é inerte — o Traefik não tem de onde "
        "tirar o certificado"
    )
    assert "traefik.ingress.kubernetes.io/router.entrypoints" in ingress, (
        "sem entrypoints=websecure o router não atende na 443"
    )


def test_the_url_on_the_pr_is_the_scheme_that_actually_works(cfg):
    """O link que vai para a PR tem que ser `https://`. Um preview servido por
    TLS e anunciado como `http://` é o mesmo bug com outra roupa: o humano
    clica, cai no inseguro, e vê a página em branco."""
    assert cfg.preview_url_for("preview-wi-x").startswith("https://"), (
        "a URL publicada na PR não é https — o app não roda nela"
    )


def test_no_certificate_is_requested_when_there_is_no_hostname():
    """Sem template não há ingress, e pedir certificado para um host que não
    existe faria o ACME falhar em loop contra o rate limit da Let's Encrypt."""
    bare = PreviewConfig()
    bare.mode = "source"
    bare.external_host_template = ""
    assert _ingress(bare) == "", "sem hostname não deve haver ingress algum"
