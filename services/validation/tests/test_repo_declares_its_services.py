"""O repositório declara os SERVIÇOS de que seus testes e seu preview precisam.

Hoje qualquer repositório com teste de integração não funciona no DSE: o
sandbox não tem Docker (testcontainers impossível), o egress é um proxy HTTP
(protocolo de Postgres/Redis não atravessa) e `connection refused` vira FAIL —
um veredito sobre o código do cliente. O preview tem UM Postgres hardcoded,
com credenciais literais e um db name global.

`services` é o mesmo desenho de `preview.start`, `install`, `test_subset`,
`reports.junit` e `lint_fix`: a plataforma não aprende "Postgres" — aprende o
conceito `image + port + env + ready`, e o repositório preenche. Os dados nunca
são invenção da plataforma nem do modelo: o schema/seed vem do comando
`prepare` (topo do manifesto, o `supabase migration up`/Flyway que o repo JÁ
tem), e dado de feature nova é fixture de teste no diff.

O que estes testes pinam além da forma:

  - `image` vai parar em YAML de Pod — o item 3.2 da auditoria é exatamente
    injeção via string do manifesto, então o alfabeto fecha AQUI (precedente:
    `_REPORT_GLOB_RE`);
  - `preview` é nome reservado (colide com o Service do próprio app no
    namespace de preview); `postgres` é PERMITIDO de propósito — quem o
    declara assume o endereço DNS legacy;
  - `$DSE_SERVICE_PASSWORD` é a única credencial que a plataforma dá, e
    referenciá-la sem declarar serviço nenhum é erro nomeado — a senha só
    existe quando um serviço existe.
"""
from __future__ import annotations

import pytest

from dse_contracts import GateStatus
from dse_validation.config import (
    L1Config,
    L1ManifestError,
    parse_repo_prepare,
    parse_repo_services,
)

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


def _manifesto(**topo):
    base = {"version": 1, "commands": {"test": ["npm", "test"]}}
    base.update(topo)
    return base


def _services(**svcs):
    return parse_repo_services(_manifesto(services=svcs), source="test")


# --- a forma -----------------------------------------------------------------

def test_a_manifest_without_services_parses_exactly_as_before():
    cfg = L1Config._from_manifest_payload(_manifesto(), source="test")
    assert cfg.services_declared == frozenset()
    assert parse_repo_services(_manifesto(), source="test") == {}


def test_a_postgres_service_with_user_and_writable_is_accepted():
    svcs = _services(postgres=_POSTGRES)
    decl = svcs["postgres"]
    assert decl.image == "postgres:16-alpine"
    assert decl.port == 5432
    assert decl.env["POSTGRES_DB"] == "app"
    assert decl.ready == ["pg_isready", "-U", "postgres"]
    assert decl.user == 70
    assert decl.writable == ["/var/lib/postgresql/data", "/var/run/postgresql"]


def test_an_unknown_service_field_is_a_named_error():
    with pytest.raises(L1ManifestError) as err:
        _services(db={**_POSTGRES, "imagen": "typo"})
    assert err.value.status is GateStatus.ERROR
    assert "imagen" in str(err.value)


def test_a_service_name_must_be_a_dns_label():
    for ruim in ("Db", "meu_banco", "-db", "db-", "a" * 25, ""):
        with pytest.raises(L1ManifestError):
            parse_repo_services(_manifesto(services={ruim: dict(_POSTGRES)}),
                                source="test")


def test_the_name_preview_is_reserved_and_the_error_says_why():
    with pytest.raises(L1ManifestError) as err:
        _services(preview=dict(_POSTGRES))
    assert "reserved" in str(err.value)


def test_the_name_postgres_is_allowed_because_it_takes_over_the_legacy_dns():
    assert "postgres" in _services(postgres=dict(_POSTGRES))


# --- as portas de segurança --------------------------------------------------

def test_an_image_reference_is_a_closed_alphabet_never_yaml_or_shell():
    """O valor entra num manifesto de Pod escrito por f-string. O alfabeto
    fecha no parser, antes de a string existir — item 3.2 da auditoria."""
    ok = ["postgres:16-alpine", "redis", "bitnami/postgresql:16",
          "ghcr.io/acme/pg:1.0",
          "postgres@sha256:" + "a" * 64]
    for image in ok:
        _services(db={**_POSTGRES, "image": image})

    ruins = ["postgres:16\nkind: Pod", "postgres:16 --privileged",
             "postgres:$(id)", "postgres:16;rm -rf /", 'postgres:"16"',
             "UPPER/case:1", "a" * 300]
    for image in ruins:
        with pytest.raises(L1ManifestError):
            _services(db={**_POSTGRES, "image": image})


def test_a_port_below_1024_is_refused_naming_nonroot():
    with pytest.raises(L1ManifestError) as err:
        _services(db={**_POSTGRES, "port": 80})
    assert "non-root" in str(err.value)


def test_ports_are_unique_across_services_and_distinct_from_the_preview_port():
    with pytest.raises(L1ManifestError):
        _services(db=dict(_POSTGRES), cache={**_POSTGRES, "port": 5432})
    with pytest.raises(L1ManifestError):
        parse_repo_services(
            _manifesto(services={"db": dict(_POSTGRES)},
                       preview={"port": 5432, "start": ["./run"]}),
            source="test")


def test_env_names_are_validated_and_values_coerced_to_str():
    svcs = _services(db={**_POSTGRES, "env": {"MAX_CONN": 50}})
    assert svcs["db"].env["MAX_CONN"] == "50"

    with pytest.raises(L1ManifestError):
        _services(db={**_POSTGRES, "env": {"9BAD": "x"}})
    with pytest.raises(L1ManifestError):
        _services(db={**_POSTGRES, "env": {"A B": "x"}})


def test_writable_paths_are_absolute_closed_and_never_escape_the_pod_volumes():
    for ruim in (["relativo/x"], ["/a/../b"], ["/"], ["/workspace"],
                 ["/workspace/sub"], ["/checkpoint.git"], ["/a;b"],
                 ["/x"] * 9):
        with pytest.raises(L1ManifestError):
            _services(db={**_POSTGRES, "writable": ruim})


def test_user_zero_is_refused_root_is_not_a_thing_here():
    with pytest.raises(L1ManifestError) as err:
        _services(db={**_POSTGRES, "user": 0})
    assert "runAsNonRoot" in str(err.value)


def test_more_than_four_services_is_refused():
    quatro = {f"svc{i}": {**_POSTGRES, "port": 5432 + i} for i in range(4)}
    parse_repo_services(_manifesto(services=quatro), source="test")

    cinco = {f"svc{i}": {**_POSTGRES, "port": 5432 + i} for i in range(5)}
    with pytest.raises(L1ManifestError):
        parse_repo_services(_manifesto(services=cinco), source="test")


# --- prepare e a credencial --------------------------------------------------

def test_prepare_is_a_top_level_argv_never_a_shell_string():
    assert parse_repo_prepare(
        _manifesto(prepare=["sh", "-c", "npx prisma migrate deploy"])
    ) == ["sh", "-c", "npx prisma migrate deploy"]
    assert parse_repo_prepare(_manifesto()) == []

    with pytest.raises(L1ManifestError):
        parse_repo_prepare(_manifesto(prepare="npx prisma migrate deploy"))


def test_the_password_token_requires_a_declared_service():
    """A senha só existe quando um serviço existe: um `$DSE_SERVICE_PASSWORD`
    órfão viraria env literal com o nome do token dentro."""
    with pytest.raises(L1ManifestError) as err:
        L1Config._from_manifest_payload(
            _manifesto(preview={"start": ["./run"],
                                "env": {"DB_URL": "pg://u:$DSE_SERVICE_PASSWORD@x/db"}}),
            source="test")
    assert "DSE_SERVICE_PASSWORD" in str(err.value)

    # com serviço declarado, o token é legítimo nos DOIS blocos
    L1Config._from_manifest_payload(
        _manifesto(services={"postgres": dict(_POSTGRES)},
                   preview={"start": ["./run"],
                            "env": {"DB_URL": "pg://u:$DSE_SERVICE_PASSWORD@x/db"}}),
        source="test")


def test_from_manifest_payload_rejects_a_bad_services_block_early_like_preview():
    """A dupla-validação: `_from_manifest_payload` é o portão do bootstrap e da
    emenda — um bloco que ele aceita e o sandbox recusa vira PR mergeada que
    quebra dias depois, longe do diff que a causou."""
    with pytest.raises(L1ManifestError):
        L1Config._from_manifest_payload(
            _manifesto(services={"db": {"port": 5432}}),  # sem image
            source="test")

    cfg = L1Config._from_manifest_payload(
        _manifesto(services={"postgres": dict(_POSTGRES)}), source="test")
    assert cfg.services_declared == frozenset({"postgres"})
