// Modulo de semeadura (seed) de memorias para o plugin memory-orkmind
// Suporta importacao de fatos a partir de arquivos do workspace ou JSON externo

import { createHash, randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import type pg from "pg";
import type { EmbeddingClient } from "./embeddings.js";

export interface SeedEntry {
  content: string;
  collection: string;
  priority?: string;
  tags?: Record<string, string[]>;
}

// Gera hash SHA-256 do conteudo para idempotencia
function contentHash(text: string): string {
  return createHash("sha256").update(text.trim()).digest("hex");
}

// Verifica se uma memoria com o mesmo content_hash ja existe
async function alreadyExists(pool: pg.Pool, hash: string): Promise<boolean> {
  const result = await pool.query<{ count: string }>(
    "SELECT count(*) as count FROM memories WHERE content_hash = $1",
    [hash]
  );
  return parseInt(result.rows[0]?.count ?? "0", 10) > 0;
}

// Insere uma entrada de seed, gerando embedding se possivel
async function insertSeedEntry(
  pool: pg.Pool,
  entry: SeedEntry,
  embedder: EmbeddingClient | null
): Promise<{ inserted: boolean; reason?: string }> {
  const hash = contentHash(entry.content);

  if (await alreadyExists(pool, hash)) {
    return { inserted: false, reason: "ja existe (content_hash duplicado)" };
  }

  let embedding: number[] | null = null;
  if (embedder) {
    try {
      embedding = await embedder.embed(entry.content);
    } catch (err) {
      console.warn(
        `[memory-orkmind] seed: embedding falhou para "${entry.content.slice(0, 60)}...", salvando sem vetor`
      );
    }
  }

  const vectorStr = embedding ? `[${embedding.join(",")}]` : null;
  const id = randomUUID();

  await pool.query(
    `INSERT INTO memories (id, content, collection, tags, priority, source, embedding, content_hash, created_at, updated_at)
     VALUES ($1, $2, $3, $4, $5, 'bootstrap', $6::vector, $7, now(), now())
     ON CONFLICT (id) DO NOTHING`,
    [
      id,
      entry.content,
      entry.collection,
      JSON.stringify(entry.tags ?? {}),
      entry.priority ?? "medium",
      vectorStr,
      hash,
    ]
  );

  return { inserted: true };
}

// Extrai fatos de um arquivo Markdown do workspace (USER.md, SOUL.md, IDENTITY.md)
// Procura linhas no formato: - **Chave:** valor
function extractFactsFromMarkdown(content: string, sourceFile: string): SeedEntry[] {
  const entries: SeedEntry[] = [];
  const lines = content.split("\n");

  for (const line of lines) {
    const match = line.match(/^\s*-\s+\*\*(.+?):\*\*\s*(.+)$/);
    if (match) {
      const key = match[1].trim();
      const value = match[2].trim();
      if (!value || value === "_" || value.startsWith("_(")) continue;

      entries.push({
        content: `${key}: ${value}`,
        collection: key.toLowerCase().includes("prefer") ? "preference" : "fact",
        tags: { source_file: [sourceFile] },
      });
    }
  }

  return entries;
}

// Seed a partir dos arquivos do workspace (~/.openclaw/workspace/)
export async function seedFromAgents(
  pool: pg.Pool,
  embedder: EmbeddingClient | null,
  workspacePath: string
): Promise<{ total: number; inserted: number; skipped: number }> {
  const files = ["USER.md", "SOUL.md", "IDENTITY.md"];
  const allEntries: SeedEntry[] = [];

  for (const file of files) {
    const filePath = resolve(workspacePath, file);
    try {
      const content = await readFile(filePath, "utf-8");
      const facts = extractFactsFromMarkdown(content, file);
      allEntries.push(...facts);
    } catch {
      console.log(`[memory-orkmind] seed: arquivo ${file} nao encontrado, ignorando.`);
    }
  }

  if (allEntries.length === 0) {
    console.log("[memory-orkmind] seed: nenhum fato extraido dos arquivos do workspace.");
    return { total: 0, inserted: 0, skipped: 0 };
  }

  let inserted = 0;
  let skipped = 0;

  for (const entry of allEntries) {
    const result = await insertSeedEntry(pool, entry, embedder);
    if (result.inserted) {
      inserted++;
      console.log(`  + [${entry.collection}] ${entry.content}`);
    } else {
      skipped++;
      console.log(`  = [skip] ${entry.content} (${result.reason})`);
    }
  }

  return { total: allEntries.length, inserted, skipped };
}

// Seed a partir de arquivo JSON
export async function seedFromFile(
  pool: pg.Pool,
  embedder: EmbeddingClient | null,
  filePath: string
): Promise<{ total: number; inserted: number; skipped: number }> {
  const raw = await readFile(resolve(filePath), "utf-8");
  const entries = JSON.parse(raw) as SeedEntry[];

  if (!Array.isArray(entries)) {
    throw new Error("Arquivo JSON deve conter um array de entradas.");
  }

  let inserted = 0;
  let skipped = 0;

  for (const entry of entries) {
    if (!entry.content || !entry.collection) {
      console.warn(`  ! Entrada invalida ignorada: ${JSON.stringify(entry).slice(0, 100)}`);
      skipped++;
      continue;
    }

    // Nunca criar rule/instruction via seed automatico
    if (entry.collection === "rule" || entry.collection === "instruction") {
      console.warn(`  ! Colecao "${entry.collection}" bloqueada em seed: salvando como "fact".`);
      entry.collection = "fact";
    }

    const result = await insertSeedEntry(pool, entry, embedder);
    if (result.inserted) {
      inserted++;
      console.log(`  + [${entry.collection}] ${entry.content.slice(0, 80)}`);
    } else {
      skipped++;
      console.log(`  = [skip] ${entry.content.slice(0, 80)} (${result.reason})`);
    }
  }

  return { total: entries.length, inserted, skipped };
}

// Remove todas as memorias inseridas por seed (source='bootstrap')
export async function removeSeedEntries(pool: pg.Pool): Promise<number> {
  const result = await pool.query(
    "DELETE FROM memories WHERE source = 'bootstrap'"
  );
  return result.rowCount ?? 0;
}
