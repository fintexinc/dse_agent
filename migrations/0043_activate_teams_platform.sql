-- Ativa `teams` como plataforma de primeira classe.
--
-- O adapter (services/adapter-teams) foi entregue completo na Fase 4 e ficou
-- desativado por decisão de roadmap: o registro do bot só existiu em
-- 2026-08-14 (Azure Bot `dse-teams-bot`, single-tenant). Enquanto isso, a
-- string 'teams' era recusada pelo banco, e `platform_compat.is_activated()`
-- lê exatamente esse estado para devolver 501 antes de qualquer escrita.
--
-- São QUATRO CHECKs, não dois. O rascunho services/adapter-teams/activation.sql
-- (Fase 4) previa work_items.source e identity_links.platform; medido no banco
-- vivo em 2026-08-14, `tenant_platform_bindings` e `repo_bindings` também
-- enumeram plataforma — o segundo nem existia quando o rascunho foi escrito
-- (migração 0023). Deixar esses dois de fora daria um adapter que recebe a
-- mensagem e não tem como resolver tenant nem repositório: o item nasceria sem
-- destino.
--
-- Tudo aditivo: os CHECKs só ganham um valor a mais, nenhuma linha existente
-- deixa de ser válida, e não há dado a migrar. Idempotente por construção
-- (DROP IF EXISTS + ADD com o conjunto completo).

ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_source_check;
ALTER TABLE work_items ADD CONSTRAINT work_items_source_check
    CHECK (source IN ('slack', 'github', 'jira', 'teams'));

ALTER TABLE identity_links DROP CONSTRAINT IF EXISTS identity_links_platform_check;
ALTER TABLE identity_links ADD CONSTRAINT identity_links_platform_check
    CHECK (platform IN ('slack', 'github', 'jira', 'teams'));

ALTER TABLE tenant_platform_bindings
    DROP CONSTRAINT IF EXISTS tenant_platform_bindings_platform_check;
ALTER TABLE tenant_platform_bindings ADD CONSTRAINT tenant_platform_bindings_platform_check
    CHECK (platform IN ('slack', 'github', 'jira', 'teams'));

ALTER TABLE repo_bindings DROP CONSTRAINT IF EXISTS repo_bindings_platform_check;
ALTER TABLE repo_bindings ADD CONSTRAINT repo_bindings_platform_check
    CHECK (platform IN ('slack', 'jira', 'github', 'teams'));
