"""O guia "How to test" persiste com o preview — é de lá que os adapters leem.

O clique no botão (Slack modal / Teams dialog) acontece MUITO depois do turno
que gerou o guia; a única ponte é a linha de `wse_previews`, a mesma que já
carrega `deep_path`/`deep_note` (migração 0044, o molde). A armadilha
conhecida do `get_preview`: coluna nova precisa entrar nas DUAS listas
(SELECT e `keys`), senão grava e nunca aparece.
"""
from __future__ import annotations

from dse_contracts.activities import PreviewRef, TriggerPreviewInput

from dse_validation import db

_GUIA = {
    "steps": ["Abra /planos", "Clique em Nova Simulação", "Confira a projeção"],
    "login": "demo@acme.com / demo123 (supabase/seed.sql)",
}


def test_the_guide_round_trips_through_the_preview_row(work_item_id, tenant_id):
    db.upsert_preview(
        work_item_id=work_item_id, tenant_id=tenant_id, pr_number=7,
        repo="acme/app", status="created", url="https://p.example",
        test_guide=_GUIA,
    )
    row = db.get_preview(work_item_id)
    assert row is not None
    assert row["test_guide"] == _GUIA


def test_without_a_guide_the_row_reads_an_empty_object(work_item_id, tenant_id):
    db.upsert_preview(
        work_item_id=work_item_id, tenant_id=tenant_id, pr_number=7,
        repo="acme/app", status="created",
    )
    row = db.get_preview(work_item_id)
    assert row["test_guide"] == {}, "o default precisa ser objeto vazio, não NULL"


def test_the_contracts_carry_the_guide_with_an_empty_default():
    """PreviewRef/TriggerPreviewInput viajam o guia; ausência = {} (compat)."""
    ref = PreviewRef(work_item_id="wi_x", pr_number=7, status="created")
    assert ref.test_guide == {}
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", pr_number=7, repo="acme/app",
        head_sha="h", files_changed=[],
    )
    assert inp.test_guide == {}
    cheio = PreviewRef(work_item_id="wi_x", pr_number=7, status="created",
                       test_guide=_GUIA)
    assert cheio.test_guide["login"].startswith("demo@")
