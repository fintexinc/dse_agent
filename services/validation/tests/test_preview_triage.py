"""A triage do preview: o agente decide CONTEÚDO, e o contrato é duro.

Metade de conteúdo do laço de auto-fix (decisão de operador 2026-08-12): o
agente recebe o erro do pod + os arquivos-chave do repo NA BRANCH DA TASK e
devolve {fixable, reason, instruction}. O que estes testes fixam:

  - o veredito é validado DURO: saída malformada do modelo LEVANTA (a activity
    retenta, finito) — um default silencioso viraria "veredito infra" e o
    modelo quebrado ficaria invisível;
  - o contexto é real: os arquivos são lidos na branch da task, e chegam ao
    prompt — triage sem contexto é adivinhação paga;
  - um client sem `get_file_text` (fora do repo, fake antigo) não mata a
    triage — decide-se com o que houver;
  - `fixable` escreve na PR que o auto-fix está correndo (marker único de
    sempre): quem abre a PR no meio do laço vê "sendo consertado", não um
    degradado parado.
"""
from __future__ import annotations

import pytest
from dse_contracts import TriagePreviewFailureInput

from dse_validation.preview.triage import (
    FakePreviewTriageSession,
    triage_preview_failure_core,
)

_DETALHE = (
    "preview degraded: kubectl wait timed out — the pod said: Error: Could not "
    "find the '@angular-devkit/build-angular:browser-esbuild' builder's node package."
)


class _FilesClient:
    """Client mínimo: só o que a triage usa, gravando as chamadas."""

    def __init__(self, files: dict[tuple[str, str, str], str] | None = None):
        self.files = files or {}
        self.calls: list[tuple[str, str, str]] = []

    def get_file_text(self, repo: str, path: str, ref: str) -> str | None:
        self.calls.append((repo, path, ref))
        return self.files.get((repo, path, ref))


def _inp(**kw) -> TriagePreviewFailureInput:
    base = dict(
        work_item_id="wi_triage", tenant_id="t", repo="acme/repo", pr_number=6,
        branch="dse/wi_triage", detail=_DETALHE, kind="ui",
        autofix_round=1, autofix_cap=2,
    )
    base.update(kw)
    return TriagePreviewFailureInput(**base)


def test_triage_returns_a_strict_verdict_with_the_fake_session(monkeypatch):
    import dse_validation.github.client as gc

    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: _FilesClient())
    session = FakePreviewTriageSession(scripted=[{
        "fixable": True, "reason": "missing devDependency",
        "instruction": "add @angular-devkit/build-angular to devDependencies",
    }])

    # pr_number=0: o teste do veredito não é o teste da escrita na PR
    v = triage_preview_failure_core(_inp(pr_number=0), github_client=_FilesClient(),
                                    session=session)

    assert v.fixable is True
    assert "devDependencies" in v.instruction
    assert v.work_item_id == "wi_triage"


def test_malformed_model_output_raises_instead_of_defaulting():
    """A fronteira que importa: modelo que não responde o contrato LEVANTA.
    bool(raw.get('fixable')) de um dict sem a chave seria False — ou seja,
    'veredito infra' — e ninguém saberia que o modelo quebrou."""
    session = FakePreviewTriageSession(scripted=[{"nonsense": 1}])

    with pytest.raises(ValueError, match="fixable"):
        triage_preview_failure_core(_inp(pr_number=0), github_client=_FilesClient(),
                                    session=session)


def test_triage_reads_key_files_at_the_task_branch():
    client = _FilesClient(files={
        ("acme/repo", "package.json", "dse/wi_triage"): '{"scripts":{"start":"ng serve"}}',
        ("acme/repo", "angular.json", "dse/wi_triage"): '{"projects":{"x":{}}}',
    })
    session = FakePreviewTriageSession()

    triage_preview_failure_core(_inp(pr_number=0), github_client=client, session=session)

    assert ("acme/repo", "package.json", "dse/wi_triage") in client.calls, (
        "os arquivos têm de ser lidos NA BRANCH DA TASK — é o código que o "
        "preview tentou servir, não o main"
    )
    prompt = session.prompts[0]
    assert '"start":"ng serve"' in prompt, "o conteúdo lido chega ao prompt"
    assert "builder's node package" in prompt, "o erro do pod chega ao prompt"
    assert "(unavailable)" in prompt, "arquivo ausente vira seção vazia, não crash"


def test_triage_survives_a_client_without_get_file_text():
    session = FakePreviewTriageSession(scripted=[{
        "fixable": False, "reason": "cluster is down", "instruction": "",
    }])

    v = triage_preview_failure_core(_inp(pr_number=0), github_client=object(),
                                    session=session)

    assert v.fixable is False
    assert "(unavailable)" in session.prompts[0], (
        "sem leitor de arquivos, a triage decide com o erro sozinho — nunca quebra"
    )


def test_triage_reads_the_pods_words_from_the_ledger_when_the_detail_is_clockwork(monkeypatch):
    """O primeiro veredito de produção (wi_9580d984, 2026-08-12 14:09Z) saiu
    ERRADO por causa do input: no caminho em que a activity de preview estoura
    o prazo, o workflow só tem o boilerplate do Temporal ("StartToClose
    timeout") — e o agente diagnosticou "build lento, otimize o angular.json"
    quando a causa real ("Could not find '@angular/build'") estava gravada em
    wse_previews.detail pela attempt anterior.

    A triage passa a mesclar o detail do banco ao contexto SEMPRE: o banco é
    onde as palavras do pod moram, qualquer que seja o caminho da exceção."""
    from dse_validation.preview import triage as triage_mod

    monkeypatch.setattr(
        triage_mod, "_ledger_detail",
        lambda wi: ("preview degraded: kubectl wait timed out — the pod said: "
                    "Error: Could not find the '@angular/build:dev-server' "
                    "builder's node package."),
        raising=False,
    )
    session = FakePreviewTriageSession()

    triage_preview_failure_core(
        _inp(pr_number=0,
             detail="ActivityError: Activity task timed out (type: StartToClose)"),
        github_client=_FilesClient(), session=session,
    )

    prompt = session.prompts[0]
    assert "@angular/build" in prompt, (
        "as palavras do pod (no ledger) não chegaram ao agente — foi assim que "
        "o primeiro veredito de produção diagnosticou 'build lento' para uma "
        "dependência ausente"
    )
    assert "StartToClose" in prompt, "o erro recebido também fica (contexto do caminho)"


def test_a_fixable_verdict_writes_the_autofix_line_in_the_pr_body(monkeypatch):
    """Quem abre a PR no meio do auto-fix vê o que está acontecendo. Mesmo
    marker de sempre: o desfecho do re-preview substitui, não empilha."""
    import dse_validation.github.client as gc

    class _FakePr:
        def __init__(self, body: str):
            self.body = body

        def get_pull_request(self, repo, n):
            return {"body": self.body}

        def update_pull_request(self, repo, n, *, body):
            self.body = body

    fake = _FakePr(
        "### Fintex DSE\n\n- **Test evidence (L1)**: L1 green\n"
        "- **Preview**: did not come up — old degraded line <!-- dse:preview -->\n"
    )
    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: fake)
    session = FakePreviewTriageSession(scripted=[{
        "fixable": True, "reason": "app-caused",
        "instruction": "add the missing devDependency",
    }])

    triage_preview_failure_core(_inp(), github_client=_FilesClient(), session=session)

    assert "automatic fix attempt 1/2" in fake.body, (
        f"a PR não diz que o auto-fix está correndo: {fake.body!r}"
    )
    assert fake.body.count("- **Preview**:") == 1, "substitui a linha, não empilha"
    assert "builder's node package" in fake.body, (
        "a causa continua na frase — 'consertando' sem dizer O QUÊ é meia informação"
    )
