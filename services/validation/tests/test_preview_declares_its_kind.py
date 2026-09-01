"""Um repositório com dois apps declara UM preview por KIND.

Medido no wi_83ca26c9 (glide-path, 2026-09-01): o diff tocou `apps/web/**.tsx`,
o paths-filter classificou `kind=ui` — corretamente — e a receita honrou o
`preview.start` do manifesto, que naquele repo descreve a **API**
(`node apps/api/dist/main.js`). O pod entrou em CrashLoop, 17 minutos de
`ready_timeout` viraram `preview_degraded`, a triage cega gastou turno, e o
operador nunca viu a tela que pediu.

A causa é de ESQUEMA: o manifesto tem um bloco `preview` e o repositório tem
dois apps. E não é só o processo que difere — medido no `vite.config.ts` do
repo, o dev server do SPA é porta 8080 (`strictPort`) enquanto a API é 3000.
Um override só de `start` não resolveria; a porta que o Service publica também
muda.

O override é raso, opcional e por kind (`ui`, `deployable`). O que ele NÃO faz
é tão importante quanto o que faz: repositório que não declara override não
percebe diferença nenhuma — `for_kind` devolve o MESMO objeto, e os manifests
saem byte-idênticos. É essa identidade que protege os outros cinco repos do
tenant, nenhum dos quais declara preview de UI hoje.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dse_validation.config import (
    L1ManifestError,
    PreviewConfig,
    parse_repo_preview,
)
from dse_validation.preview import argocd

_LABELS = {"dse.fintex/work-item": "wi_teste"}

_API_START = ["sh", "-c", "node apps/api/dist/main.js"]
_UI_START = ["sh", "-c", "npm run dev -- --host 0.0.0.0 --port 8080"]


def _cfg() -> PreviewConfig:
    cfg = PreviewConfig()
    cfg.mode = "source"
    return cfg


def _manifesto(**preview):
    """O manifesto do glide-path, reduzido ao que este teste mede."""
    base = {
        "image": "node:22-bookworm-slim",
        "port": 3000,
        "start": _API_START,
        "env": {"DATABASE_URL": "postgres://localhost:5432/postgres",
                "AUTH_ISSUER": "https://preview.invalid"},
    }
    base.update(preview)
    return {"version": 1, "commands": {}, "preview": base}


def _decl(**preview):
    return parse_repo_preview(_manifesto(**preview), source="test")


def _manifests(decl, kind: str) -> dict:
    return argocd.build_manifests(
        "preview-wi", "wi_teste", "tenant-t",
        datetime.now(timezone.utc) + timedelta(hours=1), 3600, _cfg(),
        repo="acme/app", branch="dse/wi", kind=kind, repo_preview=decl)


# ---------------------------------------------------------------------------
# O parser aceita o bloco por kind — pela MESMA porta do bloco base
# ---------------------------------------------------------------------------

def test_the_manifest_declares_a_block_per_kind():
    d = _decl(ui={"port": 8080, "start": _UI_START})
    assert d.port == 3000 and d.start == _API_START, "a base fica intacta"
    assert d.by_kind["ui"].port == 8080
    assert d.by_kind["ui"].start == _UI_START


def test_a_repo_without_overrides_declares_none():
    assert _decl().by_kind == {}


def test_an_unknown_field_inside_the_override_is_named():
    """Mesma disciplina do bloco base: typo é erro explicado, não default."""
    with pytest.raises(L1ManifestError) as err:
        _decl(ui={"imagen": "node:22"})
    assert "imagen" in str(err.value)


def test_an_unknown_kind_is_refused_naming_the_valid_ones():
    with pytest.raises(L1ManifestError) as err:
        _decl(mobile={"start": _UI_START})
    texto = str(err.value)
    assert "mobile" in texto
    assert "ui" in texto and "deployable" in texto, (
        "recusar sem dizer quais valem faz o autor do manifesto chutar de novo"
    )


def test_an_override_inside_an_override_is_refused():
    """UM nível. Sem isto, `preview.ui.ui.ui` seria um manifesto válido que
    ninguém consegue ler."""
    with pytest.raises(L1ManifestError):
        _decl(ui={"ui": {"port": 8080}})


def test_install_inside_an_override_still_names_its_destination():
    """A chave `install` é de TOPO — um repositório instala de um jeito só.
    O override não abre uma segunda porta para ela."""
    with pytest.raises(L1ManifestError) as err:
        _decl(ui={"install": ["npm", "ci"]})
    assert "install" in str(err.value)


# ---------------------------------------------------------------------------
# O merge
# ---------------------------------------------------------------------------

def test_a_declaration_without_overrides_is_returned_unchanged():
    """A rede dos outros cinco repos do tenant: mesma IDENTIDADE, portanto
    manifests byte-idênticos. Igualdade não bastaria — este pin é o que
    garante que nada a jusante muda."""
    d = _decl()
    assert d.for_kind("ui") is d
    assert d.for_kind("deployable") is d


def test_a_kind_without_an_override_falls_back_to_the_base():
    d = _decl(ui={"port": 8080, "start": _UI_START})
    assert d.for_kind("deployable") is d


def test_the_override_replaces_scalars_and_merges_env():
    d = _decl(ui={"port": 8080, "start": _UI_START,
                  "env": {"AUTH_ISSUER": "https://ui.invalid", "VITE_X": "1"}})
    m = d.for_kind("ui")
    assert m.port == 8080 and m.start == _UI_START
    assert m.image == "node:22-bookworm-slim", "escalar não declarado vem da base"
    # env funde por chave: a base sobrevive, o override vence o que repete.
    assert m.env["DATABASE_URL"] == "postgres://localhost:5432/postgres"
    assert m.env["AUTH_ISSUER"] == "https://ui.invalid"
    assert m.env["VITE_X"] == "1"


def test_the_merged_declaration_carries_no_further_overrides():
    d = _decl(ui={"port": 8080})
    assert d.for_kind("ui").by_kind == {}


def test_the_top_level_install_survives_the_merge():
    payload = _manifesto(ui={"port": 8080})
    payload["install"] = ["npm", "install", "--no-audit"]
    d = parse_repo_preview(payload, source="test")
    assert d.for_kind("ui").install == ["npm", "install", "--no-audit"]


# ---------------------------------------------------------------------------
# O que o cluster recebe (a prova que interessa)
# ---------------------------------------------------------------------------

def test_the_ui_preview_runs_the_ui_process_and_publishes_its_port():
    m = _manifests(_decl(ui={"port": 8080, "start": _UI_START}), "ui")
    deploy = m["deployment.yaml"]
    assert "npm run dev" in deploy
    assert "node apps/api/dist/main.js" not in deploy, (
        "é exatamente este processo errado que derrubou o wi_83ca26c9"
    )
    assert "containerPort: 8080" in deploy
    assert "targetPort: 8080" in m["service.yaml"], (
        "porta publicada diferente da que o container escuta = 502 com pod são"
    )


def test_the_deployable_preview_keeps_the_base_process():
    m = _manifests(_decl(ui={"port": 8080, "start": _UI_START}), "deployable")
    deploy = m["deployment.yaml"]
    assert "node apps/api/dist/main.js" in deploy
    assert "npm run dev" not in deploy
    assert "containerPort: 3000" in deploy
    assert "targetPort: 3000" in m["service.yaml"]
