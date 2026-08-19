"""De onde o plano tira `forbidden_paths`.

Até 2026-08-19 a resposta era "de uma constante do contrato", escrita no
primeiro commit deste repositório e nunca revista — a mesma lista para todo
repo, e a causa de "adicione um workflow do GitHub Actions" ser uma tarefa
impossível por construção.

Agora o repo pode declarar a sua no `.dse/validation.json`, lido no BASE SHA
imutável: a PR em voo não afrouxa a própria guarda. Repo que não declara nada
mantém o default de hoje.

O que este arquivo pina é o comportamento na FALHA, que é o que sempre
apodrece: manifesto ausente, JSON torto, API fora do ar ou repo que declara
lixo NUNCA podem virar "nenhuma proteção". Degradar para o default é a única
degradação segura — o L1 é quem dá a notícia de manifesto inválido, com
mensagem própria.
"""
from __future__ import annotations

from dse_contracts import PlanArtifact

from sandbox_runtime import activities

_DEFAULT = PlanArtifact.model_fields["forbidden_paths"].default_factory()


def _resolve(monkeypatch, leitor):
    resolver = getattr(activities, "_forbidden_paths_for", None)
    assert resolver is not None, (
        "sandbox_runtime.activities._forbidden_paths_for não existe — o plano "
        "ainda tira forbidden_paths de uma constante"
    )
    monkeypatch.setattr(activities, "_read_repo_manifest_text", leitor)
    return resolver("acme/repo", "a" * 40)


def test_a_repo_that_declares_its_own_paths_gets_them(monkeypatch):
    manifesto = '{"version": 1, "commands": {}, "forbidden_paths": ["config/production/"]}'
    assert _resolve(monkeypatch, lambda *a, **k: manifesto) == ["config/production/"]


def test_a_repo_without_a_manifest_keeps_the_platform_default(monkeypatch):
    assert _resolve(monkeypatch, lambda *a, **k: None) == _DEFAULT


def test_a_manifest_without_the_block_keeps_the_platform_default(monkeypatch):
    manifesto = '{"version": 1, "commands": {"lint": ["ruff", "check", "."]}}'
    assert _resolve(monkeypatch, lambda *a, **k: manifesto) == _DEFAULT


def test_an_unreadable_manifest_never_means_no_protection(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("GitHub 500")

    assert _resolve(monkeypatch, explode) == _DEFAULT
    assert _resolve(monkeypatch, lambda *a, **k: "{isso não é json") == _DEFAULT
    # lixo declarado (tipo errado) também cai no default: quem reprova
    # manifesto inválido é o L1, e ele reprova com o nome do campo.
    assert _resolve(monkeypatch, lambda *a, **k: '{"forbidden_paths": "migrations/"}') == _DEFAULT


def test_a_repo_that_declares_nothing_protected_is_obeyed(monkeypatch):
    """Lista vazia é decisão explícita, e custou um merge revisado no base
    branch — não é o mesmo que manifesto ausente."""
    manifesto = '{"version": 1, "commands": {}, "forbidden_paths": []}'
    assert _resolve(monkeypatch, lambda *a, **k: manifesto) == []


def test_without_a_base_sha_the_default_is_the_answer(monkeypatch):
    """Sem SHA não há leitura imutável, e ler do branch seria deixar a PR
    escolher a própria guarda."""
    resolver = getattr(activities, "_forbidden_paths_for", None)
    assert resolver is not None
    chamadas: list[tuple] = []
    monkeypatch.setattr(
        activities, "_read_repo_manifest_text",
        lambda *a, **k: chamadas.append(a) or None,
    )
    assert resolver("acme/repo", None) == _DEFAULT
    assert chamadas == [], "não se lê manifesto sem SHA imutável"
