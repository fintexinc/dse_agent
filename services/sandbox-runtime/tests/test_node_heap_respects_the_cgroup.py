"""O heap do Node é dimensionado pelo CGROUP, não pelo nó.

Medido 2026-08-10 (wi_8b083140): `lint could not run: the process was killed
(exit=134)` — SIGABRT, o kernel matando o Node por estourar o teto do
container. O comentário do values já descrevia a causa ("V8 sizes its heap
from the NODE, not the cgroup — so it happily allocates past the limit and the
kernel kills it") e a mitigação era subir o teto do Pod.

Essa mitigação é frágil pelo motivo que o dia provou: a VPS foi de 16 para
32 GB e o problema PIOROU — o V8 passou a mirar um heap ainda maior contra o
mesmo cgroup. Enquanto o teto do heap não for derivado do LIMITE DO
CONTAINER, todo redimensionamento de nó reabre a ferida.

`NODE_OPTIONS=--max-old-space-size=<MiB>` é para o Node o que `MAVEN_OPTS`
(já presente, mesma lista de env) é para a JVM: dizer ao runtime qual é o
mundo dele. Margem deliberada abaixo do limite — o heap velho não é a única
memória do processo (heap novo, buffers, o próprio binário).
"""
from __future__ import annotations

import pytest

from sandbox_runtime.k8s_driver import node_heap_mib


@pytest.mark.parametrize(
    "limit,expected",
    [
        ("3Gi", 2304),    # 3072 * 0.75
        ("8Gi", 6144),
        ("2Gi", 1536),
        ("1024Mi", 768),
        ("4G", 2861),     # G decimal, não Gi
    ],
)
def test_the_heap_ceiling_is_derived_from_the_container_limit(limit, expected):
    assert node_heap_mib(limit) == expected


def test_an_unparseable_limit_yields_no_ceiling_instead_of_a_wrong_one():
    """Sem limite legível, NÃO inventar número: um teto errado é pior que
    nenhum (o padrão do V8 pelo menos é conhecido)."""
    assert node_heap_mib("") is None
    assert node_heap_mib("banana") is None


def test_the_sandbox_pod_carries_the_node_ceiling():
    """O env chega ao Pod, na mesma lista onde MAVEN_OPTS já resolve o mesmo
    problema para a JVM. A config é construída explicitamente: `mem_limit` é
    lido no IMPORT (a import-time hazard que o próprio módulo documenta), e em
    produção isso é correto — o env vem do configmap antes do processo subir."""
    from sandbox_runtime import k8s_driver
    from sandbox_runtime.driver import SandboxProvisionRequest

    manifest = k8s_driver.build_pod_manifest(
        SandboxProvisionRequest(
            work_item_id="wi_x", tenant_id="t", branch="dse/wi_x",
            workspace_path="/workspace", checkpoint_path="/checkpoint.git",
            image="node:22",
        ),
        k8s_driver.K8sSandboxConfig(mem_limit="8Gi"),
    )
    env = {e["name"]: e["value"] for e in manifest["spec"]["containers"][0]["env"]}
    assert "NODE_OPTIONS" in env, (
        "sem teto de heap o Node mira a memória do NÓ e o kernel o mata "
        "(exit 134) — e subir o nó piora, como o upgrade de hoje provou"
    )
    assert "--max-old-space-size=6144" in env["NODE_OPTIONS"]
