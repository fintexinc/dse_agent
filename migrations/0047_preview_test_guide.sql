-- "How to test": o guia dinâmico do preview (passos + login de seed), gerado
-- no MESMO turno do deep link (0044) e lido pelos adapters quando o humano
-- clica o botão na mensagem final do Slack/Teams. JSONB `{steps: [...],
-- login: "..."}`; objeto vazio = sem guia (o comportamento de sempre).
--
-- A regra da 0044 continua valendo: a URL crua fica limpa (ela alimenta o
-- baseURL do Playwright no demo evidence) — o guia é campo separado, composto
-- só na apresentação.
ALTER TABLE wse_previews
    ADD COLUMN IF NOT EXISTS test_guide JSONB NOT NULL DEFAULT '{}'::jsonb;
