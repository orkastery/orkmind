-- F3: Migracao de embedding vector(1536) para vector(1024)
--
-- Modelo: perplexity/pplx-embed-v1-0.6b (dimensao fixa 1024)
-- Esta migracao e idempotente: pode ser executada multiplas vezes.
--
-- ATENCAO: as entries existentes com embedding NULL nao sao afetadas.
-- Entries com embedding de 1536 dimensoes terao o vetor truncado
-- para 1024 (as primeiras 1024 componentes sao preservadas).

-- 1. Remover indice HNSW existente (se houver)
DROP INDEX IF EXISTS idx_memories_embedding;

-- 2. Alterar tipo da coluna para vector(1024)
ALTER TABLE memories ALTER COLUMN embedding TYPE vector(1024)
    USING CASE
        WHEN embedding IS NULL THEN NULL
        ELSE embedding::text::vector(1024)
    END;

-- 3. Recriar indice HNSW com a nova dimensao
CREATE INDEX IF NOT EXISTS idx_memories_embedding
ON memories USING hnsw (embedding vector_cosine_ops);
