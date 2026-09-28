-- F4: idempotencia dura por content_hash (solucao "sempre gravar")
--
-- Cria o indice unico parcial que torna impossivel gravar duas entries
-- com o mesmo (collection, content_hash). E a garantia no nivel do
-- Postgres que sustenta o drainer da spool: mesmo com dois drainers
-- concorrentes, o segundo INSERT falha e o item e reconhecido como
-- duplicata (status "duplicate") em vez de virar memoria repetida.
--
-- Esta migracao e idempotente: pode ser executada multiplas vezes.
--
-- ATENCAO (regra constitucional "nada se perde"): esta migracao NAO
-- apaga nem funde entries duplicadas. Se ja existirem duplicatas, a
-- criacao do indice falha de proposito e exige revisao humana. Use a
-- consulta de diagnostico abaixo para listar os casos antes de rodar.
--
--   SELECT collection, content_hash, count(*) AS total,
--          array_agg(id ORDER BY created_at) AS ids
--     FROM memories
--    WHERE content_hash IS NOT NULL
--    GROUP BY collection, content_hash
--   HAVING count(*) > 1
--    ORDER BY total DESC;
--
-- A resolucao de duplicatas preexistentes e decisao humana (versionar,
-- reclassificar de colecao ou marcar metadata), nunca DELETE automatico.

-- 1. Indice unico parcial: idempotencia dura por (colecao, hash).
--    O WHERE garante que entries legadas sem content_hash nao sao
--    afetadas (NULL nunca colide).
CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_collection_content_hash
ON memories (collection, content_hash)
WHERE content_hash IS NOT NULL;

-- 2. Indice de apoio ao lookup por hash do drainer (GET /entries/by-hash).
--    O indice unico acima ja atende (collection, content_hash), mas o
--    lookup por hash sem colecao usa este.
CREATE INDEX IF NOT EXISTS idx_memories_content_hash
ON memories (content_hash)
WHERE content_hash IS NOT NULL;
