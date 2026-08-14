"""Diff Angular só-de-estado tem que cair na cadeia `ui` do preview.

Auditoria 2026-08-14 (wi_e15f4991, PR #26 do bmo-fee-calculator-fe-dse):
o diff mudou APENAS store/reducers/selectors/types `.ts` — nenhum template.
Nenhum glob de ui casou; o `**/*.ts` do deployable ganhou; a cadeia
deployable roda numa imagem sem node → `sh: 1: npm: not found` → 900s de
espera → degraded. É a SEGUNDA encarnação do wi_cc72b204 — a primeira
ganhou o glob `.component.ts`, que não cobre os arquivos de estado.

O fix cobre o `src/app/**` (convenção do Angular CLI) sem tocar a
precedência: ui ganha quando casa; back Java (`src/main/**`) segue
deployable.
"""
from dse_contracts.activities import TriggerPreviewInput
from dse_validation.preview.paths_filter import preview_decision

_UI_GLOBS = TriggerPreviewInput.model_fields["ui_path_globs"].default_factory()
_DEP_GLOBS = TriggerPreviewInput.model_fields["deployable_globs"].default_factory()

_STATE_ONLY_DIFF = [
    "src/app/admin/grid-payout/store/grid-payouts.types.ts",
    "src/app/admin/grid-payout/store/reducers/grid-payouts.reducer.ts",
    "src/app/admin/grid-payout/store/selectors/grid-payouts.selectors.ts",
    "src/app/admin/grid-payout/store/reducers/grid-payouts.reducer.retire.spec.ts",
]


def test_state_only_angular_diff_is_ui_not_deployable():
    kind, matched = preview_decision(_STATE_ONLY_DIFF, _UI_GLOBS, _DEP_GLOBS)
    assert kind == "ui", (
        f"diff só-de-estado do Angular caiu em {kind!r} — a cadeia deployable "
        f"não tem npm e o preview degrada sempre (matched={matched})"
    )


def test_java_backend_diff_still_lands_on_the_deployable_chain():
    kind, _ = preview_decision(
        ["src/main/java/com/fintex/bmofeecalculatorbe/controller/RestController.java"],
        _UI_GLOBS, _DEP_GLOBS,
    )
    assert kind == "deployable", "o fix do src/app/** não pode capturar o back Java"
