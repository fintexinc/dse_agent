"""plan 08 §G — Kubernetes sandbox driver (real isolated runtime).

Today the agent runs in-process in the orchestrator (with the master key + creds
in the env) → the "nothing to steal inside the sandbox" threat model does NOT
hold. This driver executes each stage in an ephemeral, hardened Pod, ideally
under a strong-isolation RuntimeClass (gVisor/Kata).

What is CODE (delivered here, testable without a cluster):
  - `build_pod_manifest`: the fully hardened Pod spec (the core — the
    conformance suite validates every security property with no cluster).
  - `KubernetesSandboxDriver`: implements the same `SandboxDriver` contract.

What needs INFRA (live proof — the user's cluster decision):
  - a cluster with the RuntimeClass (gvisor/kata) installed;
  - the default-deny NetworkPolicy + egress only to the egress-proxy
    (documented);
  - the `agent-runner` image published to the cluster's registry.
Without a cluster/kubectl, `provision`/`execute_stage` FAIL CLEANLY
(fail-closed): they NEVER degrade to local execution (the same discipline as
DockerSandboxDriver).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from dse_contracts import (
    CheckpointOpRequest,
    CheckpointOpResult,
    CheckpointRef,
    WorkspaceBootstrapRequest,
    WorkspaceBootstrapResult,
)

from . import docker_driver
from .driver import (
    IsolatedStageExecutionUnavailable,
    SandboxCheckpointRequest,
    SandboxProvisionRequest,
    SandboxRebuildRequest,
    SandboxRebuildResult,
    StageExecutionRequest,
    StageExecutionResult,
)

NONROOT_UID = 10001

# 72h, deliberately not 24h. A reaped sandbox is NOT transparently rebuilt:
# the next agent turn fails to exec, retries until schedule_to_close and the
# work item ends `failed`. The lifetime has to sit beyond realistic human
# review dwell time, not just beyond the working phase.
DEFAULT_SANDBOX_TTL_SECONDS = 259200


def _ttl_seconds_from_env(default: int = DEFAULT_SANDBOX_TTL_SECONDS) -> int:
    """Tolerant parse. K8sSandboxConfig is evaluated at IMPORT time, so a bare
    int() would turn an empty or malformed DSE_SANDBOX_TTL_SECONDS — trivially
    produced by a Helm value rendering as "" — into a ValueError during import
    and crashloop the orchestrator worker. An unparsable TTL must degrade to the
    default, never take the worker down."""
    raw = (os.environ.get("DSE_SANDBOX_TTL_SECONDS") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _quantity_from_env(name: str, default: str) -> str:
    """Same import-time hazard as _ttl_seconds_from_env, different blast radius:
    a Helm value rendering as "" would put an EMPTY quantity in the manifest and
    make every `kubectl apply` fail with an invalid resource — one bad chart
    value would take provisioning down entirely, not just one Pod."""
    return (os.environ.get(name) or "").strip() or default


@dataclass
class K8sSandboxConfig:
    namespace: str = os.environ.get("DSE_SANDBOX_K8S_NAMESPACE", "dse-sandboxes")
    image: str = os.environ.get("DSE_AGENT_RUNNER_IMAGE", "dse/agent-runner:local")
    # Strong-isolation RuntimeClass. EMPTY = default runtime (WEAK isolation) —
    # build_pod_manifest logs/flags that; production must set it.
    runtime_class: str = os.environ.get("DSE_SANDBOX_RUNTIME_CLASS", "gvisor")
    service_account: str = os.environ.get("DSE_SANDBOX_SERVICE_ACCOUNT", "dse-sandbox-runner")
    #: Tetos dos SIDECARS de serviço (Tema 1) — da plataforma, jamais
    #: declaráveis: o repo não pode se conceder o nó. Espelham o postgres do
    #: preview hardcoded, que roda há semanas nesses números.
    service_cpu_request: str = _quantity_from_env("DSE_SERVICE_CPU_REQUEST", "100m")
    service_cpu_limit: str = _quantity_from_env("DSE_SERVICE_CPU_LIMIT", "500m")
    service_mem_request: str = _quantity_from_env("DSE_SERVICE_MEM_REQUEST", "128Mi")
    service_mem_limit: str = _quantity_from_env("DSE_SERVICE_MEM_LIMIT", "512Mi")
    service_ephemeral_request: str = _quantity_from_env("DSE_SERVICE_EPHEMERAL_REQUEST", "256Mi")
    service_ephemeral_limit: str = _quantity_from_env("DSE_SERVICE_EPHEMERAL_LIMIT", "1Gi")
    service_emptydir_size_limit: str = _quantity_from_env("DSE_SERVICE_EMPTYDIR_SIZE_LIMIT", "512Mi")
    service_tmp_size_limit: str = _quantity_from_env("DSE_SERVICE_TMP_SIZE_LIMIT", "256Mi")
    #: Teto do `prepare` (migração+seed do repo). Um prepare que passa disso
    #: está quebrado — e o laço não pode ficar refém dele.
    prepare_timeout_seconds: int = 300
    # Default FQDN + port 8806 (the real value comes from the configmap via env;
    # this default only applies outside the chart and avoids the stale port 3128
    # footgun).
    egress_proxy_url: str = os.environ.get("DSE_EGRESS_PROXY_URL", "http://egress-proxy.dse.svc.cluster.local:8806")
    # Repositório Maven PRIVADO do cliente. `feed_id` é o `id` do
    # `<repository>` no POM dele — o Maven casa credencial por id. Lidos do
    # ambiente do worker (o Secret), NUNCA do manifesto do Pod: o token viaja
    # por stdin do `exec` que escreve o settings.xml.
    maven_feed_id: str = os.environ.get("DSE_MAVEN_FEED_ID", "")
    maven_feed_username: str = os.environ.get("MAVEN_FEED_USERNAME", "")
    maven_feed_token: str = os.environ.get("MAVEN_FEED_TOKEN", "")
    cpu_limit: str = os.environ.get("DSE_SANDBOX_CPU_LIMIT", "3")
    mem_limit: str = os.environ.get("DSE_SANDBOX_MEM_LIMIT", "2Gi")
    # Local ephemeral storage. A clone + `npm install` fills the /workspace and
    # /tmp emptyDirs — the image sets HOME=/tmp, so the npm cache lands in the
    # SECOND one — and the kubelet bills their sum, plus /checkpoint.git,
    # against this limit. Undeclared (the state before this) no Pod can ever
    # exceed a limit of its own: the kubelet waits for node-level disk pressure
    # and then evicts a victim of its choosing, and a Pod requesting 0 is the
    # first one chosen.
    # The 2Gi, measured on the node this runs on: 29 GB rootfs at 76% = 6.8 GB
    # free, minus evictionHard nodefs.available=5% (~1.45 GB) = ~5.35 GB a
    # sandbox may actually spend. 5.35 / 2Gi ~ 2 concurrent sandboxes, which is
    # the honest disk cap; 3Gi drops that cap to 1, and >= 4Gi reproduces
    # today's failure (the node trips its own threshold before any Pod trips
    # its limit). A real large repo needs ~2-2.9 GB (clone 0.3-0.5 +
    # node_modules >= 1 + npm cache 0.3-0.5 + /checkpoint.git 0.2-0.4), so 2Gi
    # is deliberately tight and the env knob is the escape hatch until a real
    # workspace is measured inside the Pod.
    # The request buys eviction ORDER (a Pod under its request is reclaimed
    # last), not admission: the node advertises 26.6 GiB allocatable while only
    # 6.8 GB is free, so the scheduler would still admit a dozen sandboxes —
    # bounding how many exist at once is a separate control.
    # No emptyDir carries a sizeLimit yet; when one does, the three together
    # must fit inside this limit, or the Pod dies of the sum before any single
    # volume reaches its own cap.
    ephemeral_storage_limit: str = _quantity_from_env("DSE_SANDBOX_EPHEMERAL_STORAGE_LIMIT", "2Gi")
    ephemeral_storage_request: str = _quantity_from_env("DSE_SANDBOX_EPHEMERAL_STORAGE_REQUEST", "1Gi")
    kubectl: str = os.environ.get("DSE_KUBECTL", "kubectl")
    kube_context: str = os.environ.get("DSE_SANDBOX_KUBE_CONTEXT", "")
    # PVC for the git checkpoint (/checkpoint.git). Empty = emptyDir (ephemeral —
    # a rebuild after the Pod dies starts from scratch); production/VPS must
    # point at a PVC so the chaos rebuild can recover the last checkpoint.
    checkpoint_pvc: str = os.environ.get("DSE_SANDBOX_CHECKPOINT_PVC", "")
    # Absolute lifetime stamped on the Pod as `dse.fintex/expires-at`; the
    # sandbox-reaper CronJob collects Pods past that instant. This is the
    # BACKSTOP for Pods whose workflow died before teardown ran — never the
    # normal collection path, which is teardown on every terminal branch.
    # 0 disables the stamp (and with it any reaping of Pods from this build).
    ttl_seconds: int = _ttl_seconds_from_env()


#: Fração do limite do container que o heap velho do V8 pode ocupar. O resto é
#: o heap novo, buffers, o binário — memória real que o cgroup também conta.
_NODE_HEAP_FRACTION = 0.75


#: NO_PROXY é vírgula; a JVM é `|` com `*`. Duas gramáticas para a mesma lista,
#: e escrever a errada não dá erro — só manda o tráfego do cluster para o proxy.
_JVM_NON_PROXY = "localhost|127.0.0.1|*.svc|*.cluster.local"


def jvm_proxy_opts(egress_proxy_url: str | None) -> str:
    """As propriedades que dizem à JVM onde fica a saída.

    `http(s)_proxy` no ambiente resolve libcurl (git, npm, pip) e o
    `<proxies>` do settings.xml resolve o RESOLVEDOR do Maven — mas nenhum dos
    dois alcança um plugin que abre a própria conexão. `URLConnection` lê
    system properties e nada mais, e é assim que o spotless busca o formatter
    do Eclipse. Sem estas propriedades a conexão sai direta contra uma
    NetworkPolicy default-deny e morre em ConnectException, longe de qualquer
    mensagem que fale de proxy.

    Porta ausente não é inventada: a JVM sem `proxyPort` assume 80, que é
    errado mas é o padrão DELA — chutar 8806 aqui seria a plataforma decidindo
    por uma configuração que ela não leu."""
    if not egress_proxy_url:
        return ""
    resto = egress_proxy_url.split("://", 1)[-1].split("/", 1)[0]
    host, _, porta = resto.partition(":")
    if not host:
        return ""
    props = []
    for esquema in ("http", "https"):
        props.append(f"-D{esquema}.proxyHost={host}")
        if porta.isdigit():
            props.append(f"-D{esquema}.proxyPort={porta}")
    # `http.nonProxyHosts` vale para os dois esquemas na JVM; `https.nonProxyHosts`
    # nunca foi lido por ela.
    props.append(f"-Dhttp.nonProxyHosts={_JVM_NON_PROXY}")
    return " ".join(props)


def node_heap_mib(mem_limit: str) -> int | None:
    """Teto de `--max-old-space-size` (MiB) derivado do LIMITE DO CONTAINER.

    O V8 dimensiona o heap pela memória do NÓ, não pelo cgroup: num nó grande
    com container pequeno ele aloca alegremente além do teto e o kernel mata o
    processo (exit 134, medido no wi_8b083140 no lint do Angular). Subir o
    limite do Pod ameniza, mas é frágil na direção contrária — quando a VPS
    dobrou de 16 para 32 GB o problema PIOROU. Derivar do limite é o que
    sobrevive a qualquer redimensionamento.

    Devolve None para valor ilegível: um teto errado é pior que nenhum (o
    padrão do V8 ao menos é conhecido)."""
    text = (mem_limit or "").strip()
    for suffix, factor in (("Gi", 1024), ("Mi", 1), ("G", 1000), ("M", 1)):
        if text.endswith(suffix):
            try:
                value = float(text[: -len(suffix)])
            except ValueError:
                return None
            if suffix == "G":
                return int(value * 1000 / 1.048576 * _NODE_HEAP_FRACTION)
            return int(value * factor * _NODE_HEAP_FRACTION)
    return None


def pod_name_for(work_item_id: str) -> str:
    slug = "".join(c if c.isalnum() or c == "-" else "-" for c in work_item_id.lower())
    return f"dse-sbx-{slug}"[:63].rstrip("-")


# O builder mudou-se para dse_validation.build_credentials (2026-08-19): o
# preview precisa do MESMO documento e mora naquela camada — dois builders foi
# exatamente como o pod de preview ficou sem credencial enquanto o sandbox a
# tinha. O nome continua exportado daqui; os testes de contrato deste serviço
# pinam por ele.
from dse_validation.build_credentials import (  # noqa: E402
    maven_proxy_settings_xml as maven_proxy_settings_xml,
)


def _label_value(v: str) -> str:
    """K8s label value: at most 63 chars, not ending in -/_/.

    The real work_item_id is `wi_` + sha256 (64 hex) = 67 chars, which blows the
    limit and makes the Pod's `kubectl apply` fail (invalid metadata.labels). We
    truncate while preserving the recognizable prefix. This label is
    INFORMATIONAL — no selector uses it (Pods are addressed via pod_name_for)."""
    return v[:63].rstrip("-_.")



def _service_sidecars(
    services: "dict[str, Any]", cfg: "K8sSandboxConfig", service_password: str,
    container_sec: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(initContainers, volumes) dos serviços declarados — sidecars NATIVOS.

    initContainer com `restartPolicy: Always` (GA k8s ≥1.29; o k3s do piloto é
    1.31) e não container comum, pelas duas propriedades que importam aqui:
    o container principal só arranca depois do startupProbe do sidecar (o
    clone nunca dispara contra um Postgres em initdb), e o sidecar reinicia
    sozinho num Pod `restartPolicy: Never` (um banco que OOMa VOLTA, em vez de
    deixar toda rodada seguinte morrendo em ECONNREFUSED).

    A dupla-validação é aqui embaixo, em `build_pod_manifest`: o payload chega
    do probe já validado, mas quem escreve YAML re-valida com o MESMO parser —
    payload adulterado (ou de um worker antigo) não vira Pod.
    """
    from dse_validation.service_credentials import (
        references_service_password,
        translate_service_password,
    )

    sidecars: list[dict[str, Any]] = []
    volumes: list[dict[str, Any]] = []
    for name in sorted(services):
        decl = services[name]
        sec = dict(container_sec)
        if decl.user is not None:
            # Só NESTE container: PSA restricted exige non-root, não um uid
            # específico — e 70 (postgres-alpine) tem entrada no passwd da
            # imagem, o que fecha o "could not look up effective user ID" do
            # initdb. O agent-runner continua 10001; o fsGroup pod-level dá o
            # acesso aos volumes por grupo suplementar.
            sec["runAsUser"] = decl.user
            sec["runAsGroup"] = decl.user

        env: list[dict[str, str]] = []
        usa_senha = any(references_service_password(v) for v in decl.env.values())
        if usa_senha:
            # PRIMEIRO na lista: a expansão `$(VAR)` do kubelet só enxerga
            # variáveis definidas antes.
            env.append({"name": "DSE_SERVICE_PASSWORD", "value": service_password})
        env.extend(
            {"name": key, "value": translate_service_password(value)}
            for key, value in decl.env.items()
        )

        probe_handler: dict[str, Any] = (
            {"exec": {"command": list(decl.ready)}}
            if decl.ready
            else {"tcpSocket": {"port": decl.port}}
        )

        mounts: list[dict[str, Any]] = []
        graveis = list(decl.writable)
        if "/tmp" not in graveis:
            # /tmp gravável por default: readOnlyRootFilesystem também vale
            # nos sidecars, e quase toda imagem escreve algo em /tmp.
            graveis.append("/tmp")
        for idx, path in enumerate(graveis):
            vol_name = _label_value(f"svc-{name}-w{idx}")
            size = (
                cfg.service_tmp_size_limit if path == "/tmp"
                else cfg.service_emptydir_size_limit
            )
            volumes.append({"name": vol_name, "emptyDir": {"sizeLimit": size}})
            mounts.append({"name": vol_name, "mountPath": path})

        sidecars.append({
            "name": _label_value(f"svc-{name}"),
            "image": decl.image,
            # Sem isto o k8s aplica a política default por tag — e um sidecar
            # com tag mutável re-puxa a imagem a cada Pod, pagando registry na
            # latência de TODA volta.
            "imagePullPolicy": "IfNotPresent",
            # `Always` num initContainer é o que o torna SIDECAR nativo.
            "restartPolicy": "Always",
            "securityContext": sec,
            "env": env,
            "startupProbe": {**probe_handler, "periodSeconds": 2, "failureThreshold": 60},
            "readinessProbe": dict(probe_handler),
            "resources": {
                "requests": {
                    "cpu": cfg.service_cpu_request,
                    "memory": cfg.service_mem_request,
                    "ephemeral-storage": cfg.service_ephemeral_request,
                },
                "limits": {
                    "cpu": cfg.service_cpu_limit,
                    "memory": cfg.service_mem_limit,
                    "ephemeral-storage": cfg.service_ephemeral_limit,
                },
            },
            "volumeMounts": mounts,
        })
    return sidecars, volumes

def build_pod_manifest(
    request: SandboxProvisionRequest,
    cfg: K8sSandboxConfig | None = None,
    service_password: str | None = None,
) -> dict[str, Any]:
    # ---- Serviços declarados (Tema 1) -----------------------------------
    # Dupla-validação deliberada: o payload chega do probe já validado, mas
    # quem ESCREVE YAML re-valida com o MESMO parser do manifesto — payload
    # adulterado, truncado ou de um worker antigo não vira Pod. O import é
    # local pelo mesmo motivo do manifest_bootstrap: evitar ciclo de import
    # entre sandbox_runtime e dse_validation no load do worker.
    from dse_validation.config import parse_repo_services

    servicos = parse_repo_services(
        {"services": request.services} if request.services else {},
        source=f"provision:{request.work_item_id[:16]}",
    )
    if servicos and service_password is None:
        from dse_validation.service_credentials import generate_service_password

        service_password = generate_service_password()
    """Ephemeral, HARDENED Pod spec. The security core of §G (testable).

    Hardening (every item is asserted by the conformance suite):
      - runAsNonRoot + non-zero UID (pod and container);
      - allowPrivilegeEscalation=false, privileged=false, cap drop ALL;
      - readOnlyRootFilesystem=true (workspace/tmp are writable emptyDirs);
      - seccompProfile=RuntimeDefault;
      - automountServiceAccountToken=false;
      - NO hostPath/Docker socket, NO hostNetwork/PID/IPC;
      - egress only through the proxy (HTTP(S)_PROXY) — the default-deny
        NetworkPolicy is cluster-side (documented);
      - restartPolicy=Never (ephemeral); CPU/mem/ephemeral-storage limits."""
    cfg = cfg or K8sSandboxConfig()
    name = pod_name_for(request.work_item_id)
    container_sec = {
        "runAsNonRoot": True,
        "runAsUser": NONROOT_UID,
        "runAsGroup": NONROOT_UID,
        "allowPrivilegeEscalation": False,
        "privileged": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "serviceAccountName": cfg.service_account,
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": NONROOT_UID,
            "runAsGroup": NONROOT_UID,
            "fsGroup": NONROOT_UID,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "agent-runner",
                "image": cfg.image,
                "imagePullPolicy": "IfNotPresent",
                "securityContext": container_sec,
                "resources": {
                    "limits": {
                        "cpu": cfg.cpu_limit,
                        "memory": cfg.mem_limit,
                        "ephemeral-storage": cfg.ephemeral_storage_limit,
                    },
                    "requests": {
                        "cpu": "250m",
                        "memory": "512Mi",
                        "ephemeral-storage": cfg.ephemeral_storage_request,
                    },
                },
                "env": [
                    # A ÚNICA credencial que a plataforma dá ao repositório
                    # (Tema 1) — primeira da lista pela mesma regra dos
                    # sidecars: expansão $(VAR) do kubelet só enxerga o que
                    # veio antes. Presente apenas quando há serviço declarado.
                    *(
                        [{"name": "DSE_SERVICE_PASSWORD", "value": service_password}]
                        if servicos else []
                    ),
                    {"name": "HTTP_PROXY", "value": cfg.egress_proxy_url},
                    {"name": "HTTPS_PROXY", "value": cfg.egress_proxy_url},
                    {"name": "NO_PROXY", "value": "localhost,127.0.0.1,.svc,.cluster.local"},
                    # Lowercase aliases are NOT redundant. libcurl — and so git,
                    # npm, pip — reads only the lowercase `http_proxy` for
                    # http:// URLs; the uppercase form is deliberately ignored
                    # for that one scheme because an inbound `Proxy:` header
                    # would otherwise set it (httpoxy, CVE-2016-5385). With only
                    # the uppercase set, a plain-HTTP request from the sandbox
                    # goes DIRECT, bypassing the allowlist entirely.
                    {"name": "http_proxy", "value": cfg.egress_proxy_url},
                    {"name": "https_proxy", "value": cfg.egress_proxy_url},
                    {"name": "no_proxy", "value": "localhost,127.0.0.1,.svc,.cluster.local"},
                    # The JVM resolves user.home from /etc/passwd (/home/dse —
                    # read-only rootfs), NOT from the image's HOME=/tmp, so bare
                    # Maven dies creating /home/dse/.m2. /tmp is the writable
                    # emptyDir; this also makes Maven read the proxies file
                    # provision() writes to /tmp/.m2/settings.xml.
                    # `-Duser.home=/tmp` e as propriedades de proxy pelo MESMO
                    # canal: um plugin que abre a própria conexão (spotless
                    # buscando o formatter do Eclipse, checkstyle buscando a
                    # config) não passa pelo `<proxies>` do settings.xml nem
                    # pelo `https_proxy` do ambiente — só por system property.
                    {"name": "MAVEN_OPTS",
                     "value": " ".join(filter(None, [
                         "-Duser.home=/tmp", jvm_proxy_opts(cfg.egress_proxy_url)]))},
                    # O mesmo problema do MAVEN_OPTS acima, para o Node: o V8
                    # mira a memória do NÓ e é morto pelo cgroup (exit 134).
                    # Derivado do limite, não do nó — ver node_heap_mib.
                    *(
                        [{"name": "NODE_OPTIONS",
                          "value": f"--max-old-space-size={_heap}"}]
                        if (_heap := node_heap_mib(cfg.mem_limit)) else []
                    ),
                    {"name": "DSE_WORK_ITEM_ID", "value": request.work_item_id},
                    {"name": "DSE_TENANT_ID", "value": request.tenant_id},
                    {"name": "DSE_TASK_BRANCH", "value": request.branch},
                ],
                "volumeMounts": [
                    {"name": "workspace", "mountPath": "/workspace"},
                    {"name": "checkpoint", "mountPath": "/checkpoint.git"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {"name": "workspace", "emptyDir": {}},
            (
                {"name": "checkpoint", "persistentVolumeClaim": {"claimName": cfg.checkpoint_pvc}}
                if cfg.checkpoint_pvc
                else {"name": "checkpoint", "emptyDir": {}}
            ),
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    if servicos:
        sidecars, svc_volumes = _service_sidecars(
            servicos, cfg, service_password or "", container_sec
        )
        spec["initContainers"] = sidecars
        spec["volumes"].extend(svc_volumes)
    annotations: dict[str, str] = {}
    # The FULL work_item_id. It does not fit in a label — `wi_` + 64 hex = 67
    # chars and _label_value truncates to 63 — so the label cannot identify the
    # owner. Annotations have no such limit: this is what the reaper and any
    # operator use to correlate a leaked Pod back to its work item (the
    # workflow id IS the work_item_id).
    annotations["dse.fintex/work-item-id"] = request.work_item_id
    if cfg.ttl_seconds > 0:
        created = datetime.now(timezone.utc)
        annotations["dse.fintex/created-at"] = created.isoformat()
        annotations["dse.fintex/expires-at"] = (
            created + timedelta(seconds=cfg.ttl_seconds)
        ).isoformat()
    # Strong-isolation RuntimeClass: set only when configured. Empty = default
    # runtime (weak isolation) — we flag it in an annotation for the operator.
    if cfg.runtime_class:
        spec["runtimeClassName"] = cfg.runtime_class
    else:
        annotations["dse.fintex/isolation-warning"] = "no RuntimeClass — weak isolation"
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": cfg.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "dse-sandbox",
                "dse.fintex/work-item": _label_value(request.work_item_id),
                "dse.fintex/tenant": _label_value(request.tenant_id),
            },
            "annotations": annotations,
        },
        "spec": spec,
    }


class KubernetesSandboxDriver:
    """K8s driver: same contract as DockerSandboxDriver, but with real isolated
    execution. Fail-closed without a cluster/kubectl (never runs locally)."""

    def __init__(self, cfg: K8sSandboxConfig | None = None) -> None:
        self._cfg = cfg or K8sSandboxConfig()

    @property
    def workspace_is_host_visible(self) -> bool:
        return False  # the workspace lives in the Pod volume — git/hygiene via ops

    def sandbox_id_for(self, work_item_id: str) -> str:
        return pod_name_for(work_item_id)

    def execute_op(
        self, sandbox_id: str, op: str, payload: dict[str, Any], *, timeout_seconds: float = 180.0
    ) -> dict[str, Any]:
        return self._exec_op(sandbox_id, op, payload, timeout=int(timeout_seconds))

    def _kubectl(self, args: list[str], *, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
        if shutil.which(self._cfg.kubectl) is None:
            raise IsolatedStageExecutionUnavailable(
                f"kubectl ({self._cfg.kubectl}) not found — K8s runtime unavailable; "
                "local execution is forbidden as a fallback (§G fail-closed)"
            )
        ctx = ["--context", self._cfg.kube_context] if self._cfg.kube_context else []
        proc = subprocess.run(
            [self._cfg.kubectl, *ctx, *args],
            input=input_text, capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise IsolatedStageExecutionUnavailable(
                f"kubectl {' '.join(args)} failed (exit={proc.returncode}): {proc.stderr.strip()}"
            )
        return proc

    def _exec_op(self, pod_name: str, op: str, payload: dict[str, Any], *, timeout: int = 180) -> dict[str, Any]:
        """Run a runner lifecycle op INSIDE the Pod (`--op bootstrap|checkpoint`)
        — the K8s driver never operates git on a host path."""
        proc = self._kubectl(
            ["exec", "-i", pod_name, "-n", self._cfg.namespace, "--",
             "python", "-m", "agent_runner", "--op", op],
            input_text=json.dumps({"input": payload}),
            timeout=timeout,
        )
        try:
            return json.loads((proc.stdout or "").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise IsolatedStageExecutionUnavailable(
                f"agent-runner --op {op} returned non-JSON stdout: {(proc.stdout or '')[:200]!r}"
            ) from exc

    def _bootstrap(self, request: SandboxProvisionRequest) -> WorkspaceBootstrapResult:
        name = pod_name_for(request.work_item_id)
        out = self._exec_op(
            name, "bootstrap",
            WorkspaceBootstrapRequest(
                work_item_id=request.work_item_id, branch=request.branch,
                base_branch=request.base_branch, repo=request.repo,
            ).model_dump(),
        )
        result = WorkspaceBootstrapResult.model_validate(out)
        if result.failed:
            raise IsolatedStageExecutionUnavailable(
                f"workspace bootstrap failed in Pod {name}: [{result.error_kind}] {result.error}"
            )
        return result

    def provision(self, request: SandboxProvisionRequest) -> docker_driver.ProvisionedSandbox:
        manifest = build_pod_manifest(request, self._cfg)
        self._kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
        name = pod_name_for(request.work_item_id)
        servicos = sorted(request.services or {})
        # Um Pod com banco tem pull de imagem + initdb sob gVisor pela frente:
        # 120s do Pod de hoje + 90s por serviço, com teto — esperar mais que
        # isso não conserta nada, só atrasa o diagnóstico.
        wait_s = min(120 + 90 * len(servicos), 300) if servicos else 120
        try:
            self._kubectl(
                ["wait", "--for=condition=Ready", f"pod/{name}", "-n", self._cfg.namespace,
                 f"--timeout={wait_s}s"],
                timeout=wait_s + 60,
            )
        except IsolatedStageExecutionUnavailable as exc:
            # "timed out" sozinho é inútil: não separa imagem errada de initdb
            # travado de probe mentirosa. O erro carrega as palavras do próprio
            # sidecar — best-effort, o diagnóstico nunca esconde o erro real.
            partes = []
            for svc in servicos:
                try:
                    logs = self._kubectl(
                        ["logs", f"pod/{name}", "-c", _label_value(f"svc-{svc}"),
                         "-n", self._cfg.namespace, "--tail=5"],
                        timeout=30,
                    )
                    tail = (logs.stdout or logs.stderr or "").strip()[-300:]
                except Exception:  # noqa: BLE001 — diagnóstico é melhor-esforço
                    tail = "(no logs available)"
                partes.append(f"svc-{svc}: {tail}")
            detalhe = ("; ".join(partes))[:900]
            raise IsolatedStageExecutionUnavailable(
                f"{exc}" + (f"; sidecar diagnostics: {detalhe}" if partes else "")
            )
        self._bootstrap(request)
        # /tmp/.m2 because MAVEN_OPTS (build_pod_manifest) pins user.home=/tmp;
        # /tmp is an emptyDir, so this survives exactly as long as the Pod does.
        self._kubectl(
            ["exec", "-i", name, "-n", self._cfg.namespace, "--",
             "sh", "-c", "mkdir -p /tmp/.m2 && cat > /tmp/.m2/settings.xml"],
            input_text=maven_proxy_settings_xml(
                self._cfg.egress_proxy_url,
                feed_id=self._cfg.maven_feed_id,
                feed_username=self._cfg.maven_feed_username,
                feed_token=self._cfg.maven_feed_token,
            ),
        )
        if request.prepare:
            # A migração+seed DO REPO — depois do clone (`_bootstrap`) e do
            # settings.xml, porque o comando pode precisar do proxy Maven.
            # Falha NÃO é degradação: um banco sem schema faria toda rodada
            # seguinte reprovar em erro de SQL acusando o diff — melhor morrer
            # aqui, com o nome certo e o stderr na mão.
            import shlex as _shlex

            cap = int(self._cfg.prepare_timeout_seconds)
            comando = f"cd /workspace && timeout -k 10 {cap} {_shlex.join(request.prepare)}"
            try:
                proc = self._kubectl(
                    ["exec", "-i", name, "-n", self._cfg.namespace, "--",
                     "sh", "-c", comando],
                    timeout=cap + 60,
                )
            except IsolatedStageExecutionUnavailable as exc:
                raise IsolatedStageExecutionUnavailable(
                    f"the repository's prepare command failed — {exc}"
                )
            if proc.returncode != 0:
                tail = ((proc.stderr or "") + (proc.stdout or ""))[-400:]
                raise IsolatedStageExecutionUnavailable(
                    f"the repository's prepare command failed "
                    f"(exit={proc.returncode}, rc=124 means it outlived its "
                    f"{cap}s budget): {tail}"
                )
        return docker_driver.ProvisionedSandbox(
            container_id=name,
            container_name=name,
            work_item_id=request.work_item_id,
            tenant_id=request.tenant_id,
            branch=request.branch,
            workspace_host_path=request.workspace_path,
            checkpoint_bare_repo_path=request.checkpoint_path,
            resource_caps=docker_driver.ResourceCaps.from_budget(request.budget),
            created_new=True,
        )

    def run_in_pod(self, sandbox_id: str, argv: list[str], input_text: str | None = None,
                   *, timeout: int = 120) -> tuple[int, str]:
        """Run a plain command inside the sandbox Pod and return (rc, stdout).

        Public because skill materialization needs it and reaching into
        `_kubectl` from outside would couple that code to this class's private
        shape. Unlike `_exec_op` this does NOT go through the agent-runner
        protocol — it is for file plumbing, not lifecycle ops. It swallows the
        failure into an rc rather than raising: guidance must never be able to
        take a provision down.
        """
        try:
            proc = self._kubectl(
                ["exec", "-i", sandbox_id, "-n", self._cfg.namespace, "--", *argv],
                input_text=input_text, timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            return 1, str(exc)[:300]
        return proc.returncode, proc.stdout or ""

    def execute_stage(self, request: StageExecutionRequest) -> StageExecutionResult:
        started = time.time()
        payload = json.dumps({"stage": request.stage.value, "input": request.input_payload})
        proc = self._kubectl(
            ["exec", "-i", request.sandbox_id, "-n", self._cfg.namespace, "--",
             "python", "-m", "agent_runner", "--stage", request.stage.value],
            input_text=payload,
            timeout=int(request.timeout_seconds),
        )
        try:
            out = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            out = {"raw": proc.stdout}
        return StageExecutionResult(
            stage=request.stage,
            output_payload=out,
            exit_code=0,
            duration_seconds=time.time() - started,
        )

    def checkpoint(self, request: SandboxCheckpointRequest) -> CheckpointRef:
        # On K8s the workspace lives in a Pod volume — the commit/push happens
        # INSIDE it, against /checkpoint.git (PVC/emptyDir), with the same fixed
        # refspec + pre-receive hook as the local flow.
        pod = pod_name_for(request.work_item_id)
        out = self._exec_op(
            pod, "checkpoint",
            CheckpointOpRequest(
                work_item_id=request.work_item_id,
                branch=request.branch,
                phase=request.phase,
            ).model_dump(),
        )
        result = CheckpointOpResult.model_validate(out)
        if result.failed:
            raise IsolatedStageExecutionUnavailable(
                f"checkpoint failed in Pod {pod}: [{result.error_kind}] {result.error}"
            )
        return CheckpointRef(
            work_item_id=request.work_item_id, git_ref=result.sha, phase=result.phase
        )

    def rebuild(self, request: SandboxRebuildRequest) -> SandboxRebuildResult:
        # A fresh Pod mounting the SAME checkpoint volume (PVC): the bootstrap
        # inside provision finds the branch and clones — chaos-test recovery
        # with no git on the host at all. With emptyDir (dev), the checkpoint
        # dies with the Pod and the bootstrap starts from scratch (new sha).
        sandbox = self.provision(request.provision)
        state = self._bootstrap(request.provision)
        return SandboxRebuildResult(sandbox=sandbox, recovered_sha=state.sha)

    def teardown(self, sandbox_id: str) -> float:
        started = time.time()
        self._kubectl(["delete", "pod", sandbox_id, "-n", self._cfg.namespace, "--ignore-not-found", "--grace-period=5"])
        return time.time() - started
