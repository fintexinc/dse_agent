-- rc.104 — os três índices que as consultas QUENTES pedem e não tinham.
--
-- Medido em produção (2026-08-20): o EXPLAIN da leitura de `payload` por work
-- item devolve `Seq Scan on ingest_events  Filter: (work_item_id = ... AND
-- kind = ...)`. Essa consulta roda UMA VEZ POR ITEM REPROJETADO. Os dois
-- índices que a tabela já tem cobrem outras perguntas (não-processados,
-- recebidos_em) e a FK não cria índice nenhum.
--
-- `work_items` e `work_item_evidence`: o projetor varre por KEYSET
-- `(updated_at, id)` e `(updated_at, work_item_id)` a cada passada — 43.200
-- passadas por dia — e nenhuma das duas tinha índice nessa ordem.
--
-- SEM `CONCURRENTLY`, de propósito: o runner (`scripts/migrate.py`) roda cada
-- arquivo numa transação, e CONCURRENTLY é proibido dentro de uma. As três
-- tabelas têm centenas de linhas hoje, então o lock é sub-segundo — trocar o
-- runner por causa disto seria acrescentar complexidade para pagar um custo
-- que não existe. Se algum dia uma delas ficar grande, o índice já estará lá.
CREATE INDEX IF NOT EXISTS idx_ingest_events_work_item_kind
    ON ingest_events (work_item_id, kind, id);

CREATE INDEX IF NOT EXISTS idx_work_items_projector_keyset
    ON work_items (updated_at, id);

CREATE INDEX IF NOT EXISTS idx_work_item_evidence_projector_keyset
    ON work_item_evidence (updated_at, work_item_id);

-- E a função de partição por tenant que nunca teve um chamador. Nove linhas de
-- DDL descrevendo um onboarding que não existe: `create_tenant_audit_partition`
-- é definida em 0001 e nenhum serviço, script ou migração a invoca — todo o
-- audit_log vive em `audit_log_default`. Particionar por tenant também não
-- ajudaria o crescimento (o custo é temporal, não por tenant); essa decisão é
-- de roadmap e está registrada na auditoria de escala, não aqui.
DROP FUNCTION IF EXISTS create_tenant_audit_partition(TEXT);
