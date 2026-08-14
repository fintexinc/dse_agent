"""`teams` atravessa o contrato e TODOS os CHECKs de plataforma do banco.

O adapter de Teams foi entregue completo na Fase 4 e deixado desativado: o
`Platform` do contrato não conhece `teams` e os CHECKs do banco recusam a
string. O rascunho `services/adapter-teams/activation.sql` prevê DOIS CHECKs
(work_items.source, identity_links.platform) — medido em 2026-08-14, o banco
vivo tem QUATRO: `tenant_platform_bindings` e `repo_bindings` também enumeram
plataforma, e `repo_bindings` nem existia quando o rascunho foi escrito
(migração 0023). Sem os quatro não dá para vincular um canal do Teams a um
tenant nem a repositórios — ou seja, a ativação pela metade produz um adapter
que recebe a mensagem e não tem para onde levá-la.

Este teste lê as migrações (não o banco): é o corpus que precisa estar certo
para qualquer ambiente novo, e roda no grupo `tooling`, sem Postgres.
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _ROOT / "migrations"

#: Tabela -> coluna que enumera a plataforma de origem.
_PLATFORM_CHECKS = {
    "work_items": "source",
    "identity_links": "platform",
    "tenant_platform_bindings": "platform",
    "repo_bindings": "platform",
}


def _effective_allowed_values(table: str, column: str) -> set[str] | None:
    """Conjunto aceito pelo CHECK vigente da coluna, aplicando as migrações em
    ordem: a ÚLTIMA definição encontrada é a que vale (uma migração posterior
    pode dropar e recriar o CHECK)."""
    allowed: set[str] | None = None
    pattern = re.compile(
        rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
    for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        text = migration.read_text()
        # só olha o trecho que fala desta tabela (CREATE TABLE ou ALTER TABLE)
        for block in re.split(r"(?=CREATE\s+TABLE|ALTER\s+TABLE)", text, flags=re.IGNORECASE):
            if not re.search(rf"\b{table}\b", block, re.IGNORECASE):
                continue
            found = pattern.findall(block)
            if found:
                allowed = {v.strip().strip("'\"") for v in found[-1].split(",")}
    return allowed


def test_every_platform_check_accepts_teams() -> None:
    missing = {}
    for table, column in _PLATFORM_CHECKS.items():
        allowed = _effective_allowed_values(table, column)
        assert allowed, f"não achei o CHECK de {table}.{column} nas migrações"
        if "teams" not in allowed:
            missing[f"{table}.{column}"] = sorted(allowed)
    assert not missing, (
        "CHECK de plataforma que ainda recusa 'teams' — o adapter recebe a "
        f"mensagem e não consegue gravar/vincular: {missing}"
    )


def test_the_contract_knows_teams() -> None:
    import sys

    sys.path.insert(0, str(_ROOT / "packages" / "contracts"))
    from dse_contracts.conversation_event import Platform

    assert "teams" in {p.value for p in Platform}, (
        "Platform não tem `teams`: o ConversationEvent do adapter nem chega a "
        "ser construído (o adapter devolve 501 teams_not_activated)"
    )
