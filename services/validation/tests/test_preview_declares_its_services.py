"""O preview sobe os serviços que o REPO declara — e aposenta o chute.

O preview de `kind=deployable` sobe hoje UM Postgres hardcoded: imagem fixa,
credenciais literais `preview/preview`, db name GLOBAL do deployment
(`DSE_PREVIEW_DB_NAME`, hoje "fee" — o hibernate de um cliente específico
dentro da plataforma), e uma readinessProbe que testa `-d preview`, um banco
que o env não cria. Repo que precisa de Redis, de dois bancos, ou de um
Postgres com outro nome, não tem como dizer isso.

Com `services` declarado, os MESMOS sidecars nativos do sandbox sobem no pod
do preview — e o hardcoded é suprimido. O legacy fica intocado para quem não
declara nada, até os manifests migrarem por PR de emenda.

Duas decisões que não são cosmética:

  - **a senha nunca entra no manifest set.** Em modo gitops o set vira commit;
    a variável `DSE_SERVICE_PASSWORD` chega por `valueFrom.secretKeyRef`
    (Secret semeada fora do set, no closure after_namespace — o molde da
    deploy key), e os valores traduzidos usam a expansão `$(VAR)` do kubelet.
  - **o alias DNS é headless + publishNotReadyAddresses.** Um Service
    ClusterIP comum criaria DEADLOCK por construção: ele só publica endpoint
    de Pod Ready, o app não fica Ready sem o banco, e o banco está no mesmo
    Pod do app. Headless resolve para o IP do próprio Pod (entrega local, sem
    hairpin), e publishNotReadyAddresses quebra o ovo-e-galinha — mantendo o
    endereço `postgres:5432` que os manifests dos testbeds já usam.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dse_validation.config import PreviewConfig, parse_repo_services
from dse_validation.preview import argocd

_LABELS = {"dse.fintex/work-item": "wi_teste"}


def _cfg() -> PreviewConfig:
    cfg = PreviewConfig()
    cfg.mode = "source"
    return cfg


def _services(**svcs):
    return parse_repo_services({"services": svcs}, source="test")


_POSTGRES = {
    "image": "postgres:16-alpine",
    "port": 5432,
    "env": {"POSTGRES_PASSWORD": "$DSE_SERVICE_PASSWORD", "POSTGRES_DB": "app"},
    "ready": ["pg_isready", "-U", "postgres"],
}


def _deployment(**kw) -> str:
    base = dict(repo="acme/svc", branch="dse/wi", kind="deployable")
    base.update(kw)
    return argocd._source_deployment("preview-wi", _LABELS, _cfg(), **base)


def _manifests(**kw) -> dict:
    base = dict(repo="acme/svc", branch="dse/wi", kind="deployable")
    base.update(kw)
    return argocd.build_manifests(
        "preview-wi", "wi_teste", "tenant-t",
        datetime.now(timezone.utc) + timedelta(hours=1), 3600, _cfg(), **base)


# --- o legacy fica de pé ----------------------------------------------------

def test_the_legacy_deployable_without_services_keeps_its_hardcoded_postgres():
    m = _manifests()
    assert "postgres.yaml" in m
    assert "postgres:16-alpine" in m["postgres.yaml"]


def test_the_legacy_postgres_probe_checks_the_database_it_creates():
    """A probe testava `-d preview` enquanto o env criava POSTGRES_DB=fee —
    dessincronizadas desde sempre. Teste de FORMA (a semântica PQping do
    pg_isready mascara o erro ao vivo): a probe checa o banco que o env cria."""
    cfg = _cfg()
    m = _manifests()
    assert f"-d {cfg.preview_db_name}" in m["postgres.yaml"]
    assert "-d preview" not in m["postgres.yaml"]


# --- os serviços declarados -------------------------------------------------

def test_declared_services_become_native_sidecars_in_the_preview_pod():
    y = _deployment(repo_services=_services(postgres=_POSTGRES))

    assert "initContainers:" in y
    assert "restartPolicy: Always" in y
    assert "image: postgres:16-alpine" in y
    assert "startupProbe:" in y


def test_declared_services_suppress_the_hardcoded_postgres():
    m = _manifests(repo_services=_services(postgres=_POSTGRES))
    assert "postgres.yaml" not in m
    assert "svc-postgres.yaml" in m


def test_each_service_gets_a_headless_dns_alias_that_publishes_not_ready_addresses():
    m = _manifests(repo_services=_services(postgres=_POSTGRES))
    svc = m["svc-postgres.yaml"]

    assert "clusterIP: None" in svc
    assert "publishNotReadyAddresses: true" in svc
    assert "app: preview" in svc, "o selector é o pod do PRÓPRIO app"
    assert "name: postgres" in svc, "o DNS legacy `postgres:5432` continua valendo"


def test_the_password_never_appears_anywhere_in_the_manifest_set():
    """Em gitops o manifest set vira commit. A garantia não é 'tomamos
    cuidado': é que build_manifests NEM RECEBE a senha — a variável chega por
    secretKeyRef e a Secret é semeada fora do set."""
    m = _manifests(repo_services=_services(postgres=_POSTGRES))
    corpo = "\n".join(m.values())

    assert "secretKeyRef" in corpo
    assert "dse-preview-service-password" in corpo
    assert "$(DSE_SERVICE_PASSWORD)" in corpo, "valores usam a expansão do kubelet"
    for linha in corpo.splitlines():
        if "DSE_SERVICE_PASSWORD" in linha and "value:" in linha:
            raise AssertionError(f"senha literal no manifest set: {linha.strip()}")


def test_the_sidecar_env_defines_the_secret_ref_before_any_reference():
    y = _deployment(repo_services=_services(postgres=_POSTGRES))
    assert y.index("name: DSE_SERVICE_PASSWORD") < y.index("$(DSE_SERVICE_PASSWORD)"), (
        "a expansão $(VAR) do kubelet só enxerga variáveis definidas ANTES"
    )


def test_prepare_runs_in_the_app_script_before_install():
    """A migração+seed do repo roda antes do install/build do app — simétrico
    ao sandbox: o prepare é autossuficiente e o app já nasce com schema."""
    from dse_validation.config import parse_repo_preview

    decl = parse_repo_preview(
        {"install": ["pnpm", "install"], "preview": {"start": ["./run"]}},
        source="test")
    y = _deployment(repo_services=_services(postgres=_POSTGRES),
                    repo_preview=decl,
                    repo_prepare=["sh", "-c", "npx prisma migrate deploy"])

    assert "prisma migrate deploy" in y
    assert y.index("prisma migrate deploy") < y.index("pnpm install")


def test_a_ui_kind_repo_gets_its_services_too():
    """O alvo real (monorepo TS + Supabase) pode cair em kind=ui — os serviços
    não são exclusividade do deployable."""
    y = _deployment(kind="ui", repo_services=_services(postgres=_POSTGRES))
    assert "initContainers:" in y
    assert "image: postgres:16-alpine" in y
