"""O preview ensina o humano a testá-lo — o guia sai do MESMO turno do deep link.

Hoje a mensagem final entrega a URL e nada mais: o humano não sabe com que
usuário logar (as seeds do repo criam usuários que só quem leu o `seed.sql`
conhece), nem em que tela a mudança vive, nem que passos percorrer. O
`plan.test_plan` existe e morre no Tester; as seeds existem e morrem no banco.

O desenho, pinado aqui:

  - NENHUMA chamada nova de modelo: `resolve_deep_link` (que já é grounded no
    diff real e fail-open) passa a devolver também `steps` (≤8 × ≤140 chars) e
    `login` (≤200 chars) — o portão determinístico é NOSSO, como no path;
  - grounding novo: `test_plan` do plano, fatos do manifesto (prepare/services,
    nunca valor de secret) e as SEEDS reais do repo (regex fechado sobre a
    árvore + arquivos do diff, cap 2×4KB, best-effort);
  - compat: modelo que só devolve path/note → guia vazio, comportamento de
    hoje byte a byte. Lixo em steps/login → campo vazio, nunca erro.
"""
from __future__ import annotations

import json

from dse_validation.github.client import FakeGitHubClient
from dse_validation.preview import deep_link as dl


def _client() -> FakeGitHubClient:
    client = FakeGitHubClient()
    client.set_pr_files("acme/app", 7, [
        {"filename": "src/pages/Plans.tsx", "status": "modified",
         "patch": '+  <Route path="/planos" element={<Plans/>} />'},
    ])
    return client


def _resolve(resposta: str, client: FakeGitHubClient | None = None, **kw):
    return dl.resolve_deep_link(
        client or _client(), repo="acme/app", pr_number=7,
        instruction="adicionar a página de planos", files_changed=["src/pages/Plans.tsx"],
        kind="ui", complete=lambda prompt: resposta, **kw,
    )


# ---------------------------------------------------------------------------
# O guia validado (P1: o portão é nosso)
# ---------------------------------------------------------------------------

def test_a_valid_guide_comes_back_with_steps_and_login():
    r = _resolve(json.dumps({
        "path": "/planos", "note": "the new plans page",
        "steps": ["Abra /planos", "Clique em Nova Simulação", "Confira a projeção"],
        "login": "email demo@acme.com / senha demo123 (do supabase/seed.sql)",
    }))
    assert r["path"] == "/planos"
    assert r["steps"] == ["Abra /planos", "Clique em Nova Simulação", "Confira a projeção"]
    assert "demo@acme.com" in r["login"]


def test_the_guide_is_capped_and_sanitized():
    r = _resolve(json.dumps({
        "path": "/planos", "note": "x",
        "steps": ["s" * 500] * 20 + ["com\x00controle", "  ", "ok"],
        "login": "L" * 500,
    }))
    assert len(r["steps"]) <= 8
    assert all(len(s) <= 140 for s in r["steps"])
    assert all("\x00" not in s for s in r["steps"])
    assert "" not in r["steps"], "passo vazio sobreviveu à sanitização"
    assert len(r["login"]) <= 200


def test_a_model_that_only_knows_path_and_note_yields_an_empty_guide():
    """Compat: a forma de hoje continua valendo byte a byte."""
    r = _resolve(json.dumps({"path": "/planos", "note": "the new plans page"}))
    assert r["path"] == "/planos"
    assert r["note"] == "the new plans page"
    assert r["steps"] == []
    assert r["login"] == ""


def test_junk_steps_or_login_fail_open_to_empty_never_error():
    r = _resolve(json.dumps({
        "path": "/planos", "note": "x",
        "steps": {"nao": "é lista"}, "login": ["nem", "string"],
    }))
    assert r["path"] == "/planos", "lixo no guia não pode derrubar o path"
    assert r["steps"] == []
    assert r["login"] == ""


def test_steps_survive_a_null_path():
    """Mudança na home: raiz é o landing certo E ainda há o que testar."""
    r = _resolve(json.dumps({
        "path": None, "note": "", "steps": ["Recarregue a home", "Veja o banner"],
        "login": "",
    }))
    assert r["path"] is None
    assert r["steps"] == ["Recarregue a home", "Veja o banner"]


# ---------------------------------------------------------------------------
# O grounding novo no prompt
# ---------------------------------------------------------------------------

def test_the_prompt_carries_test_plan_manifest_facts_and_seeds():
    prompts: list[str] = []

    def capta(prompt: str):
        prompts.append(prompt)
        return json.dumps({"path": None, "note": ""})

    client = _client()
    dl.resolve_deep_link(
        client, repo="acme/app", pr_number=7,
        instruction="adicionar a página de planos", files_changed=[],
        kind="ui", complete=capta,
        test_plan="logar com o usuário seed e criar uma simulação",
        manifest_facts="prepare: supabase db reset; services: postgres (POSTGRES_DB)",
        seed_block="-- supabase/seed.sql\ninsert into users (email) values ('demo@acme.com');",
    )
    assert prompts, "o completer nunca foi chamado"
    p = prompts[0]
    assert "logar com o usuário seed" in p
    assert "supabase db reset" in p
    assert "demo@acme.com" in p


def test_seed_files_block_reads_the_real_tree_best_effort():
    """As credenciais REAIS vivem nas seeds do repo — o bloco as encontra por
    um regex fechado sobre a árvore, e falha de API vira bloco vazio."""
    client = FakeGitHubClient()
    client.set_tree_paths("acme/app", "dse/wi_x", [
        "src/index.ts", "supabase/seed.sql", "docs/README.md",
    ])
    client.set_file_text("acme/app", "supabase/seed.sql", "dse/wi_x",
                         "insert into users (email, password) values ('demo@acme.com','demo123');")
    bloco = dl.seed_files_block(client, "acme/app", "dse/wi_x", files_changed=[])
    assert "demo@acme.com" in bloco
    assert "supabase/seed.sql" in bloco

    vazio = dl.seed_files_block(FakeGitHubClient(), "acme/app", "dse/wi_x", files_changed=[])
    assert vazio == ""


def test_seed_files_block_is_capped():
    client = FakeGitHubClient()
    client.set_tree_paths("acme/app", "dse/wi_x",
                          [f"db/seeds/{i}_seed.sql" for i in range(10)])
    for i in range(10):
        client.set_file_text("acme/app", f"db/seeds/{i}_seed.sql", "dse/wi_x", "x" * 100_000)
    bloco = dl.seed_files_block(client, "acme/app", "dse/wi_x", files_changed=[])
    assert len(bloco) <= 2 * 4096 + 500, "o cap de 2 arquivos × 4KB não segurou"
