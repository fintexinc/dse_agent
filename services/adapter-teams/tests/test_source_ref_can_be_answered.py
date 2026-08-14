"""O `source_ref` do Teams tem que carregar o endereço COMPLETO da resposta.

Encontrado ao ativar (2026-08-14): `source_ref` guardava só o
`conversation_id`. Mas quem responde é o orchestrator, horas depois, por
`post_tracking_comment` → `_resolve_comment_target`, e endereçar uma mensagem
no connector do Bot Framework exige DOIS valores: o `service_url` (regional,
que chega em cada Activity e varia por tenant/região) e o `conversation_id`.
Sem o primeiro, o resolvedor devolve None, a Activity reporta
`{"ok": True, "skipped": ...}` e o item anda inteiro sem NADA aparecer no
canal — a falha silenciosa que o adapter foi construído para não ter.

Diferente do Slack, onde o `channel` basta porque a URL da API é global.
"""
from __future__ import annotations

from adapter_teams import events

from helpers import teams_activity


def test_source_ref_carries_the_service_url():
    ref = events.source_ref(teams_activity())
    assert ref.get("service_url") == "https://smba.trafficmanager.net/emea/", (
        "sem service_url o orchestrator não consegue responder: "
        f"_resolve_comment_target devolve None e o status some — {ref}"
    )
    assert ref["conversation_id"] == "19:channel_abc@thread.tacv2"


def test_the_saved_ref_is_exactly_what_the_orchestrator_needs():
    """Contrato entre os dois lados, pinado nos dois: o que o adapter GRAVA
    tem que ser suficiente para o resolvedor do orchestrator ENDEREÇAR."""
    ref = events.source_ref(teams_activity())
    required = {"service_url", "conversation_id"}
    assert required <= set(ref), (
        f"chaves que o _resolve_comment_target exige, faltando: {required - set(ref)}"
    )
    assert all(ref[key] for key in required), f"chave vazia em {ref}"
