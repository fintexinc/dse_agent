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
import urllib.parse
from dataclasses import dataclass
from xml.sax.saxutils import escape as xml_escape
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
    cpu_limit: str = os.environ.get("DSE_SANDBOX_CPU_LIMIT", "1")
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


def maven_proxy_settings_xml(
    egress_proxy_url: str,
    *,
    feed_id: str = "",
    feed_username: str = "",
    feed_token: str = "",
) -> str:
    """Maven's resolver honors NEITHER the http(s)_proxy env vars NOR the
    -Dhttps.proxyHost system properties — a `<proxies>` block in settings.xml is
    the only channel it respects. Without it every artifact download from a
    sandbox Pod dies with "Network is unreachable" (default-deny NetworkPolicy).
    nonProxyHosts mirrors NO_PROXY above.

    `feed_*` (2026-08-18): repositório privado do cliente. O `<servers>` é, pelo
    mesmo motivo do `<proxies>`, o único canal de credencial que o resolver lê.
    O `feed_id` TEM de ser o `id` do `<repository>` do POM — o Maven casa
    credencial por id, e um nome diferente é ignorado sem aviso, produzindo o
    mesmo 403 de antes com a credencial presente. Vazio = nada de `<servers>`:
    tenant sem feed privado gera exatamente o documento de sempre.
    """
    parsed = urllib.parse.urlparse(egress_proxy_url)
    host, port = parsed.hostname or "", parsed.port or 8806
    non_proxy = "localhost|127.0.0.1|*.svc|*.cluster.local"
    proxies = "".join(
        f"<proxy><id>dse-egress-{scheme}</id><active>true</active>"
        f"<protocol>{scheme}</protocol><host>{host}</host><port>{port}</port>"
        f"<nonProxyHosts>{non_proxy}</nonProxyHosts></proxy>"
        for scheme in ("https", "http")
    )
    servers = ""
    if feed_id and feed_token:
        # `escape`: o PAT é gerado por terceiro e pode conter `&`, `<`, `>`. Um
        # deles cru quebraria o XML, e o Maven falharia com erro de parse — que
        # seria diagnosticado como qualquer outra coisa menos a credencial.
        servers = (
            "<servers><server>"
            f"<id>{xml_escape(feed_id)}</id>"
            f"<username>{xml_escape(feed_username or 'dse')}</username>"
            f"<password>{xml_escape(feed_token)}</password>"
            "</server></servers>"
        )
    return (
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">'
        f"{servers}<proxies>{proxies}</proxies></settings>"
    )


def _label_value(v: str) -> str:
    """K8s label value: at most 63 chars, not ending in -/_/.

    The real work_item_id is `wi_` + sha256 (64 hex) = 67 chars, which blows the
    limit and makes the Pod's `kubectl apply` fail (invalid metadata.labels). We
    truncate while preserving the recognizable prefix. This label is
    INFORMATIONAL — no selector uses it (Pods are addressed via pod_name_for)."""
    return v[:63].rstrip("-_.")


def build_pod_manifest(request: SandboxProvisionRequest, cfg: K8sSandboxConfig | None = None) -> dict[str, Any]:
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
                    {"name": "MAVEN_OPTS", "value": "-Duser.home=/tmp"},
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
    def supports_isolated_stage_execution(self) -> bool:
        return True

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
        self._kubectl(["wait", "--for=condition=Ready", f"pod/{name}", "-n", self._cfg.namespace, "--timeout=120s"])
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
