-- rc.103 — deep link do preview: o caminho decidido por LLM (validado pela
-- plataforma) e a nota de 1 linha. Colunas separadas da `url` DE PROPÓSITO:
-- a url crua alimenta o baseURL do Playwright no demo evidence, e um path
-- dentro dela re-rootaria as navegações. A composição url+deep_path acontece
-- só na apresentação (PR body, Slack, comentário de evidência).
ALTER TABLE wse_previews ADD COLUMN IF NOT EXISTS deep_path TEXT NOT NULL DEFAULT '';
ALTER TABLE wse_previews ADD COLUMN IF NOT EXISTS deep_note TEXT NOT NULL DEFAULT '';
