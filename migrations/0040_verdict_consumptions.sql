-- rc do canal mínimo, item 3 — consumo one-shot de veredito NO CANAL.
--
-- O workflow já consome one-shot do lado dele (a flag é lida e resetada no
-- laço de espera), mas isso não impede o SEGUNDO signal de um clique duplicado
-- de re-armar a flag depois do consumo — e o próximo parque do mesmo item se
-- auto-resolveria com o veredito velho (a classe do wi_8edaef39). O anticorpo
-- é na BORDA: o primeiro clique consome a decisão aqui (INSERT ... ON CONFLICT
-- DO NOTHING, atômico); o duplicado não grava evento nenhum e quem clicou
-- tarde recebe "já resolvido por X às HH:MM".
--
-- A chave é (work_item_id, stage) — NÃO o ts da mensagem: a mensagem de status
-- é UMA por item, editada in-place, então o ts é o mesmo para o prompt de
-- approve e para o parque. `stage` distingue a decisão; o RE-ARM acontece
-- quando o adapter renderiza botões daquele stage de novo (re-parque legítimo
-- do mesmo item = nova decisão), via DELETE antes do render.
CREATE TABLE IF NOT EXISTS verdict_consumptions (
    work_item_id TEXT NOT NULL,
    stage        TEXT NOT NULL,
    consumed_by  TEXT NOT NULL,
    consumed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (work_item_id, stage)
);

GRANT SELECT, INSERT, DELETE ON verdict_consumptions TO dse_app;
