"""O deep link é PROVADO contra o serviço vivo antes de virar botão.

Medido no wi_f1f27266 (glide-path, 2026-08-31): o modelo resolveu `/version`
lendo o patch da PR — e o patch não contém o main.ts, onde vivem
`setGlobalPrefix("api")` e o versionamento de URI. O app serve tudo sob
`/api/v1/...`; o How to test apontou `/version`; o operador clicou num 404.
O botão existe exatamente para mostrar a rota funcionando — link que cai em
404 é o entregável falhando na mão de quem pediu.

O modelo continua NUNCA compondo URL (o portão `_validate_path` fica). O que
entra é medição: com o preview Ready, o caminho declarado e as variantes de
prefixo comuns são tentados contra o serviço INTERNO, e vence o primeiro que
responder algo ≠ 404. Nenhum respondeu, ou o probe quebrou → o caminho
declarado fica como está (fail-open: um probe morto não pode piorar o link).
"""
from __future__ import annotations

from dse_validation.preview.deep_link import reconcile_deep_path, rewrite_guide_paths


def _probe(respostas: dict[str, int | None]):
    chamados: list[str] = []

    def probe(candidato: str) -> int | None:
        chamados.append(candidato)
        return respostas.get(candidato, 404)

    probe.chamados = chamados  # type: ignore[attr-defined]
    return probe


def test_the_measured_case_the_prefixed_variant_wins():
    """wi_f1f27266: /version 404, /api/v1/version 200."""
    probe = _probe({"/version": 404, "/api/version": 404, "/api/v1/version": 200})
    assert reconcile_deep_path("/version", probe) == "/api/v1/version"


def test_a_declared_path_that_answers_is_kept_untouched():
    probe = _probe({"/health": 200})
    assert reconcile_deep_path("/health", probe) == "/health"
    assert probe.chamados == ["/health"], "respondeu: nenhuma variante é tentada"


def test_auth_walls_count_as_existing():
    """401/403 é rota que EXISTE (atrás de login) — não se segue procurando."""
    probe = _probe({"/admin": 401})
    assert reconcile_deep_path("/admin", probe) == "/admin"


def test_when_nothing_answers_the_declared_path_stays():
    probe = _probe({})  # tudo 404
    assert reconcile_deep_path("/version", probe) == "/version"


def test_a_broken_probe_changes_nothing():
    probe = _probe({"/version": None})
    assert reconcile_deep_path("/version", probe) == "/version"
    assert probe.chamados == ["/version"], "probe morto: aborta, não insiste"


def test_no_path_no_probe():
    probe = _probe({})
    assert reconcile_deep_path(None, probe) is None
    assert reconcile_deep_path("", probe) in ("", None)
    assert probe.chamados == []


def test_the_guide_steps_follow_the_reconciled_path():
    guide = {"login": "", "steps": [
        "Open /version in the browser or curl it",
        "Verify response is valid JSON",
    ]}
    novo = rewrite_guide_paths(guide, "/version", "/api/v1/version")
    assert novo["steps"][0] == "Open /api/v1/version in the browser or curl it"
    assert novo["steps"][1] == "Verify response is valid JSON"
    # o original não é mutado (o dict pode já ter ido para outro consumidor)
    assert guide["steps"][0] == "Open /version in the browser or curl it"


def test_guide_rewrite_is_a_noop_when_the_path_did_not_change():
    guide = {"steps": ["Open /version"]}
    assert rewrite_guide_paths(guide, "/version", "/version") == guide
