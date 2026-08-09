"""G-1/G-2/G-3 — as três camadas medidas na autópsia do preview (2026-08-09,
4/4 PRs desde o gate degradadas):

  G-1  o clone é anônimo (argocd.py: "PUBLIC repos only", documentado) e os
       repos migraram para org privada → `could not read Username`, CrashLoop,
       300s, degraded — 4 de 4. Fix: deploy key read-only POR REPO, montada
       como secret no namespace, clone via SSH. O limite de segurança do
       docstring é respeitado por construção: a key não alcança nada além do
       próprio repo.
  G-2  a receita é npm-only para todo kind — um preview `deployable` (BE Java)
       morreria no `npm install` mesmo com auth. Fix: a receita segue o kind
       do paths-filter; deployable = o comando de build do PRÓPRIO
       .dse/validation.json (a régua do L1 — jq -r '.commands.build[2]'),
       jar + Postgres efêmero no namespace (Flyway migra no boot). De carona:
       o kind entra nos details do preview_triggered (buraco de
       observabilidade medido).
  G-3  o FE usa caminhos relativos (/api/v1/...) sem env, sem proxy, sem
       build de produção → shell com 404 em tudo. Fix: proxy.conf GERADO no
       deploy (nunca commitado no repo do cliente): degrau 2 quando o irmão
       de group_id tem preview vivo (svc in-cluster derivado do namespace);
       degrau 1 (fallback) para o alvo estável configurado.

Vermelho antes de cada uma.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dse_contracts.activities import TriggerPreviewInput
from dse_validation.config import PreviewConfig
from dse_validation.preview import argocd
from dse_validation.preview.argocd import _source_deployment, build_manifests

_FE_REPO = "fintexinc/bmo-fee-calculator-fe-dse"
_BE_REPO = "fintexinc/bmo-fee-calculator-be-dse"
_LABELS = "    app.kubernetes.io/managed-by: dse-preview\n"


def _cfg() -> PreviewConfig:
    cfg = PreviewConfig()
    cfg.mode = "source"
    return cfg


def test_g1_private_repo_clone_is_authenticated_by_deploy_key():
    """4/4 medido: `fatal: could not read Username` — clone HTTPS anônimo
    contra org privada. O clone vai por SSH com a deploy key read-only do
    repo, montada de secret no namespace do preview."""
    y = _source_deployment(
        "preview-wi-x", _LABELS, _cfg(), repo=_FE_REPO, branch="dse/wi_x"
    )
    assert f"git@github.com:{_FE_REPO}.git" in y, (
        "clone via SSH — HTTPS anônimo é a camada 1 medida da degradação"
    )
    assert "GIT_SSH_COMMAND" in y, "a key montada precisa ser usada pelo git"
    assert "dse-preview-deploy-key" in y, "a secret da deploy key montada no pod"


def test_g2_deployable_kind_builds_with_the_repos_own_ruler():
    """O BE (kind=deployable) morre hoje no `npm install`. A receita segue o
    kind: build = o comando do .dse/validation.json do repo (a régua que o L1
    já usa — commands.build é ['sh','-c','./mvnw -B -q ... package'] no
    testbed, extraído com jq), jar rodando, readiness por socket."""
    y = _source_deployment(
        "preview-wi-y", _LABELS, _cfg(), repo=_BE_REPO, branch="dse/wi_y",
        kind="deployable",
    )
    assert "jq -r '.commands.build[2]'" in y, (
        "o build é o do manifesto do repo — nunca uma segunda receita"
    )
    assert "java -jar" in y
    assert "npm install" not in y, "npm não pertence à receita deployable"
    assert "tcpSocket" in y, "readiness por socket — actuator é opcional no repo"
    assert "SPRING_DATASOURCE_URL" in y, "o app aponta para o Postgres efêmero"


def test_g2_deployable_manifests_include_ephemeral_postgres():
    ms = build_manifests(
        "preview-wi-y", "wi_y", "dev-tenant",
        datetime.now(timezone.utc) + timedelta(hours=1), 3600, _cfg(),
        repo=_BE_REPO, branch="dse/wi_y", kind="deployable",
    )
    joined = "\n".join(ms.values())
    assert "image: postgres:" in joined, "Postgres efêmero no namespace"
    assert "name: postgres" in joined, "Service `postgres` para o datasource"


def test_g2_kind_is_recorded_in_the_ledger(tmp_path):
    """Buraco de observabilidade medido: o kind (ui/deployable) do paths-filter
    não chegava ao ledger — o `preview_triggered` do workflow lê `preview.kind`,
    então o core tem que devolvê-lo mesmo no caminho DEGRADADO (o 4/4 medido).
    Antes do fix o PreviewRef degradado vinha com kind=''."""
    cfg = _cfg()
    cfg.kube_context = "k3d-cluster-that-does-not-exist"
    cfg.repo_dir = str(tmp_path / "repo")
    cfg.sync_timeout_s = 5
    import uuid
    ref = argocd.trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=f"wi_kind_{uuid.uuid4().hex[:10]}", tenant_id="dev-tenant",
            repo=_FE_REPO, pr_number=17,
            files_changed=["src/app/admin/grid-payout/grid-payout.component.html"],
        ),
        cfg=cfg,
    )
    assert ref.status == "degraded", "sem cluster, degrada (como em produção)"
    assert ref.kind == "ui", "o kind do paths-filter viaja no PreviewRef até o ledger"


def test_g3_fe_proxy_targets_the_live_sibling_or_the_fallback():
    """O FE chama /api/v1/... relativo — no preview, 404 em tudo. Degrau 2: o
    irmão de group_id com preview vivo é o backend, via svc in-cluster
    derivado do namespace. Degrau 1: o alvo estável configurado. Nota medida:
    hoje NÃO existe API estável na VPS — o fallback nasce configurável e
    aponta para onde o operador mandar."""
    cfg = _cfg()
    assert argocd.resolve_fe_api_target(
        "preview-wi-sib", True, cfg
    ) == "http://preview.preview-wi-sib.svc.cluster.local"
    cfg.fe_api_fallback = "http://stable.example"
    assert argocd.resolve_fe_api_target(None, False, cfg) == "http://stable.example"
    cfg.fe_api_fallback = ""
    assert argocd.resolve_fe_api_target(None, False, cfg) is None


def test_g3_ui_recipe_writes_the_generated_proxy_conf():
    y = _source_deployment(
        "preview-wi-z", _LABELS, _cfg(), repo=_FE_REPO, branch="dse/wi_z",
        kind="ui", api_proxy_target="http://preview.preview-wi-sib.svc.cluster.local",
    )
    assert "proxy.preview.json" in y, "proxy GERADO no deploy, nunca commitado no repo"
    assert "--proxy-config" in y
    assert "http://preview.preview-wi-sib.svc.cluster.local" in y
    y_no_target = _source_deployment(
        "preview-wi-z", _LABELS, _cfg(), repo=_FE_REPO, branch="dse/wi_z",
        kind="ui", api_proxy_target=None,
    )
    assert "--proxy-config" not in y_no_target, "sem alvo, sem proxy — nunca um alvo inventado"
