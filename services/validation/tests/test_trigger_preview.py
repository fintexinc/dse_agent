"""WSE-E4-T10 — per-PR previews. Two layers of tests:

  1. Deterministic logic (FR-20 paths-filter, ADR-26 caps, degraded on failure)
     — fast, against real Postgres (never touches the cluster).
  2. REAL integration against the `dse-preview` k3d cluster (Argo CD v2.13.3 +
     ApplicationSet + git smart HTTP from docker-compose.wse.yml): namespace
     created, URL responding, TTL destroying it via the GitOps reaper. Flagged as
     the slowest test in the suite (~2-4min) — it is the phase's exit criterion.
"""
from __future__ import annotations

import pytest

from dse_contracts.activities import TriggerPreviewInput

from dse_validation import db
from dse_validation.config import PreviewConfig
from dse_validation.preview.argocd import (
    get_preview_http_status,
    namespace_for,
    reap_expired_previews,
    trigger_preview_core,
    wait_namespace_gone,
)
from dse_validation.preview.paths_filter import (
    file_matches_glob,
    is_ui_touching,
    preview_decision,
)

DEFAULT_GLOBS = ["ui/**", "frontend/**", "**/*.css", "**/*.tsx", "**/*.jsx"]
DEPLOYABLE_GLOBS = [
    "**/Dockerfile", "Dockerfile", "**/*.py", "**/*.go", "**/*.rb",
    "**/*.java", "**/*.ts", "**/*.js", "k8s/**", "deploy/**", "charts/**",
    "**/requirements*.txt", "pyproject.toml", "go.mod", "package.json",
]


# ---------------------------------------------------------------------------
# 1a. FR-20 paths-filter (purely deterministic)
# ---------------------------------------------------------------------------
def test_backend_only_files_do_not_touch_ui():
    assert not is_ui_touching(["api/handler.py", "README.md", "migrations/0001.sql"], DEFAULT_GLOBS)


def test_ui_globs_match_expected_shapes():
    assert is_ui_touching(["frontend/app.tsx"], DEFAULT_GLOBS)
    assert is_ui_touching(["ui/components/button/index.js"], DEFAULT_GLOBS)  # nested ui/**
    assert is_ui_touching(["styles/app.css"], DEFAULT_GLOBS)  # **/*.css inside a directory
    assert is_ui_touching(["app.css"], DEFAULT_GLOBS)  # **/*.css at the ROOT (documented tweak)
    assert not is_ui_touching([], DEFAULT_GLOBS)


def test_glob_semantics_documented():
    assert file_matches_glob("ui/a/b/c.js", "ui/**")
    assert file_matches_glob("web/x.tsx", "**/*.tsx")
    assert file_matches_glob("x.tsx", "**/*.tsx")
    assert not file_matches_glob("api/x.py", "ui/**")


def test_docs_only_pr_skips_and_counts_as_success(work_item_id, tenant_id):
    # plan 08 §D: docs only (neither UI nor a deployable service) → skip, counts
    # as success, NEVER blocks. (Previously a backend .py also skipped; now it
    # gets a preview — see test_backend_service_change_now_previews.)
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=11, files_changed=["docs/x.md", "README.md", "CHANGELOG.md"],
        )
    )
    assert ref.status == "skipped_backend_only"  # counts as success, NEVER blocks
    row = db.get_preview(work_item_id)
    assert row["status"] == "skipped_backend_only"


# ---------------------------------------------------------------------------
# plan 08 §D — previewability decision (ui | deployable | none)
# ---------------------------------------------------------------------------
def test_preview_decision_ui_has_precedence():
    kind, hits = preview_decision(["frontend/app.tsx", "api/main.py"], DEFAULT_GLOBS, DEPLOYABLE_GLOBS)
    assert kind == "ui" and "frontend/app.tsx" in hits


def test_preview_decision_backend_service_is_deployable():
    kind, hits = preview_decision(["wallet/service.py", "Dockerfile"], DEFAULT_GLOBS, DEPLOYABLE_GLOBS)
    assert kind == "deployable" and hits


def test_preview_decision_docs_only_is_none():
    kind, hits = preview_decision(["docs/x.md", "README.md"], DEFAULT_GLOBS, DEPLOYABLE_GLOBS)
    assert kind == "none" and hits == []


def test_deploys_preview_gate_disabled_skips_without_touching_cluster(work_item_id, tenant_id):
    # repo not flagged deploys_preview → skipped_disabled at step 0 (before any
    # contact with the cluster). Proof: invalid kube_context, no error.
    cfg = PreviewConfig()
    cfg.kube_context = "invalid-context-proves-it-does-not-touch-the-cluster"
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=15, files_changed=["frontend/app.tsx"], preview_enabled=False,
        ),
        cfg=cfg,
    )
    assert ref.status == "skipped_disabled"
    assert db.get_preview(work_item_id)["status"] == "skipped_disabled"


def test_backend_service_change_now_previews_reaches_provision(work_item_id, tenant_id, tmp_path):
    # D2: a backend service PR (.py) is NO LONGER skipped as backend-only — it
    # passes the paths-filter and reaches provisioning (which degrades here, with
    # no cluster; the point is that it did NOT stop at skipped_backend_only).
    cfg = PreviewConfig()
    cfg.kube_context = "k3d-cluster-that-does-not-exist"
    cfg.repo_dir = str(tmp_path / "repo")
    cfg.sync_timeout_s = 5
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/svc",
            pr_number=16, files_changed=["wallet/service.py"],
        ),
        cfg=cfg,
    )
    assert ref.status == "degraded"  # reached provisioning (did not skip)
    assert ref.status != "skipped_backend_only"


def test_external_url_is_browser_reachable_when_configured():
    cfg = PreviewConfig()
    cfg.external_host_template = "https://{namespace}.preview.dse.local"
    assert cfg.preview_url_for("preview-wi-1") == "https://preview-wi-1.preview.dse.local"
    # no template → internal DNS (the link still shows up on the PR — D1 — but is not clickable)
    cfg2 = PreviewConfig()
    cfg2.external_host_template = ""
    assert cfg2.preview_url_for("preview-wi-1").endswith(".svc.cluster.local")


# ---------------------------------------------------------------------------
# plan 08 §D — D3 (Ingress) + D4 (PR image) in the manifests
# ---------------------------------------------------------------------------
def _manifests(cfg, **kw):
    from datetime import datetime, timedelta, timezone
    from dse_validation.preview.argocd import build_manifests
    exp = datetime.now(timezone.utc) + timedelta(seconds=600)
    return build_manifests("preview-wi-x", "wi-x", "tenant_dev", exp, 600, cfg, **kw)


def test_ingress_generated_with_hostname_from_template():
    cfg = PreviewConfig()
    cfg.external_host_template = "http://{namespace}.preview.localhost:8081"
    m = _manifests(cfg)
    assert "ingress.yaml" in m
    assert "host: preview-wi-x.preview.localhost" in m["ingress.yaml"]  # no scheme/port
    assert "ingressClassName: traefik" in m["ingress.yaml"]


def test_no_ingress_without_external_host():
    cfg = PreviewConfig()
    cfg.external_host_template = ""
    assert "ingress.yaml" not in _manifests(cfg)


def test_label_values_respect_k8s_63_char_limit():
    """Real run (issue #2 → PR #10): a work_item_id of `wi_`+64 hex = 67 chars
    became a label value and k8s REJECTED the namespace (label value ≤ 63) — the
    preview degraded with no URL. The full id goes in the annotation; the label is
    truncated."""
    import re
    from datetime import datetime, timedelta, timezone
    from dse_validation.preview.argocd import build_manifests

    wi = "wi_" + "a" * 64  # 67 chars, like the real id
    cfg = PreviewConfig()
    exp = datetime.now(timezone.utc) + timedelta(seconds=600)
    m = build_manifests("preview-wi-x", wi, "tenant_dev", exp, 600, cfg)

    label_re = re.compile(r'dse\.fintex/work-item:\s+"([^"]*)"')
    for name, manifest in m.items():
        for val in label_re.findall(manifest):
            assert len(val) <= 63, f"the label in {name} has {len(val)} chars (>63)"
    # the FULL id must be preserved in the namespace's annotation
    assert f'dse.fintex/work-item-id: "{wi}"' in m["namespace.yaml"]


def test_pr_image_and_app_port_flow_into_deployment():
    cfg = PreviewConfig()
    cfg.app_port = 3000
    m = _manifests(cfg, image="k3d-dse-registry:5510/dse-preview/wallet:abc123")
    assert "image: k3d-dse-registry:5510/dse-preview/wallet:abc123" in m["deployment.yaml"]
    assert "containerPort: 3000" in m["deployment.yaml"]
    assert "targetPort: 3000" in m["service.yaml"]  # Service publishes 80 → app_port


def test_build_pr_image_fail_safe_reasons(tmp_path, monkeypatch):
    from dse_validation.preview.pr_image import build_pr_image
    cfg = PreviewConfig()
    # flag off → placeholder
    cfg.build_image = False
    ref, reason, port = build_pr_image(work_item_id="wi-nope", repo="a/b", head_sha="s", cfg=cfg)
    assert ref is None and reason == "build_disabled" and port is None
    # on but with no workspace → placeholder with a reason
    cfg.build_image = True
    monkeypatch.setenv("DSE_SANDBOX_STATE_DIR", str(tmp_path))
    ref, reason, port = build_pr_image(work_item_id="wi-nope", repo="a/b", head_sha="s", cfg=cfg)
    assert ref is None and reason.startswith("workspace_not_found")
    # workspace with no Dockerfile AND no package.json → placeholder (not Node)
    ws = tmp_path / "wi-nodf" / "workspace"
    ws.mkdir(parents=True)
    ref, reason, port = build_pr_image(work_item_id="wi-nodf", repo="a/b", head_sha="s", cfg=cfg)
    assert ref is None and reason == "no_dockerfile_and_not_node" and port is None


def test_synthesize_node_dockerfile_for_app_without_dockerfile(tmp_path):
    """Operator decision (2026-07-22): a Node app with no Dockerfile → DSE
    synthesizes a default Dockerfile (without touching the repo) and detects port 3000."""
    import json as _json
    from dse_validation.preview.pr_image import _synthesize_node_dockerfile, _DEFAULT_NODE_PORT

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text(_json.dumps({
        "type": "module", "main": "server.js",
        "scripts": {"start": "node server.js"}, "dependencies": {},
    }))
    path = _synthesize_node_dockerfile(str(ws), _DEFAULT_NODE_PORT)
    assert path is not None
    content = open(path).read()
    assert "FROM node:22-alpine" in content
    assert f"ENV PORT={_DEFAULT_NODE_PORT}" in content
    assert f"EXPOSE {_DEFAULT_NODE_PORT}" in content
    assert 'CMD ["npm", "start"]' in content
    # TEMPORARY file — outside the workspace (does not pollute the task's git)
    assert str(ws) not in path
    import os as _os
    _os.remove(path)


def test_synthesize_falls_back_to_node_main_without_start_script(tmp_path):
    import json as _json
    from dse_validation.preview.pr_image import _synthesize_node_dockerfile, _DEFAULT_NODE_PORT

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text(_json.dumps({"main": "app.js", "scripts": {}}))
    path = _synthesize_node_dockerfile(str(ws), _DEFAULT_NODE_PORT)
    assert path is not None
    assert 'CMD ["node", "app.js"]' in open(path).read()
    import os as _os
    _os.remove(path)


def test_synthesize_returns_none_when_not_node(tmp_path):
    from dse_validation.preview.pr_image import _synthesize_node_dockerfile, _DEFAULT_NODE_PORT
    ws = tmp_path / "ws"
    ws.mkdir()  # no package.json
    assert _synthesize_node_dockerfile(str(ws), _DEFAULT_NODE_PORT) is None


# ---------------------------------------------------------------------------
# D1 — preview link IN THE PR's DESCRIPTION (not as a comment, nor only on the issue)
# ---------------------------------------------------------------------------
_PR_BODY_BASE = (
    "### Fintex DSE — automatically generated PR\n\n"
    "- **WorkItem**: `wi_x`\n"
    "- **Risk class**: `medium`\n"
    "- **Summary**: corrige o bug\n"
    "- **Test evidence (L1)**: (no evidence link)\n\n"
    "Closes #2\n"
)


class _FakePrBodyClient:
    def __init__(self, body):
        self.body = body
    def get_pull_request(self, repo, n):
        return {"number": n, "state": "open", "body": self.body}
    def update_pull_request(self, repo, n, *, body):
        self.body = body


def test_preview_link_written_into_pr_body_after_evidence(monkeypatch):
    """Operator request (2026-07-22): the preview link goes in the PR's
    DESCRIPTION, as `- **Preview**: <url>`, right after the L1 evidence bullet."""
    import dse_validation.preview.argocd as arg
    import dse_validation.github.client as gc

    fake = _FakePrBodyClient(_PR_BODY_BASE)
    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: fake)
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="a/b", pr_number=11,
        files_changed=["src/store.js"],
    )
    url = "http://preview-x.preview.localhost:8081"
    arg._put_preview_in_pr_body(
        inp, arg.preview_body_line("created", url=url), actor="system:test")
    body = fake.body
    assert f"- **Preview**: {url}" in body
    # positioned right after the L1 evidence
    ev = body.index("- **Test evidence (L1)**:")
    pv = body.index("- **Preview**:")
    assert ev < pv


def test_preview_link_in_body_is_idempotent(monkeypatch):
    """A re-trigger (fix cycle) REWRITES the same line — it does not duplicate it."""
    import dse_validation.preview.argocd as arg
    import dse_validation.github.client as gc

    fake = _FakePrBodyClient(_PR_BODY_BASE)
    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: fake)
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="a/b", pr_number=11,
        files_changed=["src/store.js"],
    )
    arg._put_preview_in_pr_body(
        inp, arg.preview_body_line("created", url="http://old.localhost"), actor="t")
    arg._put_preview_in_pr_body(
        inp, arg.preview_body_line("created", url="http://new.localhost"), actor="t")
    assert fake.body.count("- **Preview**:") == 1  # a single line
    assert "http://new.localhost" in fake.body and "http://old.localhost" not in fake.body


def test_preview_link_body_noop_without_pr_number(monkeypatch):
    """Sem PR não há onde escrever — mas desde 2026-08-11 isso DEIXA RASTRO.
    Silêncio era o modo de falha: sem o evento, "a PR não recebeu o preview" e
    "o preview nunca rodou" ficam indistinguíveis no ledger."""
    import dse_validation.preview.argocd as arg
    import dse_validation.github.client as gc
    fake = _FakePrBodyClient(_PR_BODY_BASE)
    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: fake)
    emitidos: list[str] = []
    monkeypatch.setattr(arg, "audit_emit", lambda **kw: emitidos.append(kw.get("action", "")))
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="a/b", pr_number=0,
        files_changed=["src/store.js"],
    )
    arg._put_preview_in_pr_body(
        inp, arg.preview_body_line("created", url="http://x"), actor="t")
    assert fake.body == _PR_BODY_BASE  # pr_number=0 → não toca o corpo
    assert "preview_line_in_pr_body_failed" in emitidos


# ---------------------------------------------------------------------------
# 1b. per-tenant concurrency caps (ADR-26, day 1)
# ---------------------------------------------------------------------------
def test_concurrency_cap_evicts_oldest_lru(work_item_id, tenant_id, tmp_path):
    """Cap full => LRU eviction: the OLDEST preview yields its slot to the new PR
    (operator decision 2026-07-23). The cap gate stops degrading when there is
    something to evict — here the flow gets past the cap and only degrades LATER,
    at provisioning (nonexistent kube_context), proving the slot was freed."""
    db.set_preview_cap(tenant_id, 2)
    # two active "created" rows for the SAME tenant (real count in Postgres);
    # -0 is the oldest (smaller created_at) — the eviction candidate.
    for i in range(2):
        db.upsert_preview(
            work_item_id=f"{work_item_id}-{i}", tenant_id=tenant_id, pr_number=i,
            repo="acme/app", status="created", namespace=f"preview-x-{i}",
        )
    assert db.count_active_previews(tenant_id) == 2
    cfg = PreviewConfig()
    cfg.repo_dir = str(tmp_path / "preview-repo")  # real gitops in a temp repo
    cfg.kube_context = "k3d-cluster-that-does-not-exist"
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=12, files_changed=["frontend/app.tsx"],
        ),
        cfg=cfg,
    )
    # the oldest was reaped (slot freed); the newest is still active
    assert db.get_preview(f"{work_item_id}-0")["status"] == "reaped"
    assert db.get_preview(f"{work_item_id}-1")["status"] == "created"
    # and the flow got PAST the cap gate (degraded here comes from the nonexistent cluster)
    assert ref.status == "degraded"
    assert "cap" not in ref.detail


def test_concurrency_cap_degrades_when_eviction_fails(work_item_id, tenant_id, tmp_path, monkeypatch):
    """A failed removal must still free the slot.

    This test used to assert the opposite — "nothing was marked reaped without a
    real removal" — which reads as prudence and was the bug. The reaper works
    from cluster state and the namespace's own expiry annotation and never reads
    `wse_previews`, so a row that is never released is not caution: it is a slot
    lost for good. Three of them expired on a Saturday, the reaper deleted their
    namespaces, the removal then raised because the directory was already gone,
    the eviction aborted, and previews stayed off for the tenant from that day
    on.

    The trade is deliberate and asymmetric. Freeing the slot risks leaking one
    namespace until its TTL expires — which the reaper collects anyway. Not
    freeing it disables the feature permanently.
    """
    db.set_preview_cap(tenant_id, 1)
    db.upsert_preview(
        work_item_id=f"{work_item_id}-old", tenant_id=tenant_id, pr_number=1,
        repo="acme/app", status="created", namespace="preview-x-old",
    )
    from dse_validation.preview import gitops as gitops_mod

    def _boom(repo_dir, name):
        raise RuntimeError("git unavailable")

    # argocd resolves `gitops.remove_preview_dir` at call time — patching the module is enough.
    monkeypatch.setattr(gitops_mod, "remove_preview_dir", _boom)
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=14, files_changed=["frontend/app.tsx"],
        )
    )
    # The slot IS released, so the cap is no longer what stops this preview.
    # It may still degrade further down (there is no cluster in this test), but
    # never again for `concurrency_cap`.
    assert db.get_preview(f"{work_item_id}-old")["status"] == "reaped"
    assert "cap" not in (ref.detail or "")


def test_an_expired_preview_does_not_hold_a_slot(work_item_id, tenant_id):
    """The reaper deletes namespaces on TTL and never writes to this table, so a
    row past its expiry names a namespace that no longer exists. Counting it
    against the cap is how the feature switched itself off after three uses."""
    from datetime import datetime, timedelta, timezone

    db.set_preview_cap(tenant_id, 1)
    db.upsert_preview(
        work_item_id=f"{work_item_id}-dead", tenant_id=tenant_id, pr_number=1,
        repo="acme/app", status="created", namespace="preview-x-dead",
        # `expires_at` is stored verbatim, not derived from ttl_seconds — so an
        # expired row has to be written as one.
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert db.count_active_previews(tenant_id) == 0, "an expired preview still held a slot"

    # And a live one does hold its slot — the cap is still a real ceiling.
    db.upsert_preview(
        work_item_id=f"{work_item_id}-live", tenant_id=tenant_id, pr_number=2,
        repo="acme/app", status="created", namespace="preview-x-live",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert db.count_active_previews(tenant_id) == 1

    # Eviction takes the dead one first: evicting a preview somebody is looking
    # at, while a corpse sits next to it, would be gratuitous.
    oldest = db.list_oldest_active_previews(tenant_id, limit=1)
    assert oldest[0]["work_item_id"] == f"{work_item_id}-dead"


def test_cap_zero_blocks_immediately_without_touching_cluster(work_item_id, tenant_id):
    db.set_preview_cap(tenant_id, 0)
    cfg = PreviewConfig()
    cfg.kube_context = "nonexistent-context-proves-it-does-not-touch-the-cluster"
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=13, files_changed=["ui/x.css"],
        ),
        cfg=cfg,
    )
    assert ref.status == "degraded"  # and it raised no kubectl error => it never called the cluster


# ---------------------------------------------------------------------------
# 1c. failure mode 9 — preview failure => degraded (the PR is never blocked)
# ---------------------------------------------------------------------------
def test_cluster_failure_degrades_instead_of_blocking(work_item_id, tenant_id, tmp_path):
    cfg = PreviewConfig()
    cfg.kube_context = "k3d-cluster-that-does-not-exist"
    cfg.repo_dir = str(tmp_path / "repo")  # isolated repo (does not pollute the real one)
    cfg.sync_timeout_s = 5
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=14, files_changed=["frontend/app.tsx"],
        ),
        cfg=cfg,
    )
    assert ref.status == "degraded"
    assert ref.detail  # explicit reason (P6/P8)
    row = db.get_preview(work_item_id)
    assert row["status"] == "degraded"


# ---------------------------------------------------------------------------
# 2. REAL integration against the k3d cluster (the phase's exit criterion)
# ---------------------------------------------------------------------------
def _k3d_cluster_available() -> bool:
    """Requires the FULL capability: an active k3d cluster AND Argo CD installed
    (the test materializes a real Application). There is no cluster on the CI
    runner; on a k3d without Argo CD it also skips — the proof only runs in the
    complete environment (dev/VPS with Argo CD)."""
    import shutil
    import subprocess

    if shutil.which("kubectl") is None:
        return False
    ctx = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True)
    if ctx.returncode != 0 or not ctx.stdout.strip().startswith("k3d-"):
        return False
    argo = subprocess.run(
        ["kubectl", "get", "namespace", "argocd"], capture_output=True, text=True
    )
    return argo.returncode == 0


@pytest.mark.skipif(not _k3d_cluster_available(), reason="requires an active k3d cluster (Argo CD)")
def test_preview_e2e_real_cluster_create_serve_and_ttl_reap(work_item_id, tenant_id):
    """Against the REAL k3d cluster: (a) Argo CD's ApplicationSet materializes the
    ephemeral namespace with Deployment+Service; (b) the preview URL answers
    HTTP 200; (c) TTL expired => the GitOps reaper removes it from git and the
    namespace is DESTROYED (prune + finalizer). ~2-4min."""
    ref = trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=15, files_changed=["frontend/pages/index.tsx", "api/x.py"],
        ),
        ttl_seconds=1800,
    )
    assert ref.status == "created", ref.detail
    namespace = namespace_for(work_item_id)
    assert ref.namespace == namespace
    assert ref.url and namespace in ref.url

    # (b) URL really responding (in-cluster curl probe against the Service)
    assert get_preview_http_status(namespace) == 200

    # (c) TTL: force expiration in the durable state and run the deterministic reaper
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE wse_previews SET expires_at = now() - interval '1 second' WHERE work_item_id = %s",
                (work_item_id,),
            )
        conn.commit()
    finally:
        conn.close()

    reaped = reap_expired_previews()
    assert work_item_id in reaped
    wait_namespace_gone(namespace, timeout_s=240)  # namespace really destroyed

    row = db.get_preview(work_item_id)
    assert row["status"] == "reaped" and row["reaped_at"] is not None

    # audit (P8): created + reaped
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM audit_log WHERE work_item_id = %s "
                "AND action IN ('preview_created','preview_reaped') ORDER BY id",
                (work_item_id,),
            )
            actions = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    assert actions == ["preview_created", "preview_reaped"]
