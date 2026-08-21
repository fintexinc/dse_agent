"""WSA-E6-T1 — Path A/B correlation.

For an incoming `ConversationEvent`, `correlate(...)` decides whether it opens
a new task (Path A, "new_task" -> `admit_work_item`) or is a signal to a task
already in flight (Path B, "signal" -> `SignalWorkflow`, decided by the
caller: the dispatcher itself or WS-B via the Temporal client).

Deterministic lookup by `source_ref` (thread_ts/PR number/ticket) against
`work_items` with a NON-terminal status. `source_ref` convention used for the
match (see adapters): Slack `{"channel":..., "thread_ts":...}`, GitHub
`{"repo":..., "number":...}` (the same number covers issue and PR — the GitHub
API shares the number namespace between them).

Quem está no canal fala com o DSE (decisão do operador, 2026-08-21). A allowlist
de DIREÇÃO — que fazia o comentário de um terceiro virar
`steering_rejected_unauthorized` e sumir em silêncio — saiu daqui, do Slack e do
Teams. O convite ao canal É a autorização: quem tem acesso já lê tudo que o DSE
escreve ali (plano, arquivos tocados, veredito dos gates), e a assimetria de
poder ler e não poder responder custava mais do que protegia. Cada superfície
nova recriava o problema, porque a mesma pessoa tem uma identidade por
plataforma e nenhuma delas nasce na lista.

Isto NÃO afrouxou a aprovação de plano: `approval` nunca passou por este gate.
Quem pode aprovar segue resolvido pela cascata própria (CODEOWNERS →
aprovadores designados do access bundle), numa activity do orchestrator.

Event correlated to a WorkItem already in a TERMINAL state (done/failed): by
definition it cannot receive a signal (the workflow has already ended) — the
documented rule is to allow creating a NEW WorkItem with a provenance link to
the previous one (`provenance_work_item_id`), recorded by the caller in the
`details` of the admission audit row.
"""
from __future__ import annotations

import json
from typing import Any, Literal, NamedTuple

from dse_contracts import ConversationEvent, WorkItemStatus


CorrelationKind = Literal["new_task", "signal"]

_TERMINAL_STATUSES = {WorkItemStatus.done.value, WorkItemStatus.failed.value}


class CorrelationResult(NamedTuple):
    kind: CorrelationKind
    work_item_id: str | None
    provenance_work_item_id: str | None = None


def correlate(
    conn,
    *,
    tenant_id: str,
    event: ConversationEvent,
    requester_principal: str,
    correlation_ref: dict[str, Any] | None = None,
) -> CorrelationResult:
    """Correlaciona um evento a um WorkItem: tarefa nova, ou sinal para uma
    que já existe. Leitura pura — não escreve nem commita."""
    ref = correlation_ref if correlation_ref is not None else event.source_ref

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, requester FROM work_items
            WHERE tenant_id = %s AND source_ref @> %s::jsonb
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tenant_id, json.dumps(ref)),
        )
        row = cur.fetchone()

    if row is None:
        return CorrelationResult("new_task", None)

    matched_id, status, wi_requester = row

    if status in _TERMINAL_STATUSES:
        # Documented rule: a terminal WorkItem does not receive a signal — it
        # becomes a new WorkItem with provenance to the previous one.
        return CorrelationResult("new_task", None, provenance_work_item_id=matched_id)

    return CorrelationResult("signal", matched_id)
