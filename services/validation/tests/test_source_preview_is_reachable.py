"""O preview de fonte tem que ser ALCANÇÁVEL, não só compilar.

Medido no preview da PR #19 (fintexinc/bmo-fee-calculator-fe-dse, 2026-08-11).
O log do pod dizia:

    Application bundle generation complete. [28.243 seconds]

...e o pod ficou `0/1 Running` para sempre, até o timeout. Três defeitos
independentes, cada um sozinho suficiente para produzir exatamente esse
sintoma — app compilado, servindo, e inalcançável:

1. **`::1`**. O socket dentro do pod era `tcp 0 0 ::1:4200 :::* LISTEN`.
   `ng serve` escuta em localhost IPv6 por padrão; nem a sonda do kubelet nem
   o Service alcançam isso. `curl` de outro pod: HTTP 000.
2. **A porta**. `ng serve` IGNORA a env `PORT`, e o `npm start` do repo é
   `ng serve --configuration development` sem `--port`. A receita mandava
   `PORT=3000` e o servidor subia na 4200.
3. **O Host**. O dev server do Angular 19 é Vite, que recusa Host desconhecido:
   HTTP 403, `Blocked request. This host is not allowed.` — vindo do ingress,
   com o app de pé.

Por que isto merece teste e não só a correção: os três são invisíveis em tudo
que o DSE já mede. O build passou, o `npm install` passou, o pod está `Running`,
não há log de erro — o defeito só aparece de FORA, e "de fora" é exatamente o
que a suíte não faz. Um teste sobre o comando é o mais perto que dá para chegar
sem subir um cluster, e cobre a regressão que importa: alguém reescrever a
linha de start e derrubar uma das três flags sem perceber.

A quarta asserção é de fronteira: `--disable-host-check` NÃO é a correção. O
hostname do preview é determinístico e nosso; liberar só ele custa uma linha e
não abre o dev server para DNS rebinding.
"""
from __future__ import annotations

import pytest
from dse_validation.config import PreviewConfig

from dse_validation.preview.argocd import build_manifests


def _source_container_command(cfg: PreviewConfig, namespace: str = "preview-wi-x") -> str:
    """A linha de comando do container do preview de FONTE (kind=ui)."""
    from datetime import datetime, timedelta, timezone

    exp = datetime.now(timezone.utc) + timedelta(seconds=600)
    manifests = build_manifests(
        namespace, "wi-x", "tenant_dev", exp, 600, cfg,
        repo="fintexinc/bmo-fee-calculator-fe-dse", branch="dse/wi-x", kind="ui",
    )
    deployment = manifests["deployment.yaml"]
    assert "npm start" in deployment, (
        "este teste é sobre o preview de FONTE; o manifest gerado não tem "
        "`npm start`, então o cenário mudou e a asserção abaixo mentiria"
    )
    return deployment


@pytest.fixture()
def cfg() -> PreviewConfig:
    c = PreviewConfig()
    c.mode = "source"          # a receita de fonte; sem isto sai o preview estático
    c.external_host_template = "https://{namespace}.preview.notas.api.br"
    return c


def test_the_dev_server_listens_on_all_interfaces(cfg):
    """Defeito 1: `::1`. Sem `--host 0.0.0.0` nada fora do pod alcança —
    nem a sonda, e o preview fica 0/1 com o app rodando lá dentro."""
    assert "--host 0.0.0.0" in _source_container_command(cfg), (
        "sem `--host 0.0.0.0` o `ng serve` escuta em `::1` e o pod nunca fica "
        "Ready. Foi o socket medido na PR #19: `tcp ::1:4200 LISTEN`"
    )


def test_the_port_is_a_flag_because_the_env_var_is_ignored(cfg):
    """Defeito 2: a env `PORT` não é lida pelo Angular CLI. A porta que o
    Service e a sonda usam tem que ir na linha de comando."""
    command = _source_container_command(cfg)
    port = PreviewConfig().source_port
    assert f"--port {port}" in command, (
        f"a porta {port} só existe como env `PORT`, que o `ng serve` IGNORA — "
        f"o servidor sobe na 4200 e o Service aponta para o vazio"
    )


def test_the_ingress_hostname_is_allowed_by_the_dev_server(cfg):
    """Defeito 3: o Vite recusa Host desconhecido com 403. O host liberado tem
    que ser EXATAMENTE o do ingress, senão o 403 volta com outro nome."""
    namespace = "preview-wi-8d80bad6"
    command = _source_container_command(cfg, namespace)
    hostname = cfg.external_hostname_for(namespace)

    assert hostname, "a fixture configura o template; sem host o teste não prova nada"
    assert f"--allowed-hosts {hostname}" in command, (
        f"o dev server (Vite/Angular 19) responde 403 «Blocked request. This "
        f"host is not allowed» para {hostname}. É o mesmo host que o ingress "
        f"publica — tem que ser liberado no start"
    )


def test_the_fix_is_not_to_disable_the_host_check(cfg):
    """PIN de fronteira. `--disable-host-check` faria os três testes acima
    passarem por acidente e abriria o dev server para qualquer Host. Como o
    hostname é derivado de template e conhecido aqui, não há ganho nenhum."""
    assert "--disable-host-check" not in _source_container_command(cfg), (
        "a checagem de Host foi DESLIGADA em vez de o host do ingress ser "
        "liberado. O hostname é determinístico e está em escopo — use "
        "`--allowed-hosts`"
    )


def test_without_a_hostname_template_the_command_is_still_valid(cfg):
    """Sem template de host não há ingress, e o preview é só interno ao
    cluster: `--allowed-hosts` não tem valor para receber. O comando não pode
    sair com uma flag pela metade."""
    bare = PreviewConfig()
    bare.mode = "source"
    bare.external_host_template = ""
    command = _source_container_command(bare)

    assert "--host 0.0.0.0" in command, "host e porta continuam valendo sem ingress"
    assert "--allowed-hosts" not in command, (
        "`--allowed-hosts` sem valor engole a flag seguinte (é `array` no "
        "schema do Angular) — sem hostname a flag não deve existir"
    )
