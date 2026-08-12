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


def test_the_start_command_does_not_require_one_exact_script(cfg):
    """A receita não pode depender de o repo ter EXATAMENTE `npm start`.

    Medido na PR #5 (2026-08-11), terceiro modo de falha da mesma família:

        npm error Missing script: "start"

    Antes disso já tinham sido `browserTarget` (PRs #2 e #3) e o `ng serve` em
    `::1` (PR #19). O padrão é sempre o mesmo — a receita assume uma forma de
    repositório, o repositório tem outra, e o preview morre em CrashLoop.

    O comando passa a TENTAR o que existe, em ordem, em vez de exigir um nome:
    `start`, depois `dev`, depois o Angular CLI local. E se não houver nenhum,
    diz isso em vez de deixar o npm reclamar de script faltando."""
    command = _source_container_command(cfg)

    assert "npm start" in command, "o caminho normal continua sendo `npm start`"
    assert "run dev" in command, (
        "sem alternativa, um repo que chama o script de `dev` morre em "
        "CrashLoop — e é a mesma classe do `Missing script: start` da PR #5"
    )
    assert "node_modules/.bin/ng" in command, (
        "sem o CLI local como último degrau, um repo Angular sem script "
        "nenhum não tem como subir"
    )


def test_a_repo_with_no_way_to_serve_says_so(cfg):
    """Quando nenhum degrau serve, a mensagem tem de ser NOSSA e explicar. O
    `npm error Missing script` manda o humano procurar no lugar errado: o
    problema não é o npm, é o repositório não declarar como se serve."""
    command = _source_container_command(cfg)
    assert "DSE preview:" in command, (
        "o comando termina sem mensagem própria; o humano recebe o erro cru do "
        "npm e vai depurar a ferramenta em vez do repositório"
    )


def test_the_generated_command_is_valid_shell(cfg, tmp_path):
    """Os testes acima procuram SUBSTRINGS, e substring não prova que o `sh`
    aceita o comando. A primeira versão da cadeia de fallback passou nos dois e
    gerava `\\\\\\"` — escape duplo, porque o script já é escapado uma vez pelo
    `json.dumps` que o põe no YAML. O container teria morrido no arranque com
    erro de sintaxe, e o sintoma seria mais um CrashLoop indistinguível dos
    outros três.

    `sh -n` analisa sem executar: é a diferença entre "o texto contém o que eu
    esperava" e "isto roda"."""
    import json
    import subprocess

    deployment = _source_container_command(cfg)
    # o script vive numa linha `- "<json>"` dentro de args
    linha = next(ln.strip() for ln in deployment.splitlines()
                 if ln.strip().startswith("- \"") and "npm" in ln)
    script = json.loads(linha[2:].strip())

    proc = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"o `sh` recusa o comando gerado: {proc.stderr.strip()}\n\n{script[:400]}"
    )
    assert "\\\"" not in script, (
        "o script carrega barras de escape literais — sinal de escape duplo: "
        "`json.dumps` já escapa quando põe o script no YAML"
    )
