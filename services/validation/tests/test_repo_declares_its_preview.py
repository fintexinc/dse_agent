"""G7: o preview é do REPO, não da plataforma.

Hoje a receita `deployable` (preview/argocd.py:360-414) chuta a forma do repo
em quatro lugares: a imagem (`eclipse-temurin:17-jdk`, boa para Java 17 e só),
o artefato (`ls target/*.jar`, que só existe em projeto de módulo único), o env
(`BMO_DB_URL`/`BMO_DB_USER`/… — nomes de variável de UM cliente dentro da
plataforma) e a porta. O próprio comentário do arquivo (:385-388) chama isso de
dívida e nomeia o caminho: o repo declara seu preview do mesmo jeito que já
declara o build.

O gatilho concreto é o `fintexinc/calculation-engine-service`: Java 21
multi-módulo, jar em `bootstrap/target/`, sem banco nenhum. Com a receita atual
ele quebra três vezes antes de subir.

Regra de ouro destes testes: **repo que não declara nada continua igual**. A
dívida sai por adição, não por troca — o repo BMO que depende dos nomes
antigos não pode notar diferença.
"""
from __future__ import annotations

import pytest

from dse_validation.config import L1ManifestError, PreviewConfig
from dse_validation.preview import argocd

try:
    from dse_validation.config import RepoPreviewDeclaration, parse_repo_preview
except ImportError:  # vermelho: o contrato ainda não existe
    RepoPreviewDeclaration = None  # type: ignore[assignment]
    parse_repo_preview = None  # type: ignore[assignment]

_LABELS = {"dse.fintex/work-item": "wi_teste"}


def _cfg() -> PreviewConfig:
    cfg = PreviewConfig()
    cfg.mode = "source"
    return cfg


def _declara(**campos):
    assert parse_repo_preview is not None, (
        "dse_validation.config.parse_repo_preview não existe — o repo não tem "
        "como declarar seu preview e a plataforma segue chutando a forma dele"
    )
    return parse_repo_preview({"version": 1, "commands": {}, "preview": campos})


# --------------------------------------------------------------------------
# O contrato: o manifesto aceita o bloco, e o valida
# --------------------------------------------------------------------------
def test_the_manifest_accepts_a_preview_block():
    """Chave desconhecida no manifesto é erro DURO (config.py:622) — então sem
    esta whitelist um repo que declara `preview` tem TODA run virando
    l1_manifest_invalid, e o gate morre antes do Coder."""
    from dse_validation.config import L1Config

    cfg = L1Config._from_manifest_payload(
        {
            "version": 1,
            "commands": {"build": ["sh", "-c", "./mvnw package"]},
            "preview": {"image": "eclipse-temurin:21-jdk"},
        },
        source="teste",
    )
    assert cfg.build_cmd == ["sh", "-c", "./mvnw package"], (
        "o bloco preview não pode atrapalhar o parse dos comandos"
    )


def test_an_unknown_key_inside_preview_is_a_clear_error():
    """Mesma disciplina do resto do manifesto: erro explicado, nunca campo
    ignorado em silêncio (um typo em `imagen:` viraria default sem aviso)."""
    with pytest.raises(L1ManifestError) as exc:
        _declara(imagen="eclipse-temurin:21-jdk")
    assert "imagen" in str(exc.value)


def test_a_repo_that_declares_nothing_gets_todays_defaults():
    decl = _declara()
    assert decl.image is None and decl.artifact_glob is None
    assert decl.env == {} and decl.port is None


# --------------------------------------------------------------------------
# A receita: o que o repo declara chega ao pod
# --------------------------------------------------------------------------
def test_a_java21_multimodule_repo_gets_its_image_and_its_jar():
    """O caso do calculation-engine-service: imagem 21 (17 não compila release
    21) e o jar em bootstrap/target — o `ls target/*.jar` de hoje falharia com
    `set -eu` antes do java -jar."""
    y = argocd._source_deployment(
        "preview-wi", _LABELS, _cfg(), repo="fintexinc/calculation-engine-service",
        branch="dse/wi_teste", kind="deployable",
        repo_preview=_declara(image="eclipse-temurin:21-jdk",
                              artifact_glob="bootstrap/target/*.jar"),
    )
    assert "eclipse-temurin:21-jdk" in y, "a imagem declarada pelo repo não chegou"
    assert "eclipse-temurin:17-jdk" not in y, "a imagem da plataforma sobreviveu"
    assert "bootstrap/target/*.jar" in y, "o glob declarado não chegou"
    assert "grep -v plain" in y, "o filtro do jar 'plain' do Spring Boot sumiu"


def test_the_declared_env_replaces_the_hardcoded_client_variables():
    """O bloco BMO_DB_* é de outro cliente. Quando o repo declara o próprio
    env, os nomes alheios não podem viajar junto — este repo não tem banco."""
    y = argocd._source_deployment(
        "preview-wi", _LABELS, _cfg(), repo="fintexinc/calculation-engine-service",
        branch="dse/wi_teste", kind="deployable",
        repo_preview=_declara(env={"SM_REST_BASE_URL": "http://sms.invalid",
                                   "SPRING_PROFILES_ACTIVE": "preview"}),
    )
    assert "SM_REST_BASE_URL" in y and "SPRING_PROFILES_ACTIVE" in y
    assert "BMO_DB_URL" not in y, (
        "nome de variável de outro cliente foi injetado num repo que declarou "
        "o próprio env"
    )


def test_a_repo_that_declares_nothing_keeps_image_and_artifact_defaults():
    """A rede de segurança do G7, ATUALIZADA em 2026-08-19 (Fase A3): imagem e
    glob continuam com os defaults de sempre — mas o env de fallback deixou de
    carregar os nomes de UM cliente (`BMO_DB_*`/Spring). Este teste afirmava o
    contrário; a mudança de política está em
    test_preview_recipe_is_repo_agnostic.py, e a migração dos repos BMO para
    `preview.env` declarado é pré-condição do deploy da rc.101."""
    y = argocd._source_deployment(
        "preview-wi", _LABELS, _cfg(), repo="fintexinc/bmo-fee-calculator-be-dse",
        branch="dse/wi_teste", kind="deployable", repo_preview=None,
    )
    assert "eclipse-temurin:17-jdk" in y
    assert "SERVER_PORT" in y
    assert "BMO_DB_URL" not in y
    assert "ls target/*.jar" in y or "target/*.jar" in y


def test_the_build_command_shape_the_recipe_depends_on_is_pinned():
    """HISTÓRICO + FASE A3: `jq -r '.commands.build[2]'` assumia que o build é
    ["sh","-c","<comando>"] — um argv puro fazia o jq devolver null e o pod
    rodava `sh -c null` SILENCIOSO. Desde a Fase A3 o caminho principal é o
    argv parseado pelo validador do L1 (read_repo_build_cmd); o jq continua
    como fallback de API falhada, endurecido: `// empty` + falha nomeada."""
    y = argocd._source_deployment(
        "preview-wi", _LABELS, _cfg(), repo="acme/repo", branch="dse/wi",
        kind="deployable", repo_preview=None,
    )
    assert "jq -r '.commands.build[2] // empty'" in y, (
        "a receita mudou de fonte do build — o manifesto do repo é a régua"
    )
    assert "no build command" in y, "manifesto sem build falha nomeado, nunca sh -c null"
