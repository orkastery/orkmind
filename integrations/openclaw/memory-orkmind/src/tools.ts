// Ferramentas de memoria do plugin memory-orkmind
// memory_recall, memory_store, memory_forget

import { randomUUID } from "node:crypto";
import type pg from "pg";
import type { EmbeddingClient } from "./embeddings.js";
import type { ResolvedConfig } from "./config.js";
import {
  searchByVector,
  searchByText,
  insertMemory,
  deleteMemoryById,
  deleteByQuery,
  getMemoryById,
  type MemoryRow,
} from "./database.js";

// Resultado no formato que o OpenClaw espera de uma tool (AgentToolResult):
// `content` e o que o modelo le; `details` fica para log e interface. Espelha
// o `textResult` do SDK. Devolver `{ type, text }` solto deixava o modelo sem
// conteudo nenhum.
export interface ToolResult {
  content: Array<{ type: "text"; text: string }>;
  details: unknown;
}

function textResult(text: string, details?: unknown): ToolResult {
  return { content: [{ type: "text", text }], details };
}

// O OpenClaw (>= 2026.7.1) chama `execute(toolCallId, params, signal, onUpdate)`.
// Com `execute(params)` o primeiro argumento era o id da chamada, uma string,
// e `params.query.slice` quebrava na primeira busca.

// Formata resultado de busca para exibicao
function formatResult(row: MemoryRow): string {
  const score = row.score != null ? ` (score: ${row.score.toFixed(3)})` : "";
  const display = row.essence || row.content;
  return `[${row.id}] [${row.collection}/${row.priority}]${score} ${display}`;
}

// Colecoes validas do OrkMind
const VALID_COLLECTIONS = new Set([
  "rule", "instruction", "fact", "learning", "preference", "decision",
  "content", "agenda", "contacts", "handoff", "roadmap", "files",
  "docs", "dags", "tools", "users", "session", "artifact",
  "compliance", "semantic_log",
]);

// Categorias mapeadas para o padrao memory-lancedb
const CATEGORY_MAP: Record<string, string> = {
  fact: "fact",
  preference: "preference",
  decision: "decision",
  entity: "contacts",
  learning: "learning",
  rule: "rule",
  instruction: "instruction",
};

// Detecta tentativas de prompt injection
function looksLikeInjection(text: string): boolean {
  const lower = text.toLowerCase();
  const patterns = [
    "ignore previous",
    "ignore all previous",
    "disregard",
    "you are now",
    "new instructions",
    "system prompt",
    "override",
    "<system>",
    "</system>",
    "\\[system\\]",
  ];
  return patterns.some((p) => lower.includes(p));
}

// Detecta conteudo duplicado por similaridade alta
async function isNearDuplicate(
  pool: pg.Pool,
  embedding: number[],
  threshold = 0.95
): Promise<boolean> {
  const results = await searchByVector(pool, embedding, 1, threshold);
  return results.length > 0;
}

export function createMemoryRecallTool(
  pool: pg.Pool,
  embedder: EmbeddingClient | null,
  config: ResolvedConfig
) {
  return {
    name: "memory_recall",
    description:
      "Busca na memoria de longo prazo por fatos, preferencias, decisoes e regras " +
      "relevantes a uma consulta. Retorna as memorias mais similares via busca vetorial.",
    parameters: {
      type: "object" as const,
      properties: {
        query: {
          type: "string",
          description: "Texto de busca para encontrar memorias relevantes",
        },
        limit: {
          type: "integer",
          description: "Numero maximo de resultados (padrao: 5)",
          minimum: 1,
          maximum: 20,
        },
      },
      required: ["query"],
    },
    execute: async (_toolCallId: string, params: { query: string; limit?: number }) => {
      const query = params.query.slice(0, config.recallMaxChars);
      const limit = params.limit ?? config.recallMaxResults;

      let results: MemoryRow[];

      if (embedder) {
        try {
          const embedding = await embedder.embed(query);
          results = await searchByVector(pool, embedding, limit);
        } catch {
          // Fallback para busca textual se embedding falhar
          results = await searchByText(pool, query, limit);
        }
      } else {
        results = await searchByText(pool, query, limit);
      }

      if (results.length === 0) {
        return textResult("Nenhuma memoria encontrada para esta consulta.");
      }

      const formatted = results.map(formatResult).join("\n");
      return textResult(
        `Memorias encontradas (${results.length}):\n${formatted}`,
        results.map((r) => ({
          id: r.id,
          content: r.content,
          collection: r.collection,
          score: r.score,
          priority: r.priority,
        }))
      );
    },
  };
}

export function createMemoryStoreTool(
  pool: pg.Pool,
  embedder: EmbeddingClient | null,
  _config: ResolvedConfig
) {
  return {
    name: "memory_store",
    description:
      "Armazena um fato, preferencia, decisao ou informacao importante na " +
      "memoria de longo prazo. A memoria ficara disponivel em conversas futuras.",
    parameters: {
      type: "object" as const,
      properties: {
        text: {
          type: "string",
          description: "O conteudo a memorizar",
        },
        category: {
          type: "string",
          enum: ["fact", "preference", "decision", "entity", "learning"],
          description: "Categoria da memoria: fact, preference, decision, entity, learning",
        },
      },
      required: ["text"],
    },
    execute: async (_toolCallId: string, params: { text: string; category?: string }) => {
      const text = params.text.trim();
      if (!text) {
        return textResult("Erro: texto vazio.");
      }

      if (looksLikeInjection(text)) {
        return textResult("Rejeitado: o texto parece conter uma tentativa de prompt injection.");
      }

      const category = params.category ?? "fact";
      const collection = CATEGORY_MAP[category] ?? "fact";

      let embedding: number[] | null = null;
      if (embedder) {
        try {
          embedding = await embedder.embed(text);
          // Verifica duplicata
          if (await isNearDuplicate(pool, embedding)) {
            return textResult("Memoria similar ja existe. Nenhuma nova entrada criada.");
          }
        } catch {
          // Continua sem embedding
        }
      }

      const id = randomUUID();
      await insertMemory(pool, {
        id,
        content: text,
        collection,
        tags: {},
        priority: "medium",
        source: "agent",
        embedding,
      });

      return textResult(`Memoria armazenada com sucesso. ID: ${id} [${collection}]`, {
        id,
        collection,
        text,
      });
    },
  };
}

export function createMemoryForgetTool(
  pool: pg.Pool,
  embedder: EmbeddingClient | null,
  config: ResolvedConfig
) {
  return {
    name: "memory_forget",
    description:
      "Remove uma memoria por ID ou por busca textual. Se buscar por texto, " +
      "remove automaticamente se houver uma correspondencia acima de 90% de " +
      "similaridade. Caso contrario, lista candidatos para o usuario escolher.",
    parameters: {
      type: "object" as const,
      properties: {
        memoryId: {
          type: "string",
          description: "ID exato da memoria a remover",
        },
        query: {
          type: "string",
          description: "Texto para buscar e remover a memoria mais similar",
        },
      },
    },
    execute: async (_toolCallId: string, params: { memoryId?: string; query?: string }) => {
      if (params.memoryId) {
        const ok = await deleteMemoryById(pool, params.memoryId);
        if (ok) {
          return textResult(`Memoria ${params.memoryId} removida com sucesso.`);
        }
        return textResult(`Memoria ${params.memoryId} nao encontrada ou protegida contra remocao.`);
      }

      if (params.query) {
        const query = params.query.slice(0, config.recallMaxChars);

        if (embedder) {
          try {
            const embedding = await embedder.embed(query);
            const { deleted, candidates } = await deleteByQuery(
              pool,
              embedding,
              5
            );
            if (deleted && candidates.length > 0) {
              return textResult(`Memoria removida: [${candidates[0].id}] ${candidates[0].content.slice(0, 100)}`);
            }
            if (candidates.length > 0) {
              const list = candidates.map(formatResult).join("\n");
              return textResult(
                `Nenhuma correspondencia acima de 90%. Candidatos:\n${list}\nUse memoryId para remover uma especifica.`,
                candidates.map((c) => ({ id: c.id, score: c.score }))
              );
            }
          } catch {
            // Fallback sem embedding
          }
        }

        return textResult("Nenhuma memoria encontrada para esta busca.");
      }

      return textResult("Informe memoryId ou query para localizar a memoria a remover.");
    },
  };
}

// --- Tools do contrato OpenClaw (memory_search / memory_get) ---

// Extrai ID de um path no formato orkmind://<id> ou retorna o valor bruto
function extractIdFromPath(path: string): string {
  if (path.startsWith("orkmind://")) {
    return path.slice("orkmind://".length);
  }
  return path;
}

export function createMemorySearchTool(
  pool: pg.Pool,
  embedder: EmbeddingClient | null,
  config: ResolvedConfig
) {
  return {
    name: "memory_search",
    label: "Memory Search",
    description:
      "Busca semantica obrigatoria na memoria OrkMind. Execute ANTES de responder " +
      "sobre trabalho anterior, decisoes, datas, pessoas, preferencias ou tarefas. " +
      "Retorna trechos relevantes ranqueados por similaridade vetorial.",
    parameters: {
      type: "object" as const,
      properties: {
        query: {
          type: "string",
          description: "Texto de busca semantica",
        },
        maxResults: {
          type: "integer",
          minimum: 1,
          description: "Numero maximo de resultados (padrao: 5)",
        },
        minScore: {
          type: "number",
          description: "Score minimo de similaridade (padrao: 0.3)",
        },
      },
      required: ["query"],
      additionalProperties: false,
    },
    execute: async (_toolCallId: string, params: {
      query: string;
      maxResults?: number;
      minScore?: number;
    }) => {
      const query = params.query.slice(0, config.recallMaxChars);
      const limit = params.maxResults ?? config.recallMaxResults;
      const minScore = params.minScore ?? 0.3;

      let results: MemoryRow[];

      if (embedder) {
        try {
          const embedding = await embedder.embed(query);
          results = await searchByVector(pool, embedding, limit, minScore);
        } catch {
          results = await searchByText(pool, query, limit);
        }
      } else {
        results = await searchByText(pool, query, limit);
      }

      if (results.length === 0) {
        return textResult("Nenhuma memoria encontrada para esta consulta.");
      }

      const formatted = results
        .map((r) => {
          const score = r.score != null ? ` (score: ${r.score.toFixed(3)})` : "";
          const path = `orkmind://${r.id}`;
          const snippet = r.essence || r.content.slice(0, 200);
          return `- ${path}${score} [${r.collection}] ${snippet}`;
        })
        .join("\n");

      return textResult(
        `Memorias encontradas (${results.length}):\n${formatted}`,
        results.map((r) => ({
          path: `orkmind://${r.id}`,
          startLine: 0,
          endLine: 0,
          score: r.score ?? 0,
          snippet: r.essence || r.content.slice(0, 200),
          source: "memory" as const,
        }))
      );
    },
  };
}

export function createMemoryGetTool(
  pool: pg.Pool,
  _embedder: EmbeddingClient | null,
  _config: ResolvedConfig
) {
  return {
    name: "memory_get",
    label: "Memory Get",
    description:
      "Le o conteudo completo de uma memoria especifica do OrkMind pelo caminho " +
      "(formato orkmind://<id>). Use apos memory_search para obter detalhes " +
      "de uma entrada encontrada.",
    parameters: {
      type: "object" as const,
      properties: {
        path: {
          type: "string",
          description: "Caminho da memoria (ex: orkmind://<uuid>)",
        },
      },
      required: ["path"],
      additionalProperties: false,
    },
    execute: async (_toolCallId: string, params: { path: string }) => {
      const id = extractIdFromPath(params.path);

      const memory = await getMemoryById(pool, id);

      if (!memory) {
        return textResult(`Memoria nao encontrada: ${params.path}`);
      }

      const lines = [
        `ID: ${memory.id}`,
        `Colecao: ${memory.collection}`,
        `Prioridade: ${memory.priority}`,
        `Fonte: ${memory.source}`,
        `Criado em: ${memory.created_at}`,
        `Atualizado em: ${memory.updated_at}`,
        memory.essence ? `Essencia: ${memory.essence}` : null,
        ``,
        memory.content,
      ].filter(Boolean);

      return textResult(lines.join("\n"), {
        path: `orkmind://${memory.id}`,
        truncated: false,
      });
    },
  };
}
