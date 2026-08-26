"""The Planner's curated-doc channel, and the distinction that keeps it honest.

`hydrate_planner_context` used to read AGENTS.md and CODEOWNERS off
`workspace_dir` — a directory `provision_sandbox` creates and the workflow runs
the Planner BEFORE (workflows.py:1693 vs :1697). On the docker profile it did not
exist yet; under `sandboxDriver: k8s` it is never created on the worker at all.
So `render()`'s `if self.agents_md:` guard skipped the block on every production
turn, and the ledger said nothing either way, because '' was the only value the
code could produce and nobody could tell it from a repo that had no doc.

The fix reads at the base ref through the GitHub App. What is pinned here is the
three-state convention that makes the fix auditable rather than merely working:

    ''    the file is genuinely not in the repo at this ref   -> no event
    None  we could not ask (no App, transport error)          -> an event

Conflating those two is the original bug, not a detail of it.
"""
from __future__ import annotations

import sandbox_runtime.activities as acts


class _Client:
    """Stands in for RealGitHubClient. `files` maps path -> text; anything absent
    answers None, which is what a 404 becomes in `get_file_text`."""

    def __init__(self, files: dict[str, str], *, raises: Exception | None = None):
        self.files = files
        self.raises = raises
        self.asked: list[str] = []

    def get_file_text(self, repo: str, path: str, ref: str) -> str | None:
        self.asked.append(path)
        if self.raises is not None:
            raise self.raises
        return self.files.get(path)


def _run(monkeypatch, client, *, configured=True):
    monkeypatch.setattr(acts, "_planner_github_client", lambda: client, raising=True)

    class _Cfg:
        is_configured = configured

    import dse_validation.config as cfg

    monkeypatch.setattr(cfg, "GitHubConfig", lambda: _Cfg(), raising=True)
    tel: dict = {}
    docs = acts._repo_docs_for_planner("acme/api", "main", telemetry=tel)
    return docs, tel


def test_absent_doc_is_empty_string_and_emits_no_degradation(monkeypatch):
    """A repo without an AGENTS.md is a fact about that repo. It must not look
    like a failure of ours, or the metric that says whether curating one is worth
    anything becomes unreadable."""
    (agents_md, codeowners), tel = _run(monkeypatch, _Client({}))

    assert agents_md == ""
    assert codeowners == ""
    assert "repo_doc_unavailable_reason" not in tel
    assert tel["agents_md_source"] == "absent"
    assert tel["codeowners_source"] == "absent"


def test_unconfigured_app_returns_none_and_names_the_reason(monkeypatch):
    """None, not ''. The caller turns this into `planner_repo_doc_unavailable`;
    '' here would silently claim the repo curates nothing."""
    (agents_md, codeowners), tel = _run(monkeypatch, _Client({}), configured=False)

    assert agents_md is None and codeowners is None
    assert tel["repo_doc_unavailable_reason"] == "github_app_not_configured"


def test_transport_failure_is_unavailable_not_absent(monkeypatch):
    """The Planner keeps planning — the docs are context, not a requirement, the
    same rule the tree fetch follows — but the ledger records that it ran without
    them."""
    (agents_md, _), tel = _run(monkeypatch, _Client({}, raises=TimeoutError("slow")))

    assert agents_md is None
    assert tel["repo_doc_unavailable_reason"] == "TimeoutError"


def test_reads_the_doc_and_records_its_size(monkeypatch):
    client = _Client({"AGENTS.md": "# Conventions\nUse kebab-case filenames.\n"})
    (agents_md, _), tel = _run(monkeypatch, client)

    assert "kebab-case" in agents_md
    assert tel["agents_md_source"] == "repo"
    assert tel["agents_md_chars"] == len(agents_md)
    assert tel["agents_md_truncated_chars"] == 0


def test_codeowners_follows_github_precedence(monkeypatch):
    """.github/CODEOWNERS wins over the root copy — GitHub's own order. The
    workspace read probed root-first, which was backwards."""
    client = _Client({"CODEOWNERS": "* @root", ".github/CODEOWNERS": "* @dotgithub"})
    (_, codeowners), tel = _run(monkeypatch, client)

    assert codeowners == "* @dotgithub"
    assert tel["codeowners_path"] == ".github/CODEOWNERS"
    assert client.asked[1] == ".github/CODEOWNERS"


def test_oversized_doc_is_cut_on_a_line_boundary_and_the_cut_is_recorded(monkeypatch):
    """Half a convention reads exactly like a whole one — the same reason
    `_expand_into` refuses to slice a directory listing."""
    # O tamanho DERIVA do cap: um número fixo aqui (eram 400 linhas) para de
    # exceder assim que o cap sobe, e o teste passa a provar nada — foi o que
    # aconteceu quando o cap virou 20.000 em 2026-08-26.
    _LINHA = "rule {}: never do the thing"
    n = acts._PLANNER_AGENTS_MD_MAX_CHARS // len(_LINHA.format(0)) + 50
    body = "\n".join(_LINHA.format(i) for i in range(n))
    assert len(body) > acts._PLANNER_AGENTS_MD_MAX_CHARS

    (agents_md, _), tel = _run(monkeypatch, _Client({"AGENTS.md": body}))

    assert len(agents_md) <= acts._PLANNER_AGENTS_MD_MAX_CHARS + 50
    assert agents_md.endswith("[truncated to the planner doc budget]")
    assert "rule 0: never do the thing" in agents_md
    assert tel["agents_md_truncated_chars"] > 0
    # No dangling half-rule before the marker.
    assert agents_md.split("\n[truncated")[0].endswith("the thing")


def test_both_docs_at_their_caps_cannot_evict_a_skill(monkeypatch):
    """The caps exist to bound this: the live 21-skill registry renders at 10.351
    chars against a 16.000-char budget, so the docs have to fit in the gap."""
    assert (
        acts._PLANNER_AGENTS_MD_MAX_CHARS + acts._PLANNER_CODEOWNERS_MAX_CHARS + 10_351
        <= acts._PLANNER_CONTEXT_BUDGET_CHARS
    )

# ---------------------------------------------------------------------------
# O espécime REAL (não o de testbed que calibrou o cap original)
# ---------------------------------------------------------------------------
#: `AGENTS.md` de fintexinc/glide-path-planner-93, medido em 2026-08-26: 18.438
#: chars. O cap original (2.800) foi calibrado contra um AGENTS.md de testbed
#: com 2.256 — o primeiro repositório real tem 8x isso.
_REAL_AGENTS_MD_CHARS = 18_438

#: Onde as convenções que o modelo PRECISAVA estavam nesse arquivo. Ambas caíam
#: fora do cap, e cada uma matou um work item: `app.inject` (o repo não tem
#: supertest — o Coder importou o que não existe e 22 erros de lint type-aware
#: viraram insolúveis, wi_d1e069ad) e `gen:contract` (endpoint novo exige
#: regenerar o golden do OpenAPI — CI vermelho na PR #792).
_CONVENTION_POSITIONS = {"app.inject": 5_071, "gen:contract": 7_926}


def test_a_real_client_agents_md_arrives_whole():
    """Duas escaladas pagas saíram daqui: o DSE lia 15% do arquivo e o modelo
    era culpado por não seguir a convenção que nós escondemos dele."""
    assert acts._PLANNER_AGENTS_MD_MAX_CHARS >= _REAL_AGENTS_MD_CHARS, (
        f"o cap ({acts._PLANNER_AGENTS_MD_MAX_CHARS}) corta o AGENTS.md real "
        f"({_REAL_AGENTS_MD_CHARS} chars) — as convenções em "
        f"{sorted(_CONVENTION_POSITIONS.items(), key=lambda kv: kv[1])} nunca "
        "chegam ao Planner"
    )
    for nome, posicao in _CONVENTION_POSITIONS.items():
        assert acts._PLANNER_AGENTS_MD_MAX_CHARS > posicao, nome


def test_the_real_doc_is_delivered_uncut_end_to_end(monkeypatch):
    """A constante não basta: o que importa é o que SAI de `_repo_docs_for_planner`."""
    corpo = "\n".join(
        f"line {i}: a convention the model must follow" for i in range(430)
    )
    assert len(corpo) >= _REAL_AGENTS_MD_CHARS, f"espécime curto demais: {len(corpo)}"

    (agents_md, _), tel = _run(monkeypatch, _Client({"AGENTS.md": corpo}))

    assert tel["agents_md_truncated_chars"] == 0, "o doc real ainda é cortado"
    assert "truncated" not in agents_md
    assert agents_md.endswith("line 429: a convention the model must follow"), (
        "o fim do arquivo — onde as convenções deste repo moram — não chegou"
    )
