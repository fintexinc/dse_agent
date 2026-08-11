-- Um binding passa a poder listar VÁRIOS repositórios.
--
-- A chave primária antiga — (tenant_id, platform, binding_type, binding_value)
-- — dizia, na estrutura, que uma origem tem no máximo um repositório. Isso
-- valia enquanto o binding respondia "qual repo"; deixou de valer quando o
-- roteador passou a existir, porque a pergunta útil virou "quais repos esta
-- origem pode alcançar". Com o repo na chave, um canal (ou um project do Jira)
-- carrega quantas linhas precisar.
--
-- Nada a migrar em dados: linhas existentes já são únicas por (origem, repo),
-- então a chave nova as aceita como estão. A antiga era ESTRITAMENTE mais
-- restritiva, e é por isso que esta migração não pode perder linha.
ALTER TABLE repo_bindings DROP CONSTRAINT repo_bindings_pkey;
ALTER TABLE repo_bindings ADD CONSTRAINT repo_bindings_pkey
      PRIMARY KEY (tenant_id, platform, binding_type, binding_value, repo);

-- O conjunto que a origem delimitou, decidido no momento da admissão.
--
-- Guardado aqui, e não recalculado depois, porque os sinais que produziram o
-- recorte (o `component` de um issue do Jira, por exemplo) NÃO sobrevivem em
-- `source_ref` — só o `ticket_key` sobrevive. Recalcular exigiria duplicar a
-- montagem de sinais de cada adapter num segundo lugar, que é a forma como as
-- três cópias da consulta de repos do tenant divergiram e rotearam um pedido
-- de frontend para o backend (#56).
--
-- Vazio é o normal e significa "sem recorte": o roteador segue vendo o
-- catálogo do tenant inteiro, exatamente como antes desta migração.
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS repo_candidates text[] NOT NULL DEFAULT '{}';
