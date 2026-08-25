"""Os serviços que o repo declara viram SIDECARS NATIVOS no Pod do sandbox.

Sidecar nativo = initContainer com `restartPolicy: Always` (GA no k8s ≥1.29; o
k3s do piloto é v1.31.4). Não é pedantismo de API — são as duas propriedades
que um container comum não dá:

  - ordenação: o container principal só arranca depois do startupProbe do
    sidecar, então o clone nunca dispara contra um Postgres no meio do initdb;
  - restart independente num Pod `restartPolicy: Never`: um banco que OOMa
    VOLTA, em vez de deixar todas as rodadas seguintes de Tester/L1 morrendo
    em ECONNREFUSED.

O tráfego é localhost dentro do Pod — não atravessa NetworkPolicy nem o
egress-proxy (o problema "proxy HTTP não fala protocolo de banco" desaparece
por construção). PSA `restricted` é admission no namespace: sidecar sem o
hardening completo = Pod REJEITADO no apply, então o hardening é replicado
campo a campo e testado aqui.

A senha (`$DSE_SERVICE_PASSWORD`) é TRADUZIDA, não substituída: o literal vira
`$(DSE_SERVICE_PASSWORD)` — expansão do kubelet — e a variável é definida
PRIMEIRO na lista de env de quem a referencia. É o que resolve o caso
substring (`postgresql://u:$DSE_SERVICE_PASSWORD@localhost/db`), que um
secretKeyRef sozinho não resolve.
"""
from __future__ import annotations

import pytest

from sandbox_runtime.driver import SandboxProvisionRequest
from sandbox_runtime.k8s_driver import K8sSandboxConfig, build_pod_manifest

_POSTGRES = {
    "image": "postgres:16-alpine",
    "port": 5432,
    "env": {"POSTGRES_PASSWORD": "$DSE_SERVICE_PASSWORD",
            "POSTGRES_DB": "app",
            "PGDATA": "/var/lib/postgresql/data/pgdata"},
    "ready": ["pg_isready", "-U", "postgres"],
    "user": 70,
    "writable": ["/var/lib/postgresql/data", "/var/run/postgresql"],
}


def _req(**over):
    base = dict(work_item_id="wi_abc123", tenant_id="tenant_dev",
                branch="dse/wi_abc123", workspace_path="/w", checkpoint_path="/c")
    base.update(over)
    return SandboxProvisionRequest(**base)


def _pod(services=None, password="senha-teste-0123456789abcdef000000"):
    return build_pod_manifest(
        _req(services=services), K8sSandboxConfig(), service_password=password)


def _sidecars(pod):
    return pod["spec"].get("initContainers") or []


# --- a forma do Pod ---------------------------------------------------------

def test_a_request_without_services_produces_the_exact_pod_of_today():
    """Regressão de forma: repo que não declara nada não percebe diferença —
    sem initContainers, sem env nova no agent-runner."""
    antes = build_pod_manifest(_req(), K8sSandboxConfig())

    assert "initContainers" not in antes["spec"]
    nomes_env = [e["name"] for e in antes["spec"]["containers"][0]["env"]]
    assert "DSE_SERVICE_PASSWORD" not in nomes_env


def test_a_declared_service_becomes_a_native_sidecar_with_restart_always():
    pod = _pod({"postgres": dict(_POSTGRES)})
    sidecars = _sidecars(pod)

    assert [c["name"] for c in sidecars] == ["svc-postgres"]
    assert sidecars[0]["restartPolicy"] == "Always"
    assert sidecars[0]["image"] == "postgres:16-alpine"


def test_the_sidecar_carries_the_full_hardening_psa_restricted_requires():
    """PSA restricted é admission: um campo faltando aqui não degrada — REJEITA
    o Pod inteiro no apply. O hardening do agent-runner é replicado campo a
    campo."""
    sec = _sidecars(_pod({"postgres": dict(_POSTGRES)}))[0]["securityContext"]

    assert sec["runAsNonRoot"] is True
    assert sec["allowPrivilegeEscalation"] is False
    assert sec["privileged"] is False
    assert sec["readOnlyRootFilesystem"] is True
    assert sec["capabilities"] == {"drop": ["ALL"]}
    assert sec["seccompProfile"] == {"type": "RuntimeDefault"}


def test_service_user_overrides_runasuser_only_in_that_container():
    """`user: 70` resolve o initdb do postgres-alpine (uid com entrada no
    passwd da imagem). PSA exige non-root, não uid específico — e o override é
    do CONTAINER: o agent-runner continua 10001."""
    pod = _pod({"postgres": dict(_POSTGRES)})

    sec = _sidecars(pod)[0]["securityContext"]
    assert sec["runAsUser"] == 70
    assert sec["runAsGroup"] == 70
    assert pod["spec"]["containers"][0]["securityContext"]["runAsUser"] == 10001
    assert pod["spec"]["securityContext"]["fsGroup"] == 10001


def test_writable_paths_become_sized_emptydirs_plus_a_default_tmp():
    """readOnlyRootFilesystem também nos sidecars: escrita fora do declarado
    falha cedo e ruidosa (EROFS com o path no log), nunca silenciosa. Cada
    `writable` vira emptyDir COM sizeLimit — emptyDir sem teto come o orçamento
    de disco do Pod inteiro."""
    pod = _pod({"postgres": dict(_POSTGRES)})
    sidecar = _sidecars(pod)[0]

    mounts = {m["mountPath"]: m["name"] for m in sidecar["volumeMounts"]}
    assert "/var/lib/postgresql/data" in mounts
    assert "/var/run/postgresql" in mounts
    assert "/tmp" in mounts, "todo sidecar ganha /tmp gravável por default"

    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    for path, vol_name in mounts.items():
        assert volumes[vol_name]["emptyDir"].get("sizeLimit"), (
            f"emptyDir de {path} sem sizeLimit come o orçamento do Pod"
        )


def test_a_ready_argv_becomes_exec_startup_and_readiness_probes():
    sidecar = _sidecars(_pod({"postgres": dict(_POSTGRES)}))[0]

    assert sidecar["startupProbe"]["exec"]["command"] == ["pg_isready", "-U", "postgres"]
    assert sidecar["startupProbe"]["periodSeconds"] == 2
    assert sidecar["startupProbe"]["failureThreshold"] == 60
    assert sidecar["readinessProbe"]["exec"]["command"] == ["pg_isready", "-U", "postgres"]


def test_absent_ready_defaults_to_a_tcpsocket_probe_on_the_declared_port():
    """Exigir `ready` mataria a tarefa no parse por um argv que o autor teria
    de inventar; para redis/wiremock o tcpSocket é exatamente certo."""
    decl = {k: v for k, v in _POSTGRES.items() if k != "ready"}
    sidecar = _sidecars(_pod({"cache": {**decl, "port": 6379}}))[0]

    assert sidecar["startupProbe"]["tcpSocket"] == {"port": 6379}
    assert sidecar["readinessProbe"]["tcpSocket"] == {"port": 6379}


# --- a senha ----------------------------------------------------------------

def test_the_password_is_translated_into_kubelet_expansion_and_defined_first():
    sidecar = _sidecars(_pod({"postgres": dict(_POSTGRES)}))[0]
    env = sidecar["env"]

    assert env[0]["name"] == "DSE_SERVICE_PASSWORD", (
        "a variável tem de existir ANTES de quem a referencia — o kubelet "
        "expande na ordem da lista"
    )
    assert env[0]["value"] == "senha-teste-0123456789abcdef000000"
    valores = {e["name"]: e.get("value") for e in env}
    assert valores["POSTGRES_PASSWORD"] == "$(DSE_SERVICE_PASSWORD)", (
        "tradução para a expansão do KUBELET — resolve o caso substring que "
        "secretKeyRef sozinho não resolve"
    )
    assert valores["POSTGRES_DB"] == "app"


def test_the_agent_container_exports_dse_service_password_only_when_services_exist():
    com = _pod({"postgres": dict(_POSTGRES)})
    env = com["spec"]["containers"][0]["env"]
    assert env[0]["name"] == "DSE_SERVICE_PASSWORD", (
        "o container principal exporta a ÚNICA credencial que a plataforma dá "
        "ao repo — os comandos de teste a leem via $DSE_SERVICE_PASSWORD"
    )


def test_a_bad_services_payload_is_refused_by_the_real_parser():
    """Dupla-validação: o driver re-valida com o MESMO parser do manifesto.
    Payload que chegou adulterado (ou de um probe antigo) não vira Pod."""
    with pytest.raises(Exception):
        _pod({"db": {"image": "postgres:16\nkind: Pod", "port": 5432}})


# --- o orçamento ------------------------------------------------------------

def test_sidecar_resources_come_from_platform_knobs_never_from_the_repo():
    sidecar = _sidecars(_pod({"postgres": dict(_POSTGRES)}))[0]
    res = sidecar["resources"]

    assert res["requests"]["cpu"] == "100m"
    assert res["requests"]["memory"] == "128Mi"
    assert res["limits"]["cpu"] == "500m"
    assert res["limits"]["memory"] == "512Mi"
    assert res["limits"]["ephemeral-storage"] == "1Gi"


# --- o provision ------------------------------------------------------------

class _KubectlFake:
    """Registra cada chamada e responde sucesso. O driver real é o objeto; só a
    fronteira com o cluster é substituída."""

    def __init__(self):
        self.chamadas: list[list[str]] = []
        self.fail_wait = False

    def __call__(self, args, *, input_text=None, timeout=120):
        import subprocess as sp

        self.chamadas.append(list(args))
        if self.fail_wait and args[0] == "wait":
            from sandbox_runtime.k8s_driver import IsolatedStageExecutionUnavailable

            raise IsolatedStageExecutionUnavailable("kubectl wait failed (exit=1): timed out")
        return sp.CompletedProcess(args, 0, stdout="{}", stderr="")


def _driver(monkeypatch, fake):
    from sandbox_runtime.k8s_driver import K8sSandboxDriver, K8sSandboxConfig

    drv = K8sSandboxDriver(K8sSandboxConfig())
    monkeypatch.setattr(drv, "_kubectl", fake)
    monkeypatch.setattr(drv, "_bootstrap", lambda request: fake.chamadas.append(["<bootstrap>"]))
    return drv


def test_the_provision_wait_grows_with_declared_services_and_caps(monkeypatch):
    """Um Pod com banco tem pull + initdb sob gVisor pela frente: 120s do Pod
    de hoje + 90s por serviço, com teto de 300s."""
    fake = _KubectlFake()
    drv = _driver(monkeypatch, fake)
    drv.provision(_req(services={"postgres": dict(_POSTGRES)}))

    wait = next(c for c in fake.chamadas if c[0] == "wait")
    assert "--timeout=210s" in wait

    fake2 = _KubectlFake()
    drv2 = _driver(monkeypatch, fake2)
    tres = {f"s{i}": {**_POSTGRES, "port": 5432 + i} for i in range(3)}
    drv2.provision(_req(services=tres))
    wait2 = next(c for c in fake2.chamadas if c[0] == "wait")
    assert "--timeout=300s" in wait2, "90*3+120=390 capa em 300"


def test_prepare_runs_in_the_workspace_after_bootstrap_and_fails_the_provision_loudly(monkeypatch):
    """`prepare` é a migração+seed DO REPO — roda depois do clone (e do
    settings.xml: o comando pode precisar do proxy Maven), sob `timeout -k`.
    Falha NÃO é degradação: um banco sem schema faria toda rodada seguinte
    reprovar em erro de SQL acusando o diff — melhor morrer aqui, nomeado."""
    fake = _KubectlFake()
    drv = _driver(monkeypatch, fake)
    drv.provision(_req(services={"postgres": dict(_POSTGRES)},
                       prepare=["sh", "-c", "npx prisma migrate deploy"]))

    achatado = ["\x00".join(c) for c in fake.chamadas]
    idx_boot = next(i for i, c in enumerate(fake.chamadas) if c == ["<bootstrap>"])
    idx_prep = next(i for i, c in enumerate(achatado) if "prisma migrate deploy" in c)
    assert idx_prep > idx_boot, "prepare antes do clone rodaria num workspace vazio"
    assert "timeout -k" in achatado[idx_prep]

    class _Estoura(_KubectlFake):
        def __call__(self, args, *, input_text=None, timeout=120):
            import subprocess as sp

            self.chamadas.append(list(args))
            if any("prisma" in a for a in args):
                return sp.CompletedProcess(args, 1, stdout="", stderr="relation does not exist")
            return sp.CompletedProcess(args, 0, stdout="{}", stderr="")

    fake3 = _Estoura()
    drv3 = _driver(monkeypatch, fake3)
    with pytest.raises(Exception) as err:
        drv3.provision(_req(services={"postgres": dict(_POSTGRES)},
                            prepare=["sh", "-c", "npx prisma migrate deploy"]))
    assert "prepare" in str(err.value)
    assert "relation does not exist" in str(err.value)


def test_a_failed_sidecar_enriches_the_provision_error_with_its_own_words(monkeypatch):
    """O `kubectl wait` que estoura dizia só "timed out" — inútil para decidir
    entre imagem errada, initdb travado e probe mentirosa. O erro passa a
    carregar o status e as últimas linhas de log de cada sidecar."""
    fake = _KubectlFake()
    fake.fail_wait = True

    class _ComDiagnostico(_KubectlFake):
        def __call__(self, args, *, input_text=None, timeout=120):
            import subprocess as sp

            self.chamadas.append(list(args))
            if args[0] == "wait":
                from sandbox_runtime.k8s_driver import IsolatedStageExecutionUnavailable

                raise IsolatedStageExecutionUnavailable("kubectl wait failed (exit=1): timed out")
            if args[0] == "logs":
                return sp.CompletedProcess(args, 0, stdout="FATAL: data directory has wrong ownership", stderr="")
            return sp.CompletedProcess(args, 0, stdout="{}", stderr="")

    fake = _ComDiagnostico()
    drv = _driver(monkeypatch, fake)
    with pytest.raises(Exception) as err:
        drv.provision(_req(services={"postgres": dict(_POSTGRES)}))
    assert "svc-postgres" in str(err.value)
    assert "wrong ownership" in str(err.value)


def test_the_docker_driver_ignores_services_with_an_audit_note(caplog):
    """O driver Docker local (dev) não implementa sidecars — e não pode fingir
    que implementa. Ignora COM aviso nomeado, nunca em silêncio."""
    import logging

    from sandbox_runtime.driver import DockerSandboxDriver

    drv = DockerSandboxDriver.__new__(DockerSandboxDriver)
    with caplog.at_level(logging.WARNING):
        drv._warn_services_unsupported(_req(services={"postgres": dict(_POSTGRES)}))
    assert any("services" in r.message for r in caplog.records)
