-- A1, reparo de DADOS: irmãos de fan-out nascidos ANTES do fix (rc.59)
-- copiaram o source_ref inteiro do primário e herdaram o bot_ts alheio — o
-- clique no prompt do primário correlacionava com o irmão mais novo
-- (declined_unexpected_status medido 2x). O código novo não herda mais; as
-- linhas velhas precisam do mesmo conserto, senão o par VIVO continua
-- desviando cliques para sempre.
--
-- Remove de cada irmão os bot_ts que TAMBÉM estão na lista do primário: desde
-- o F1(b) cada mensagem é registrada só no item sobre o qual foi postada, então
-- sobreposição = herança, nunca legitimidade. Idempotente por construção.
UPDATE work_items sib
SET source_ref = jsonb_set(
        sib.source_ref, '{bot_ts}',
        COALESCE(
            (SELECT jsonb_agg(t.e)
               FROM jsonb_array_elements(sib.source_ref->'bot_ts') AS t(e)
              WHERE NOT (COALESCE(prim.source_ref->'bot_ts', '[]'::jsonb)
                         @> jsonb_build_array(t.e))),
            '[]'::jsonb
        )
    )
FROM work_items prim
WHERE sib.group_id IS NOT NULL
  AND sib.group_id = prim.id
  AND sib.id <> prim.id
  AND sib.source_ref ? 'bot_ts';
