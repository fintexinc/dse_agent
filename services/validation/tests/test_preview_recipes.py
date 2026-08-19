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


def test_an_angular_component_ts_only_change_is_ui_not_deployable():
    """(2) — a lacuna de glob medida junto do wi_cc72b204: `.ts` está em
    `deployable_globs` e NÃO estava em `ui_path_globs` (que tinha .tsx/.jsx/
    .vue/.svelte, mas não o `.ts` do Angular). Um change só-de-`.component.ts`
    num FE Angular classificava como backend e ganhava a receita Java.

    O glob é `**/*.component.ts` — convenção inequívoca de Angular, então um
    backend Node em TypeScript (`src/server.ts`) segue `deployable`, que é o
    correto. Sobre os DEFAULTS DO CONTRATO, não sobre uma cópia local: é o que
    a produção usa."""
    from dse_contracts.activities import TriggerPreviewInput
    from dse_validation.preview.paths_filter import preview_decision

    defaults = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="o/r", pr_number=1, files_changed=[],
    )
    kind, _ = preview_decision(
        ["src/app/admin/grid-payout/grid-payout.component.ts"],
        defaults.ui_path_globs, defaults.deployable_globs,
    )
    assert kind == "ui", "componente Angular é UI, não serviço deployável"

    kind_be, _ = preview_decision(
        ["src/server.ts"], defaults.ui_path_globs, defaults.deployable_globs,
    )
    assert kind_be == "deployable", "backend Node em TS continua deployable"


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


def test_g1_the_private_key_never_enters_the_manifest_set():
    """A chave é material de credencial: o manifest set vira COMMIT no repo de
    manifestos em modo gitops. Por construção, não por disciplina — a secret
    por-namespace é materializada via kubectl, fora do set."""
    ms = build_manifests(
        "preview-wi-k", "wi_k", "dev-tenant",
        datetime.now(timezone.utc) + timedelta(hours=1), 3600, _cfg(),
        repo=_FE_REPO, branch="dse/wi_k", kind="ui",
    )
    joined = "\n".join(ms.values())
    assert "kind: Secret" not in joined, "nenhuma Secret no set que vai para git"
    assert "PRIVATE KEY" not in joined


def test_g1_materialize_creates_the_per_namespace_secret(monkeypatch):
    """A secret por-namespace nasce da agregada do namespace do DSE (que o
    OPERADOR semeia — runbook), item por repo."""
    calls: list[tuple[list[str], str | None]] = []

    def _fake_kubectl(cfg, args, *, input_text=None, timeout=60):
        calls.append((args, input_text))
        if args[:2] == ["get", "secret"]:
            return type("P", (), {"stdout": "QkFTRTY0S0VZ", "returncode": 0})()
        return type("P", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(argocd, "_kubectl", _fake_kubectl)
    ok = argocd.materialize_deploy_key(_cfg(), "preview-wi-k", _FE_REPO)
    assert ok is True
    read = [c for c in calls if c[0][:2] == ["get", "secret"]]
    assert read, "lê a secret agregada do namespace do DSE"
    assert "fintexinc__bmo-fee-calculator-fe-dse" in " ".join(read[0][0]), (
        "o item é o slug determinístico do repo"
    )
    applied = [c for c in calls if c[0][0] == "apply"]
    assert applied, "aplica a secret por-namespace"
    body = applied[0][1] or ""
    assert "kind: Secret" in body and "namespace: preview-wi-k" in body
    assert "QkFTRTY0S0VZ" in body, "o material vem da agregada, nunca gerado aqui"


def test_g1prime_without_a_seeded_secret_it_mints_an_installation_token(monkeypatch):
    """Decisão de operador (POC, 2026-08-09): sem deploy keys — o clone usa o
    installation token da App JÁ instalada. Sem secret semeada e com App
    configurada, a credencial do preview é um token MINTADO AGORA (expira em
    1h; o clone leva segundos, e um preview que viva mais já clonou)."""
    def _no_aggregate(cfg, args, *, input_text=None, timeout=60):
        if args[:2] == ["get", "secret"]:
            raise RuntimeError("NotFound")
        return type("P", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(argocd, "_kubectl", _no_aggregate)
    monkeypatch.setattr(argocd, "_mint_installation_token", lambda: "ghs_faketoken")
    mode, material = argocd.resolve_preview_credential(_cfg(), _FE_REPO)
    assert mode == "token"
    import base64
    assert base64.b64decode(material).decode() == "ghs_faketoken"


def test_g1prime_seeded_secret_still_wins_as_fallback(monkeypatch):
    """O caminho da secret fica: se o operador semear, usa (código da rc.55)."""
    def _has_aggregate(cfg, args, *, input_text=None, timeout=60):
        if args[:2] == ["get", "secret"]:
            return type("P", (), {"stdout": "U1NIS0VZ", "returncode": 0})()
        return type("P", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(argocd, "_kubectl", _has_aggregate)
    monkeypatch.setattr(argocd, "_mint_installation_token", lambda: "ghs_faketoken")
    mode, material = argocd.resolve_preview_credential(_cfg(), _FE_REPO)
    assert (mode, material) == ("ssh", "U1NIS0VZ"), "secret semeada tem precedência"


def test_g1prime_token_mode_never_puts_the_token_in_argv():
    """O token vai pela MESMA secret/volume da rc.55 — nunca no pod spec (que
    `kubectl get pod -o yaml` mostra) nem em argv. O git o lê do arquivo via
    credential helper, então também não fica no .git/config do clone."""
    y = _source_deployment(
        "preview-wi-t", _LABELS, _cfg(), repo=_FE_REPO, branch="dse/wi_t",
        auth_mode="token",
    )
    assert "https://github.com/" + _FE_REPO in y, "clone HTTPS com credential helper"
    assert "/preview-keys/token" in y, "o token vem do volume, não do spec"
    assert "credential.helper" in y
    assert "ghs_" not in y and "x-access-token:$" not in y, "nenhum token literal no spec"
    assert "GIT_SSH_COMMAND" not in y, "modo token não usa SSH"


def test_g1_no_credential_at_all_degrades_with_a_named_reason(monkeypatch, tmp_path):
    """Sem secret semeada E sem App configurada, o preview degrada NOMEANDO o
    motivo — não o `could not read Username` opaco de 300s medido 4/4."""
    def _fake_kubectl(cfg, args, *, input_text=None, timeout=60):
        if args[:2] == ["get", "secret"]:
            raise RuntimeError("kubectl get secret failed (exit=1): NotFound")
        return type("P", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(argocd, "_kubectl", _fake_kubectl)
    monkeypatch.setattr(argocd, "_mint_installation_token", lambda: None)
    cfg = _cfg()
    cfg.apply_mode = "kubectl"
    cfg.repo_dir = str(tmp_path / "repo")
    cfg.sync_timeout_s = 5
    import uuid
    ref = argocd.trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=f"wi_nokey_{uuid.uuid4().hex[:8]}", tenant_id="dev-tenant",
            repo=_FE_REPO, pr_number=18,
            files_changed=["src/app/x.component.html"],
        ),
        cfg=cfg,
    )
    assert ref.status == "degraded"
    assert "deploy key" in ref.detail, f"o motivo é nomeado, não opaco: {ref.detail!r}"


def test_g2_deployable_kind_builds_with_the_repos_own_ruler():
    """O BE (kind=deployable) morre hoje no `npm install`. A receita segue o
    kind: build = o comando do .dse/validation.json do repo (a régua que o L1
    já usa — commands.build é ['sh','-c','./mvnw -B -q ... package'] no
    testbed, extraído com jq), jar rodando, readiness por socket."""
    y = _source_deployment(
        "preview-wi-y", _LABELS, _cfg(), repo=_BE_REPO, branch="dse/wi_y",
        kind="deployable",
    )
    # Fase A3: a régua continua sendo o manifesto do repo — agora parseada
    # pelo validador do L1 quando a API responde (build_cmd), com o jq
    # ENDURECIDO (`// empty` + falha nomeada) como fallback in-pod. O que este
    # pin protege é a FONTE: nunca uma segunda receita.
    assert "jq -r '.commands.build[2] // empty'" in y, (
        "o build é o do manifesto do repo — nunca uma segunda receita"
    )
    assert "java -jar" in y
    assert "npm install" not in y, "npm não pertence à receita deployable"
    assert "tcpSocket" in y, "readiness por socket — actuator é opcional no repo"
    # contrato MEDIDO no repo: jdbc-url (Hikari) não liga em
    # SPRING_DATASOURCE_URL; os placeholders do próprio repo, sim.
    # Fase A3 (2026-08-19): o env `BMO_DB_*` deixou de ser fallback da
    # PLATAFORMA — o repo que precisa dele declara em `preview.env` (os dois
    # bmo foram migrados como pré-condição do deploy da rc.101). O fallback é
    # só a porta.
    assert "SERVER_PORT" in y
    assert "BMO_DB_URL" not in y, "nome de variável de cliente voltou ao fallback da plataforma"
    assert "SPRING_FLYWAY_ENABLED" not in y, (
        "o bloco Spring era parte do MESMO fallback de cliente; o repo que "
        "precisa dele declara em preview.env"
    )


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
