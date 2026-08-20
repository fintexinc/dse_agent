"""O link do preview cai NA mudança — um LLM decide o caminho, a plataforma valida.

Medido no wi_aa299a51 (PR #153, 2026-08-19): o preview subiu perfeito e o link
postado era a RAIZ — que neste repo (API pura, sem rota em `/`) responde
`500 SYS-002`. A URL certa era `/api/v1/portfolio/calculations/metrics`, e ela
estava literalmente no diff. Pedido do operador: uma etapa de LLM decide o
caminho (rota nova de API, página nova de FE) e o humano clica e já cai na
coisa certa, com uma nota de uma linha dizendo o que olhar.

As regras da casa que este arquivo pina:

  - o modelo NUNCA compõe URL: devolve um caminho RELATIVO que passa por
    validação dura (começa com "/", sem esquema, sem espaço, tamanho capado) e
    a plataforma o anexa à SUA base;
  - fail-open: saída inválida/recusa/erro → sem caminho → o link de hoje,
    byte a byte — o preview nunca é bloqueado por isto;
  - o caminho é um campo SEPARADO (`deep_path`), composto só na apresentação —
    dentro da URL ele re-rootaria o `baseURL` do Playwright no demo evidence.
"""
from __future__ import annotations

import json

from dse_validation.github.client import FakeGitHubClient

try:  # o vermelho: o módulo ainda não existe
    from dse_validation.preview import deep_link as dl
except ImportError:  # pragma: no cover
    dl = None  # type: ignore[assignment]


def test_the_module_exists():
    assert dl is not None, (
        "dse_validation.preview.deep_link não existe — o link do preview "
        "continua sendo a raiz, que numa API pura responde SYS-002"
    )


# ---------------------------------------------------------------------------
# Validação da saída do modelo (P1: o portão é nosso)
# ---------------------------------------------------------------------------

def _resolve(resposta: str):
    client = FakeGitHubClient()
    client.set_pr_files("acme/svc", 7, [
        {"filename": "src/Controller.java", "status": "added",
         "patch": '+  @GetMapping("/api/v1/portfolio/calculations/metrics")'},
    ])
    return dl.resolve_deep_link(
        client, repo="acme/svc", pr_number=7,
        instruction="add a metrics endpoint", files_changed=["src/Controller.java"],
        kind="deployable", complete=lambda prompt: resposta,
    )


def test_a_valid_relative_path_and_note_pass():
    r = _resolve(json.dumps({"path": "/api/v1/portfolio/calculations/metrics",
                             "note": "the new metrics discovery endpoint"}))
    assert r["path"] == "/api/v1/portfolio/calculations/metrics"
    assert r["note"] == "the new metrics discovery endpoint"


def test_an_absolute_url_is_refused_the_model_never_composes_urls():
    r = _resolve(json.dumps({"path": "https://evil.example/phish", "note": "x"}))
    assert r["path"] is None, "o modelo compôs uma URL e a plataforma aceitou"


def test_a_path_with_scheme_or_whitespace_or_no_slash_is_refused():
    for ruim in ("api/metrics", "/api/me trics", "/a\nb", "//evil.example/x",
                 "/" + "a" * 300):
        r = _resolve(json.dumps({"path": ruim, "note": "x"}))
        assert r["path"] is None, f"caminho inválido aceito: {ruim!r}"


def test_null_path_means_the_root_is_the_right_landing():
    r = _resolve(json.dumps({"path": None, "note": ""}))
    assert r["path"] is None and r["note"] == ""


def test_unparseable_output_fails_open_with_the_cost_kept():
    r = _resolve("não sou json")
    assert r["path"] is None
    assert r["cost_usd"] >= 0.0


def test_the_note_is_capped():
    r = _resolve(json.dumps({"path": "/api/x", "note": "n" * 500}))
    assert r["path"] == "/api/x"
    assert len(r["note"]) <= 120


def test_the_prompt_carries_instruction_and_the_pr_patch():
    """O grounding: a string da rota vive no DIFF; a instrução dá a intenção."""
    prompts: list[str] = []
    client = FakeGitHubClient()
    client.set_pr_files("acme/svc", 7, [
        {"filename": "src/Controller.java", "status": "added",
         "patch": '+  @GetMapping("/api/v1/rota-nova")'},
    ])

    def completa(p):
        prompts.append(p)
        return json.dumps({"path": "/api/v1/rota-nova", "note": "the new route"})

    dl.resolve_deep_link(
        client, repo="acme/svc", pr_number=7, instruction="create rota-nova",
        files_changed=["src/Controller.java"], kind="deployable", complete=completa,
    )
    assert prompts and "create rota-nova" in prompts[0]
    assert "/api/v1/rota-nova" in prompts[0], "o patch da PR não chegou ao prompt"


# ---------------------------------------------------------------------------
# get_pr_files no cliente (Protocol + Fake + truncamento)
# ---------------------------------------------------------------------------

def test_the_fake_client_serves_pr_files():
    client = FakeGitHubClient()
    client.set_pr_files("acme/svc", 7, [
        {"filename": "a.java", "status": "added", "patch": "+x"},
    ])
    files = client.get_pr_files("acme/svc", 7)
    assert files and files[0]["filename"] == "a.java"


def test_pr_patches_are_capped_for_grounding_not_mirroring():
    client = FakeGitHubClient()
    client.set_pr_files("acme/svc", 7, [
        {"filename": f"f{i}.java", "status": "modified", "patch": "+" + "x" * 10_000}
        for i in range(50)
    ])
    bloco = dl.pr_patch_block(client, "acme/svc", 7)
    assert len(bloco) <= dl.PATCH_BLOCK_MAX_CHARS + 200
    assert "f0.java" in bloco


# ---------------------------------------------------------------------------
# Superfícies: composição SÓ na apresentação
# ---------------------------------------------------------------------------

def test_the_pr_body_line_lands_on_the_change():
    from dse_validation.preview.argocd import preview_body_line

    linha = preview_body_line(
        "created", url="https://p.example", namespace="ns",
        deep_path="/api/v1/metrics", deep_note="the new metrics endpoint",
    )
    assert "https://p.example/api/v1/metrics" in linha
    assert "the new metrics endpoint" in linha


def test_without_a_deep_path_the_body_line_is_byte_identical_to_today():
    from dse_validation.preview.argocd import preview_body_line

    linha = preview_body_line("created", url="https://p.example", namespace="ns")
    assert linha == "- **Preview**: https://p.example (namespace `ns`)"
