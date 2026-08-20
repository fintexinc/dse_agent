"""Plan 08 §G — security CONFORMANCE suite for the KubernetesSandboxDriver.

Validates that the generated Pod spec is hardened — WITHOUT needing a cluster
(the `build_pod_manifest` core is pure). The LIVE proof (a Pod running under
gVisor/Kata) depends on the cluster; these tests pin the POSTURE in CI so that
nobody can loosen the spec without breaking here.
"""
from __future__ import annotations

import importlib

import pytest

from sandbox_runtime import k8s_driver
from sandbox_runtime.driver import (
    IsolatedStageExecutionUnavailable,
    SandboxProvisionRequest,
    StageExecutionRequest,
)
from sandbox_runtime.k8s_driver import (
    K8sSandboxConfig,
    KubernetesSandboxDriver,
    build_pod_manifest,
)
from dse_contracts import Stage


def _req():
    return SandboxProvisionRequest(
        work_item_id="wi_abc123", tenant_id="tenant_dev", branch="dse/wi_abc123",
        workspace_path="/w", checkpoint_path="/c",
    )


def _pod(**cfg_over):
    cfg = K8sSandboxConfig(**cfg_over)
    return build_pod_manifest(_req(), cfg)


def _pod_from_env(monkeypatch, **env):
    """K8sSandboxConfig resolves env in dataclass field defaults, i.e. at IMPORT
    time — so setting the variable and calling the config does nothing. The only
    honest way to prove an env knob reaches the manifest is to re-import with
    the env set, which is what the worker process does on startup."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reloaded = importlib.reload(k8s_driver)
    try:
        return reloaded.build_pod_manifest(_req(), reloaded.K8sSandboxConfig())
    finally:
        monkeypatch.undo()
        importlib.reload(k8s_driver)


_QUANTITY_UNITS = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3}


def _as_bytes(quantity: str) -> int:
    for unit, factor in _QUANTITY_UNITS.items():
        if quantity.endswith(unit):
            return int(quantity[: -len(unit)]) * factor
    return int(quantity)


def test_pod_and_container_run_as_nonroot_uid():
    pod = _pod()
    psec = pod["spec"]["securityContext"]
    csec = pod["spec"]["containers"][0]["securityContext"]
    assert psec["runAsNonRoot"] is True and psec["runAsUser"] != 0
    assert csec["runAsNonRoot"] is True and csec["runAsUser"] != 0


def test_no_privilege_escalation_and_caps_dropped():
    csec = _pod()["spec"]["containers"][0]["securityContext"]
    assert csec["allowPrivilegeEscalation"] is False
    assert csec["privileged"] is False
    assert csec["capabilities"]["drop"] == ["ALL"]


def test_readonly_rootfs_with_writable_scratch():
    pod = _pod()
    c = pod["spec"]["containers"][0]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    mounts = {m["name"] for m in c["volumeMounts"]}
    assert {"workspace", "tmp"} <= mounts
    vols = {v["name"]: v for v in pod["spec"]["volumes"]}
    # writable scratch via emptyDir (not hostPath)
    assert "emptyDir" in vols["workspace"] and "emptyDir" in vols["tmp"]


def test_seccomp_runtime_default():
    pod = _pod()
    assert pod["spec"]["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["spec"]["containers"][0]["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"


def test_no_host_namespaces_or_socket():
    spec = _pod()["spec"]
    assert spec["hostNetwork"] is False
    assert spec["hostPID"] is False
    assert spec["hostIPC"] is False
    # no hostPath volume at all (the Docker socket in particular)
    for v in spec["volumes"]:
        assert "hostPath" not in v


def test_service_account_token_not_mounted():
    spec = _pod()["spec"]
    assert spec["automountServiceAccountToken"] is False


def test_egress_routed_through_proxy():
    env = {e["name"]: e["value"] for e in _pod()["spec"]["containers"][0]["env"]}
    assert env["HTTP_PROXY"].startswith("http")
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"]


def test_runtime_class_set_when_configured():
    pod = _pod(runtime_class="gvisor")
    assert pod["spec"]["runtimeClassName"] == "gvisor"


def test_missing_runtime_class_is_flagged_not_silent():
    # weak isolation (no RuntimeClass) is NEVER silent — it marks an annotation
    pod = _pod(runtime_class="")
    assert "runtimeClassName" not in pod["spec"]
    assert any("isolation-warning" in k for k in pod["metadata"]["annotations"])


def test_ephemeral_pod_never_restarts():
    assert _pod()["spec"]["restartPolicy"] == "Never"


def test_resource_limits_present():
    resources = _pod()["spec"]["containers"][0]["resources"]
    # ephemeral-storage belongs here with cpu/memory: a sandbox that fills the
    # node's disk must be evicted by ITS OWN limit, not by node-level pressure
    # picking a victim — and a Pod that requests 0 disk is the first victim.
    assert {"cpu", "memory", "ephemeral-storage"} <= set(resources["limits"])
    assert {"cpu", "memory", "ephemeral-storage"} <= set(resources["requests"])


def test_ephemeral_storage_request_fits_under_its_limit():
    # k8s rejects a Pod whose request exceeds its limit; the defaults must be a
    # spec that actually applies.
    resources = _pod()["spec"]["containers"][0]["resources"]
    request = _as_bytes(resources["requests"]["ephemeral-storage"])
    limit = _as_bytes(resources["limits"]["ephemeral-storage"])
    assert 0 < request <= limit


def test_emptydir_size_limits_stay_within_the_pod_ephemeral_budget():
    """The two caps must tell the same story. The kubelet bills the SUM of the
    emptyDirs against the container's ephemeral-storage limit, so emptyDirs
    whose sizeLimits add up to more than that limit would let the Pod die of the
    total while every single volume still looks healthy. No sizeLimit is set
    yet; this holds the invariant for whoever sets the first one."""
    pod = _pod()
    limit = _as_bytes(pod["spec"]["containers"][0]["resources"]["limits"]["ephemeral-storage"])
    declared = sum(
        _as_bytes(v["emptyDir"]["sizeLimit"])
        for v in pod["spec"]["volumes"]
        if "sizeLimit" in v.get("emptyDir", {})
    )
    assert declared <= limit


def test_ephemeral_storage_read_from_env(monkeypatch):
    pod = _pod_from_env(
        monkeypatch,
        DSE_SANDBOX_EPHEMERAL_STORAGE_LIMIT="7Gi",
        DSE_SANDBOX_EPHEMERAL_STORAGE_REQUEST="3Gi",
    )
    resources = pod["spec"]["containers"][0]["resources"]
    assert resources["limits"]["ephemeral-storage"] == "7Gi"
    assert resources["requests"]["ephemeral-storage"] == "3Gi"


def test_blank_ephemeral_storage_env_falls_back_to_the_default(monkeypatch):
    # A chart value rendering as "" is trivially produced; an empty quantity
    # makes `kubectl apply` reject the Pod, so it must degrade to the default
    # rather than emit a manifest that cannot be applied at all.
    pod = _pod_from_env(
        monkeypatch,
        DSE_SANDBOX_EPHEMERAL_STORAGE_LIMIT="  ",
        DSE_SANDBOX_EPHEMERAL_STORAGE_REQUEST="",
    )
    resources = pod["spec"]["containers"][0]["resources"]
    assert _as_bytes(resources["limits"]["ephemeral-storage"]) > 0
    assert _as_bytes(resources["requests"]["ephemeral-storage"]) > 0


def test_fail_closed_without_kubectl():
    # with no kubectl on PATH, provision/execute FAIL (they never run locally)
    cfg = K8sSandboxConfig(kubectl="kubectl-that-does-not-exist-xyz")
    driver = KubernetesSandboxDriver(cfg)
    with pytest.raises(IsolatedStageExecutionUnavailable):
        driver.execute_stage(StageExecutionRequest(
            sandbox_id="dse-sbx-x", work_item_id="wi", tenant_id="t",
            stage=Stage.coder, input_payload={}, timeout_seconds=5,
        ))
