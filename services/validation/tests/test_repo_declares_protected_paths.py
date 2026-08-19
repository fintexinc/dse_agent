"""O repo declara quais caminhos ele protege — e a PR não afrouxa a si mesma.

`forbidden_paths` era uma constante nossa: `[".github/workflows/",
"migrations/"]`, escrita no primeiro commit deste repositório, sem uma linha de
justificativa, e nunca revista. Todo plano de todo repo — três repos, tarefas
distintas — saía com exatamente essa lista, e ela é a razão pela qual "adicione
um workflow do GitHub Actions" era uma tarefa impossível por construção.

Uma lista de proteção que a plataforma inventa para todo mundo protege mal duas
vezes: cobre o que não importa naquele repo e deixa de fora o que importa. Ela
passa a morar no `.dse/validation.json`, ao lado do bloco `preview` (G7) — mesmo
mecanismo, mesma whitelist.

O que torna isso DEFENSÁVEL é ONDE o manifesto é lido: no BASE SHA imutável
(`L1Config.from_trusted_manifest`, e a leitura por API usa `base_sha` como ref).
Uma PR não consegue afrouxar a própria guarda: para mudar a proteção é preciso
um merge revisado por humano, e só o item SEGUINTE enxerga a mudança.

Repo que não declara nada continua com o default de hoje — a dívida sai por
adição.
"""
from __future__ import annotations

import pytest

from dse_contracts import GateStatus

from dse_validation.config import L1ManifestError

try:  # o vermelho: o parser ainda não existe
    from dse_validation.config import parse_repo_forbidden_paths
except ImportError:  # pragma: no cover - some assim que o verde entra
    parse_repo_forbidden_paths = None  # type: ignore[assignment]


def _parse(payload: dict, source: str = "manifest"):
    assert parse_repo_forbidden_paths is not None, (
        "dse_validation.config.parse_repo_forbidden_paths não existe — o repo "
        "ainda não consegue declarar os próprios caminhos protegidos"
    )
    return parse_repo_forbidden_paths(payload, source=source)


def test_a_repo_that_says_nothing_keeps_the_platform_default():
    """`None` e não `[]`: a diferença entre "não declarei" e "declarei que não
    protejo nada" é a diferença entre herdar o default e abrir mão dele."""
    assert _parse({"version": 1, "commands": {}}) is None


def test_the_declared_list_is_what_the_plan_carries():
    assert _parse(
        {"version": 1, "commands": {}, "forbidden_paths": ["config/production/", "infra/live/"]}
    ) == ["config/production/", "infra/live/"]


def test_a_repo_can_declare_that_it_protects_nothing():
    """Lista vazia é uma DECISÃO, e ela custa um merge revisado por humano no
    base branch — o item em voo lê o manifesto do base SHA, então a PR que
    esvazia a lista não se beneficia dela."""
    assert _parse({"version": 1, "commands": {}, "forbidden_paths": []}) == []


def test_an_entry_that_means_everything_is_refused():
    """`"/"` e `""` normalizam para "a raiz inteira": aceitar isso é transformar
    uma proteção em uma parede, e o sintoma apareceria só no L1, como um item
    reprovando sem explicação."""
    for entrada in ("", "   ", "/"):
        with pytest.raises(L1ManifestError) as exc:
            _parse({"version": 1, "commands": {}, "forbidden_paths": [entrada]})
        assert exc.value.status == GateStatus.ERROR


def test_a_traversal_entry_is_refused():
    with pytest.raises(L1ManifestError):
        _parse({"version": 1, "commands": {}, "forbidden_paths": ["../../etc/"]})


def test_the_wrong_shape_is_refused_with_a_readable_reason():
    with pytest.raises(L1ManifestError) as exc:
        _parse({"version": 1, "commands": {}, "forbidden_paths": "migrations/"})
    assert "forbidden_paths" in str(exc.value.detail)
    with pytest.raises(L1ManifestError):
        _parse({"version": 1, "commands": {}, "forbidden_paths": [{"path": "migrations/"}]})


def test_the_manifest_gate_stops_rejecting_the_whole_file():
    """A pegadinha real: enquanto `forbidden_paths` não estivesse na whitelist,
    um repo que a declarasse veria o manifesto INTEIRO reprovado como "unknown
    fields" — lint, typecheck, test e build sumiriam junto."""
    from dse_validation.config import L1Config

    cfg = L1Config._from_manifest_payload(
        {
            "version": 1,
            "commands": {"lint": ["ruff", "check", "."]},
            "forbidden_paths": ["config/production/"],
        },
        source="base:.dse/validation.json",
    )
    assert cfg.manifest_status == GateStatus.PASS, cfg.manifest_detail
    assert cfg.lint_cmd == ["ruff", "check", "."]
