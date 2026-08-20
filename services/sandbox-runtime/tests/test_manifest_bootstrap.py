"""O DSE escreve o primeiro `.dse/validation.json` — como PR, nunca como fato.

Hoje um repo sem manifesto queima Planner + sandbox + 1 turno de Coder + 1 de
Tester antes de o L1 descobrir a ausência e escalar ("needs .dse/validation.json
on the base branch") — e o manifesto do calculation-engine-service foi escrito à
mão pelo operador. Decisão de operador (2026-08-19): o DSE se auto-onboarda com
uma PR de bootstrap dedicada, e a tarefa original encerra com "revise, merge e
reenvie".

O precedente exato é o `repo_doc.propose_agents_md` (PR de arquivo único, sem
sandbox, com refusal guards). O que este fluxo acrescenta — e estes testes
pinam — é a REGRA DE OURO: a saída do modelo só vira PR depois de passar pelo
parser REAL (`L1Config._from_manifest_payload`). O DSE nunca propõe um
manifesto que o próprio DSE rejeitaria.
"""
from __future__ import annotations

import json

from dse_validation.github.client import FakeGitHubClient

try:  # o vermelho: o módulo ainda não existe
    from sandbox_runtime import manifest_bootstrap as mb
except ImportError:  # pragma: no cover
    mb = None  # type: ignore[assignment]

def test_the_module_exists():
    assert mb is not None, (
        "sandbox_runtime.manifest_bootstrap não existe — o repo sem manifesto "
        "continua queimando Planner+sandbox+Coder+Tester antes de escalar"
    )

_TREE_MAVEN = [
    "pom.xml", "mvnw", "azure-pipelines.yml",
    "api/pom.xml", "api/src/main/java/App.java",
    "bootstrap/pom.xml", "bootstrap/src/test/java/AppTest.java",
]

_GOOD_MANIFEST = json.dumps({
    "version": 1,
    "commands": {
        "lint": ["sh", "-c", "./mvnw -B -q spotless:check"],
        "test": ["sh", "-c", "./mvnw -B test"],
        "build": ["sh", "-c", "./mvnw -B -q -DskipTests package"],
    },
})


def _client(tree=None, files=None, prs=None) -> FakeGitHubClient:
    c = FakeGitHubClient()
    c.set_tree_paths("acme/svc", "main", list(tree or _TREE_MAVEN))
    for path, texto in (files or {}).items():
        c.set_file_text("acme/svc", path, "main", texto)
    return c


# ---------------------------------------------------------------------------
# Probe: três respostas distintas
# ---------------------------------------------------------------------------

def test_probe_distinguishes_present_absent_and_unreachable():
    # rc.105: o probe passou a devolver TAMBÉM o que falta declarar — a
    # comparação vira por chave, e o terceiro caso ganhou teste próprio em
    # test_manifest_amendment.py.
    presente = _client(files={".dse/validation.json": "{}"})
    r = mb.probe_manifest(presente, "acme/svc", "main")
    assert r["present"] is True and r["reachable"] is True

    ausente = _client()
    r = mb.probe_manifest(ausente, "acme/svc", "main")
    assert r["present"] is False and r["reachable"] is True

    class _Fora:
        def get_file_text(self, repo, path, ref):
            raise RuntimeError("GitHub 500")

    r = mb.probe_manifest(_Fora(), "acme/svc", "main")
    assert r["reachable"] is False, (
        "API fora do ar não é 'manifesto ausente' — fail-open, o fluxo segue "
        "e o L1 continua dando a notícia dura"
    )


# ---------------------------------------------------------------------------
# O gerador: parser real como portão, PR idempotente
# ---------------------------------------------------------------------------

def test_a_valid_draft_becomes_a_single_file_pr():
    client = _client()
    r = mb.bootstrap_manifest(client, "acme/svc", "main", complete=lambda prompt: _GOOD_MANIFEST)
    assert r["ok"] is True and r["pr_number"], r
    pr = client.get_open_pr_for_branch("acme/svc", mb.BRANCH)
    assert pr and pr["number"] == r["pr_number"]
    escrito = client.get_file_text("acme/svc", ".dse/validation.json", mb.BRANCH)
    assert escrito and json.loads(escrito)["version"] == 1


def test_the_real_parser_is_the_gate_between_model_and_pr():
    """Saída que o parser do L1 rejeita NUNCA vira PR — nem branch, nem file."""
    client = _client()
    lixo = json.dumps({"version": 1, "commands": {}, "campo_que_nao_existe": 1})
    r = mb.bootstrap_manifest(client, "acme/svc", "main", complete=lambda prompt: lixo)
    assert r["ok"] is False
    assert "unknown fields" in r["reason"] or "campo_que_nao_existe" in r["reason"]
    assert client.get_open_pr_for_branch("acme/svc", mb.BRANCH) is None

    r2 = mb.bootstrap_manifest(client, "acme/svc", "main", complete=lambda prompt: "não é json")
    assert r2["ok"] is False
    assert client.get_open_pr_for_branch("acme/svc", mb.BRANCH) is None


def test_an_open_bootstrap_pr_is_reused_never_duplicated():
    client = _client()
    r1 = mb.bootstrap_manifest(client, "acme/svc", "main", complete=lambda p: _GOOD_MANIFEST)
    r2 = mb.bootstrap_manifest(client, "acme/svc", "main", complete=lambda p: (_ for _ in ()).throw(AssertionError("com PR aberta não se gasta modelo")))
    assert r2["ok"] is True and r2["existing"] is True
    assert r2["pr_number"] == r1["pr_number"]


def test_the_prompt_carries_the_repos_own_ci_and_facts():
    """O grounding é determinístico: os fatos do tree e o CI EXISTENTE do repo
    (para espelhar comandos reais, não inventar). O prompt tem que carregar os
    dois — e a armadilha do preview (`build` em ["sh","-c",…]) tem que estar
    escrita nele."""
    client = _client(files={"azure-pipelines.yml": "steps:\n  - script: ./mvnw -B test\n"})
    prompts: list[str] = []

    def completa(prompt: str) -> str:
        prompts.append(prompt)
        return _GOOD_MANIFEST

    mb.bootstrap_manifest(client, "acme/svc", "main", complete=completa)
    assert prompts, "o modelo nunca foi consultado"
    p = prompts[0]
    assert "Maven" in p, "os fatos do tree (build system) não chegaram ao prompt"
    assert "./mvnw -B test" in p, "o CI existente do repo não chegou ao prompt"
    assert '"sh", "-c"' in p or "sh -c" in p, (
        "a armadilha do commands.build[2] do preview não está no prompt"
    )


def test_the_pr_body_says_where_each_command_came_from():
    client = _client()
    mb.bootstrap_manifest(client, "acme/svc", "main", complete=lambda p: _GOOD_MANIFEST)
    pr = client.get_open_pr_for_branch("acme/svc", mb.BRANCH)
    corpo = client.pr_body("acme/svc", pr["number"])
    assert "review" in corpo.lower()
    assert ".dse/validation.json" in corpo


def test_the_worker_registry_carries_both_activities():
    """A lista ACTIVITIES é o registro REAL do worker (worker.py lê
    `mod.ACTIVITIES`). Um @activity.defn fora dela não existe em produção — o
    workflow chamaria e morreria em NotFoundError retry storm. Pego ao vivo na
    própria rc.101: o rollout subiu com 10 activities e as duas novas de fora."""
    from sandbox_runtime import activities

    nomes = {
        getattr(fn, "__temporal_activity_definition").name  # noqa: B009
        for fn in activities.ACTIVITIES
    }
    assert "probe_repo_manifest" in nomes
    assert "bootstrap_repo_manifest" in nomes
