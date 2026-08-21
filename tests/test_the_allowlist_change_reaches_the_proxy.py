"""Mudar a allowlist de egress e dar `helm upgrade` não reiniciava nada.

A lista chega ao proxy como `DSE_EGRESS_ALLOW_HOSTS`, uma variável de ambiente
do ConfigMap compartilhado, consumida por `envFrom`. Kubernetes NÃO reinicia um
Pod quando um ConfigMap muda, e o servidor lê o ambiente uma vez no start
(`_build_allowlist_from_env`). Então o operador editava os values, subia o
chart, via "STATUS: deployed" — e o proxy seguia com a lista antiga até alguém
mexer na imagem por outro motivo.

Medido ao vivo (wi_2f3b8332, 2026-08-21): o spotless do calculation-engine
baixa o formatter do Eclipse em tempo de execução, `download.eclipse.org` não
estava na lista, o lint morreu sem imprimir diagnóstico e o item escalou. A
auditoria de escala já tinha previsto o laço pelo nome, item 3.5: "build dies,
operator edits values, upgrades, and the fix doesn't take effect".

A anotação de checksum no pod template é o que fecha isso: o hash do ConfigMap
renderizado entra na anotação, então qualquer mudança nele muda o template do
Pod e o Deployment rola sozinho.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1] / "infra" / "helm" / "dse"
_ANNOTATION = "checksum/config"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm não está instalado nesta máquina"
)


def _render(allow_hosts: str) -> dict:
    """O Deployment do egress-proxy, renderizado com uma allowlist dada."""
    out = subprocess.run(
        ["helm", "template", "dse", str(_CHART),
         "--set", "egressProxy.enabled=true",
         "--set-json", f'egressProxy.allowlist={allow_hosts}'],
        capture_output=True, text=True, check=True,
    ).stdout
    for doc in yaml.safe_load_all(out):
        if (doc and doc.get("kind") == "Deployment"
                and doc["metadata"]["name"].endswith("-egress-proxy")):
            return doc
    raise AssertionError("o Deployment do egress-proxy não saiu no render")


_UMA = '[{"host":"a.example.com","port":443,"purpose":"x"}]'
_OUTRA = '[{"host":"a.example.com","port":443,"purpose":"x"},'\
         '{"host":"download.eclipse.org","port":443,"purpose":"y"}]'


def test_the_pod_template_carries_a_config_checksum():
    dep = _render(_UMA)
    anotacoes = dep["spec"]["template"]["metadata"].get("annotations") or {}
    assert _ANNOTATION in anotacoes, (
        "sem checksum do ConfigMap no pod template, mudar a allowlist não "
        "reinicia o proxy e o `helm upgrade` mente"
    )


def test_adding_a_host_changes_the_checksum():
    antes = _render(_UMA)["spec"]["template"]["metadata"]["annotations"]
    depois = _render(_OUTRA)["spec"]["template"]["metadata"]["annotations"]
    assert antes[_ANNOTATION] != depois[_ANNOTATION], (
        "o host novo tem de mudar o hash — é isso que faz o Deployment rolar"
    )
