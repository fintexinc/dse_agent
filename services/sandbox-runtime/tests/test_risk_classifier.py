"""O classificador de risco volta a ter três níveis vivos.

Medido (rc.89): `diff_budget_lines` era uma constante 400 do contrato — nunca
dimensionada, nunca estimada — e `classify_risk_class` a comparava com 300/800.
Resultado: `400 > 300` sempre verdadeiro → TODO plano saía no mínimo "medium";
o ramo `> 800 → high` e o `return "low"` eram código morto. O "Risk:" que o
aprovador lia no modal era parcialmente artefato da constante.

Agora o classificador recebe a ESTIMATIVA do Planner (`estimated_lines`,
opcional): com ela, os limiares valem de verdade (um plano de 1200 linhas
parqueia como high); sem ela (None), o risco vem só de arquivos/globs — não se
inventa um número para classificar, que seria recriar o 400 com outro nome.

O gate humano não muda de população por medium→low: `require_approval_risk_classes`
exige só "high" (models.py). O que muda é que "high" por tamanho passa a existir.
"""
from __future__ import annotations

from sandbox_runtime.sessions import classify_risk_class

_FORBIDDEN = [".github/workflows/", "migrations/"]


def test_estimate_above_800_is_high():
    """O ramo que era código morto: com a constante 400, `> 800` nunca disparava."""
    assert classify_risk_class(["src/app.py"], 1200, _FORBIDDEN) == "high"


def test_estimate_above_300_is_medium():
    assert classify_risk_class(["src/app.py"], 350, _FORBIDDEN) == "medium"


def test_small_estimate_clean_files_is_low():
    """Impossível antes: 400 constante > 300 forçava medium em todo plano."""
    assert classify_risk_class(["src/app.py", "src/util.py"], 40, _FORBIDDEN) == "low"


def test_no_estimate_falls_back_to_files_and_globs_only():
    """Sem estimativa, os critérios de linhas são PULADOS — nada de número
    inventado. O risco vem dos arquivos."""
    assert classify_risk_class(["src/app.py", "src/util.py"], None, _FORBIDDEN) == "low"
    # glob de risco médio continua valendo sozinho
    assert classify_risk_class(["src/config/settings.py"], None, _FORBIDDEN) == "medium"
    # muitos arquivos continuam valendo sozinhos
    muitos = [f"src/f{i}.py" for i in range(16)]
    assert classify_risk_class(muitos, None, _FORBIDDEN) == "medium"


def test_forbidden_and_high_globs_still_dominate():
    """Uma estimativa pequena não rebaixa o que os caminhos dizem."""
    assert classify_risk_class(["migrations/0099_x.sql"], 10, _FORBIDDEN) == "high"
    assert classify_risk_class(["src/core/auth_service.py"], 10, _FORBIDDEN) == "high"


# ---------------------------------------------------------------------------
# O matcher de `forbidden_paths` é UM só (2026-08-19)
# ---------------------------------------------------------------------------
# Este classificador usava `startswith`/`fnmatch` ancorado na raiz enquanto o
# gate L1 (`plan_compliance._is_forbidden`) casa segmento em qualquer
# profundidade. Em monorepo os dois davam respostas OPOSTAS para o mesmo
# arquivo: aqui "low", lá violação. O efeito prático não é de rótulo — com
# "low" o plano nem chega ao gate humano (a política parqueia só "high"), e o
# caminho protegido só aparece depois, quando o L1 reprova um diff que ninguém
# autorizou. O matcher do gate, que é o mais estrito e o que decide de verdade,
# virou a implementação única (dse_contracts.paths.first_forbidden_match).


def test_a_protected_path_inside_a_monorepo_package_is_high():
    assert classify_risk_class(
        ["packages/web/.github/workflows/ci.yml"], 40, _FORBIDDEN
    ) == "high"


def test_a_directory_named_like_a_protected_one_is_not_high():
    """Alcançar mais fundo não pode trocar o falso negativo por falso positivo.

    `medium` e não `low` porque `**/*.sql` está em _MEDIUM_RISK_GLOBS — outra
    regra, que este teste não julga. O que ele pina é que `migrations_backup/`
    NÃO é `migrations/`."""
    assert classify_risk_class(
        ["services/api/migrations_backup/0001.sql"], 40, _FORBIDDEN
    ) != "high"
