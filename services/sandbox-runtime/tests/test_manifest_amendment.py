"""Manifesto que existe mas não declara o que a plataforma precisa: o DSE emenda.

A rc.101 ensinou o DSE a escrever o PRIMEIRO `.dse/validation.json` de um
repositório. Mas o gatilho é `reachable and not present` — repo que JÁ tem
manifesto nunca ganha nada. Toda vez que a plataforma passa a exigir um campo
novo, os repos já onboardados ficam para trás e alguém escreve PR à mão.

A emenda fecha isso, com uma diferença de SEVERIDADE que o desenho precisa
respeitar:

  - manifesto AUSENTE: nenhum gate consegue rodar → PR de bootstrap e a tarefa
    ENCERRA ("revise, mergeie e reenvie");
  - manifesto INCOMPLETO: os gates rodam, só o preview degrada → PR de emenda e
    a tarefa SEGUE.

Escalar por campo faltando trocaria um preview degradado por uma tarefa morta.

A emenda também é conservadora por construção: ela ADICIONA a chave que falta
ao JSON que já existe, preservando todo o resto — e passa pelo mesmo portão de
sempre (o parser REAL do L1) antes de virar PR.
"""
from __future__ import annotations

import json

from dse_validation.github.client import FakeGitHubClient

try:
    from sandbox_runtime import manifest_bootstrap as mb
except ImportError:  # pragma: no cover
    mb = None  # type: ignore[assignment]

_TREE = ["pom.xml", "mvnw", "src/main/java/App.java"]

_SEM_START = json.dumps({
    "version": 1,
    # Os quatro gates declarados de propósito: desde que a emenda passou a
    # cobrar comando de gate ausente (NOT_CONFIGURED reprova a rodada e nada
    # no laço alcança), um fixture com só `test` provaria outra coisa — cada
    # teste abaixo isola O QUE ele quer medir.
    "commands": {"lint": ["./mvnw", "-B", "spotless:check"],
                 "typecheck": ["./mvnw", "-B", "compile"],
                 "test": ["sh", "-c", "./mvnw -B test"],
                 "build": ["sh", "-c", "./mvnw -B package"]},
    "preview": {"image": "eclipse-temurin:21-jdk", "port": 8181},
}, indent=2)

_COM_START = json.dumps({
    "version": 1,
    # Os quatro gates declarados de propósito: desde que a emenda passou a
    # cobrar comando de gate ausente (NOT_CONFIGURED reprova a rodada e nada
    # no laço alcança), um fixture com só `test` provaria outra coisa — cada
    # teste abaixo isola O QUE ele quer medir.
    "commands": {"lint": ["./mvnw", "-B", "spotless:check"],
                 "typecheck": ["./mvnw", "-B", "compile"],
                 "test": ["sh", "-c", "./mvnw -B test"],
                 "build": ["sh", "-c", "./mvnw -B package"]},
    "preview": {"image": "eclipse-temurin:21-jdk", "port": 8181,
                "start": ["sh", "-c", "java -jar bootstrap/target/*.jar"]},
}, indent=2)


def _client(manifesto: str | None) -> FakeGitHubClient:
    c = FakeGitHubClient()
    c.set_tree_paths("acme/svc", "main", list(_TREE))
    if manifesto is not None:
        c.set_file_text("acme/svc", ".dse/validation.json", "main", manifesto)
    return c


def test_the_module_exposes_the_amendment():
    assert mb is not None and hasattr(mb, "amend_manifest"), (
        "sandbox_runtime.manifest_bootstrap.amend_manifest não existe — repo "
        "já onboardado continua exigindo PR à mão a cada campo novo"
    )


# ---------------------------------------------------------------------------
# O probe distingue as três situações, não duas
# ---------------------------------------------------------------------------

def test_the_probe_reports_what_the_manifest_is_missing():
    r = mb.probe_manifest(_client(_SEM_START), "acme/svc", "main")
    assert r["present"] is True and r["reachable"] is True
    assert "preview.start" in (r.get("missing") or []), (
        "o probe não sabe dizer que o manifesto existe mas está incompleto"
    )


def test_a_complete_manifest_reports_nothing_missing():
    r = mb.probe_manifest(_client(_COM_START), "acme/svc", "main")
    assert r["present"] is True and not (r.get("missing") or [])


def test_a_repo_without_a_preview_block_is_not_nagged():
    """Repo que não tem preview nenhum não precisa declarar como sobe.

    A asserção mira `preview.start` em vez da lista inteira: a emenda passou a
    cobrar TAMBÉM comando de gate ausente, e um manifesto que declara só
    `test` é de fato incompleto — ele morreria em NOT_CONFIGURED no primeiro
    L1. O que este teste protege é a regra do preview, não a do gate."""
    sem_preview = json.dumps({"version": 1, "commands": {"test": ["pytest"]}})
    r = mb.probe_manifest(_client(sem_preview), "acme/svc", "main")
    assert "preview.start" not in (r.get("missing") or [])


# ---------------------------------------------------------------------------
# A emenda
# ---------------------------------------------------------------------------

def test_the_amendment_adds_the_key_and_preserves_everything_else():
    client = _client(_SEM_START)
    r = mb.amend_manifest(
        client, "acme/svc", "main", missing=["preview.start"],
        complete=lambda p: _COM_START,
    )
    assert r["ok"] is True and r["pr_number"]
    escrito = json.loads(client.get_file_text("acme/svc", ".dse/validation.json", mb.BRANCH_AMEND))
    assert escrito["preview"]["start"], "a chave que faltava não foi acrescentada"
    assert escrito["commands"]["test"] == ["sh", "-c", "./mvnw -B test"], (
        "a emenda mexeu em algo que não era dela"
    )
    assert escrito["preview"]["image"] == "eclipse-temurin:21-jdk"


def test_a_draft_the_real_parser_rejects_never_becomes_a_pr():
    client = _client(_SEM_START)
    lixo = json.dumps({"version": 1, "commands": {}, "preview": {"imagem": "x"}})
    r = mb.amend_manifest(client, "acme/svc", "main", missing=["preview.start"],
                          complete=lambda p: lixo)
    assert r["ok"] is False
    assert client.get_open_pr_for_branch("acme/svc", mb.BRANCH_AMEND) is None


def test_an_amendment_that_does_not_add_what_was_missing_is_refused():
    """O portão específico da emenda: o modelo pode devolver JSON válido que
    simplesmente não resolve o problema. Sem esta checagem a PR abriria e a
    próxima tarefa abriria outra igual."""
    client = _client(_SEM_START)
    r = mb.amend_manifest(client, "acme/svc", "main", missing=["preview.start"],
                          complete=lambda p: _SEM_START)
    assert r["ok"] is False
    assert client.get_open_pr_for_branch("acme/svc", mb.BRANCH_AMEND) is None


def test_an_open_amendment_pr_is_reused_never_duplicated():
    client = _client(_SEM_START)
    r1 = mb.amend_manifest(client, "acme/svc", "main", missing=["preview.start"],
                           complete=lambda p: _COM_START)
    r2 = mb.amend_manifest(
        client, "acme/svc", "main", missing=["preview.start"],
        complete=lambda p: (_ for _ in ()).throw(AssertionError("com PR aberta não se gasta modelo")),
    )
    assert r2["ok"] is True and r2["existing"] is True and r2["pr_number"] == r1["pr_number"]


def test_the_amendment_uses_a_branch_of_its_own():
    """Bootstrap e emenda não podem colidir: um cria o arquivo, o outro o
    altera, e um repo pode precisar dos dois em momentos diferentes."""
    assert mb.BRANCH_AMEND != mb.BRANCH
