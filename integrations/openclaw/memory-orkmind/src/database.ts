// Conexao PostgreSQL + pgvector para o plugin memory-orkmind
// Usa o driver pg (node-postgres) com queries diretas

import pg from "pg";

const { Pool } = pg;

let pool: pg.Pool | null = null;

export function getPool(databaseUrl: string): pg.Pool {
  if (!pool) {
    pool = new Pool({
      connectionString: databaseUrl,
      max: 5,
      idleTimeoutMillis: 30000,
      connectionTimeoutMillis: 10000,
    });
  }
  return pool;
}

export async function closePool(): Promise<void> {
  if (pool) {
    await pool.end();
    pool = null;
  }
}

// Valida se a tabela 'memories' e a extensao 'vector' existem no banco
export async function validateSchema(pool: pg.Pool): Promise<boolean> {
  try {
    const tableResult = await pool.query<{ exists: boolean }>(
      "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'memories') AS exists"
    );
    if (!tableResult.rows[0]?.exists) {
      console.error(
        "[memory-orkmind] Tabela 'memories' nao encontrada. Execute as migrations do OrkMind."
      );
      return false;
    }

    const vectorResult = await pool.query<{ exists: boolean }>(
      "SELECT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'vector') AS exists"
    );
    if (!vectorResult.rows[0]?.exists) {
      console.warn(
        "[memory-orkmind] Extensao 'vector' (pgvector) nao encontrada. Busca vetorial indisponivel."
      );
    }

    return true;
  } catch (err) {
    console.error("[memory-orkmind] Falha ao validar schema:", err);
    return false;
  }
}

export interface MemoryRow {
  id: string;
  content: string;
  collection: string;
  tags: Record<string, string[]>;
  priority: string;
  mandatory: boolean;
  source: string;
  created_at: string;
  updated_at: string;
  embedding: number[] | null;
  essence: string | null;
  score?: number;
}

/**
 * Carrega TODAS as regras mandatorias ativas (N1/N2).
 *
 * Nao usa embedder nem vetor: regras mandatorias sao carregadas por
 * predicado, nunca por similaridade. Filtro identico ao
 * `_load_mandatory_rules` do plugin Hermes (__init__.py:918-922).
 *
 * Deliberadamente SEM `LIMIT`: o corte por budget acontece na formatacao
 * (rules.ts), que avisa quantas regras foram omitidas. Um `LIMIT` aqui
 * seria descarte silencioso, exatamente o que o D-MA9 existe para evitar.
 */
export async function getMandatoryRules(pool: pg.Pool): Promise<MemoryRow[]> {
  const result = await pool.query<MemoryRow>(
    `SELECT id, content, collection, tags, priority, mandatory, source,
            created_at, updated_at, essence
     FROM memories
     WHERE mandatory = true
       AND injection_risk = false
       AND conflict = false
       AND (expires_at IS NULL OR expires_at > now())
     ORDER BY (priority = 'critical') DESC, created_at ASC`
  );
  return result.rows;
}

// Busca vetorial por similaridade cosseno no pgvector
//
// `excludeMandatory` e usado apenas pelo nivel 3 do hook (contexto
// recuperado): as mandatorias ja vao integralmente no system prompt, entao
// reinjeta-las no contexto duplicaria custo sem ganho. Tools e CLI seguem
// com o comportamento atual (false).
export async function searchByVector(
  pool: pg.Pool,
  embedding: number[],
  limit: number,
  minScore = 0.3,
  excludeMandatory = false
): Promise<MemoryRow[]> {
  const vectorStr = `[${embedding.join(",")}]`;
  const mandatoryFilter = excludeMandatory ? "AND mandatory = false" : "";
  const result = await pool.query<MemoryRow>(
    `SELECT id, content, collection, tags, priority, mandatory, source,
            created_at, updated_at, essence,
            1 - (embedding <=> $1::vector) AS score
     FROM memories
     WHERE embedding IS NOT NULL
       AND injection_risk = false
       AND conflict = false
       AND (expires_at IS NULL OR expires_at > now())
       ${mandatoryFilter}
     ORDER BY mandatory DESC, embedding <=> $1::vector
     LIMIT $2`,
    [vectorStr, limit]
  );
  return result.rows.filter((r) => (r.score ?? 0) >= minScore);
}

// Busca full-text como fallback quando nao ha embedding
export async function searchByText(
  pool: pg.Pool,
  query: string,
  limit: number,
  excludeMandatory = false
): Promise<MemoryRow[]> {
  const mandatoryFilter = excludeMandatory ? "AND mandatory = false" : "";
  const result = await pool.query<MemoryRow>(
    `SELECT id, content, collection, tags, priority, mandatory, source,
            created_at, updated_at, essence,
            ts_rank(to_tsvector('english', content), plainto_tsquery('english', $1)) AS score
     FROM memories
     WHERE to_tsvector('english', content) @@ plainto_tsquery('english', $1)
       AND injection_risk = false
       AND conflict = false
       AND (expires_at IS NULL OR expires_at > now())
       ${mandatoryFilter}
     ORDER BY score DESC
     LIMIT $2`,
    [query, limit]
  );
  return result.rows;
}

// Insere nova memoria no banco
export async function insertMemory(
  pool: pg.Pool,
  params: {
    id: string;
    content: string;
    collection: string;
    tags: Record<string, string[]>;
    priority: string;
    source: string;
    embedding: number[] | null;
  }
): Promise<void> {
  const vectorStr = params.embedding
    ? `[${params.embedding.join(",")}]`
    : null;
  await pool.query(
    `INSERT INTO memories (id, content, collection, tags, priority, source, embedding, created_at, updated_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7::vector, now(), now())
     ON CONFLICT (id) DO UPDATE SET
       content = EXCLUDED.content,
       collection = EXCLUDED.collection,
       tags = EXCLUDED.tags,
       priority = EXCLUDED.priority,
       embedding = EXCLUDED.embedding,
       updated_at = now(),
       version = memories.version + 1`,
    [
      params.id,
      params.content,
      params.collection,
      JSON.stringify(params.tags),
      params.priority,
      params.source,
      vectorStr,
    ]
  );
}

// Remove memoria por ID
export async function deleteMemoryById(
  pool: pg.Pool,
  memoryId: string
): Promise<boolean> {
  const result = await pool.query(
    `DELETE FROM memories WHERE id = $1 AND protected = false`,
    [memoryId]
  );
  return (result.rowCount ?? 0) > 0;
}

// Remove memorias por busca vetorial (deleta a mais similar acima de 90%)
export async function deleteByQuery(
  pool: pg.Pool,
  embedding: number[],
  limit: number
): Promise<{ deleted: boolean; candidates: MemoryRow[] }> {
  const encontrados = await searchByVector(pool, embedding, limit, 0.0);
  if (encontrados.length === 0) {
    return { deleted: false, candidates: [] };
  }
  // searchByVector passou a ordenar por `mandatory DESC` primeiro (para que
  // regras mandatorias nunca sejam cortadas pelo LIMIT em memory_search).
  // Aqui isso seria perigoso: elegeria uma regra mandatoria como "melhor"
  // candidata a remocao mesmo sendo menos similar. Reordenamos por score
  // para preservar exatamente a semantica anterior de memory_forget.
  const candidates = [...encontrados].sort(
    (a, b) => (b.score ?? 0) - (a.score ?? 0)
  );
  // Se o melhor resultado tem score >= 0.9, deleta automaticamente
  const best = candidates[0];
  if ((best.score ?? 0) >= 0.9) {
    const ok = await deleteMemoryById(pool, best.id);
    return { deleted: ok, candidates: ok ? [best] : candidates };
  }
  return { deleted: false, candidates };
}

// Busca memoria por ID
export async function getMemoryById(
  pool: pg.Pool,
  id: string
): Promise<MemoryRow | null> {
  const result = await pool.query<MemoryRow>(
    `SELECT id, content, collection, tags, priority, mandatory, source,
            created_at, updated_at, essence
     FROM memories
     WHERE id = $1
       AND (expires_at IS NULL OR expires_at > now())`,
    [id]
  );
  return result.rows[0] ?? null;
}

// Conta total de memorias
export async function countMemories(pool: pg.Pool): Promise<number> {
  const result = await pool.query<{ count: string }>(
    `SELECT count(*) as count FROM memories WHERE (expires_at IS NULL OR expires_at > now())`
  );
  return parseInt(result.rows[0]?.count ?? "0", 10);
}

// Lista memorias com paginacao
export async function listMemories(
  pool: pg.Pool,
  limit: number,
  orderByCreatedAt = false
): Promise<MemoryRow[]> {
  const order = orderByCreatedAt ? "created_at DESC" : "updated_at DESC";
  const result = await pool.query<MemoryRow>(
    `SELECT id, content, collection, tags, priority, mandatory, source,
            created_at, updated_at, essence
     FROM memories
     WHERE (expires_at IS NULL OR expires_at > now())
     ORDER BY ${order}
     LIMIT $1`,
    [limit]
  );
  return result.rows;
}
