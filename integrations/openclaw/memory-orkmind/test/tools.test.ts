// Testes das tools na assinatura real do OpenClaw.
//
// O runtime (>= 2026.7.1) chama `execute(toolCallId, params, signal, onUpdate)`
// e le o resultado em `content`. As tools recebiam `execute(params)`: o id da
// chamada caia em `params` e `params.query.slice` quebrava na primeira busca.
// Aqui as tools sao obtidas pelo mesmo caminho do runtime (as fabricas que o
// plugin entrega a `api.registerTool`) e chamadas com os quatro argumentos.

import test from "node:test";
import assert from "node:assert/strict";

import { resolveConfig } from "../src/config.js";
import plugin, { __testing } from "../src/index.js";

const DSN = "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind_openclaw_test";

const MEMORIA = {
  id: "m1",
  content: "O deploy usa GitHub Actions.",
  collection: "fact",
  priority: "medium",
  mandatory: false,
  source: "agent",
  tags: {},
  essence: null,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
  score: 0.82,
};

interface Chamada {
  sql: string;
  params?: unknown[];
}

// Pool fake: responde pelo formato do SQL, sem banco.
function criarPool() {
  const chamadas: Chamada[] = [];
  const pool = {
    async query(sql: string, params?: unknown[]) {
      chamadas.push({ sql, params });
      if (sql.includes("DELETE FROM memories")) return { rows: [], rowCount: 1 };
      if (sql.includes("INSERT INTO memories")) return { rows: [], rowCount: 1 };
      return { rows: [MEMORIA], rowCount: 1 };
    },
  };
  return { pool: pool as never, chamadas };
}

interface ToolRegistrada {
  name: string;
  execute: (
    toolCallId: string,
    params: Record<string, unknown>,
    signal?: AbortSignal,
    onUpdate?: (partial: unknown) => void
  ) => Promise<{ content: Array<{ type: string; text: string }>; details: unknown }>;
}

function montar() {
  __testing.reset();
  const fabricas = new Map<string, () => ToolRegistrada>();
  const api = {
    pluginConfig: { databaseUrl: DSN },
    registerMemoryCapability() {},
    registerCli() {},
    on() {},
    registerTool(fabrica: () => ToolRegistrada, opts?: { names?: string[] }) {
      for (const nome of opts?.names ?? []) fabricas.set(nome, fabrica);
    },
  };
  (plugin as { register: (api: unknown) => void }).register(api);
  const { pool, chamadas } = criarPool();
  __testing.setConfig(resolveConfig({ databaseUrl: DSN }));
  __testing.setPool(pool);
  __testing.setEmbedder(null);

  // Como o runtime faz: pede a tool a fabrica e chama com o id da chamada primeiro.
  const chamar = (nome: string, params: Record<string, unknown>) => {
    const tool = fabricas.get(nome)!();
    assert.equal(tool.name, nome);
    return tool.execute(`call-${nome}`, params, new AbortController().signal, () => {});
  };
  return { fabricas, chamadas, chamar };
}

function texto(resultado: { content: Array<{ type: string; text: string }> }): string {
  assert.ok(Array.isArray(resultado.content), "resultado sem content");
  assert.equal(resultado.content.length, 1);
  assert.equal(resultado.content[0].type, "text");
  return resultado.content[0].text;
}

test("plugin registra as cinco tools de memoria", () => {
  const { fabricas } = montar();
  assert.deepEqual(
    [...fabricas.keys()].sort(),
    ["memory_forget", "memory_get", "memory_recall", "memory_search", "memory_store"]
  );
});

test("memory_search recebe o id da chamada antes dos parametros", async () => {
  const { chamar, chamadas } = montar();
  const r = await chamar("memory_search", { query: "como e o deploy?" });
  assert.match(texto(r), /orkmind:\/\/m1/);
  const busca = chamadas.find((c) => c.sql.includes("plainto_tsquery"));
  assert.equal((busca!.params as unknown[])[0], "como e o deploy?");
  const details = r.details as Array<{ path: string; source: string }>;
  assert.equal(details[0].path, "orkmind://m1");
  assert.equal(details[0].source, "memory");
});

test("memory_get le pelo caminho e devolve o conteudo em content", async () => {
  const { chamar, chamadas } = montar();
  const r = await chamar("memory_get", { path: "orkmind://m1" });
  assert.match(texto(r), /O deploy usa GitHub Actions\./);
  assert.deepEqual(r.details, { path: "orkmind://m1", truncated: false });
  const leitura = chamadas.find((c) => c.sql.includes("WHERE id = $1"));
  assert.deepEqual(leitura!.params, ["m1"]);
});

test("memory_recall busca pelo texto recebido em params", async () => {
  const { chamar, chamadas } = montar();
  const r = await chamar("memory_recall", { query: "deploy", limit: 2 });
  assert.match(texto(r), /Memorias encontradas \(1\)/);
  const busca = chamadas.find((c) => c.sql.includes("plainto_tsquery"));
  assert.deepEqual(busca!.params, ["deploy", 2]);
});

test("memory_store grava o texto recebido em params", async () => {
  const { chamar, chamadas } = montar();
  const r = await chamar("memory_store", { text: "Prefere respostas curtas.", category: "preference" });
  assert.match(texto(r), /Memoria armazenada com sucesso/);
  const insercao = chamadas.find((c) => c.sql.includes("INSERT INTO memories"));
  const params = insercao!.params as unknown[];
  assert.equal(params[1], "Prefere respostas curtas.");
  assert.equal(params[2], "preference");
});

test("memory_forget remove pelo memoryId recebido em params", async () => {
  const { chamar, chamadas } = montar();
  const r = await chamar("memory_forget", { memoryId: "m1" });
  assert.match(texto(r), /Memoria m1 removida/);
  const remocao = chamadas.find((c) => c.sql.includes("DELETE FROM memories"));
  assert.deepEqual(remocao!.params, ["m1"]);
});

test("respostas sem resultado tambem saem em content", async () => {
  const { chamar } = montar();
  assert.match(texto(await chamar("memory_store", { text: "   " })), /Erro: texto vazio/);
  assert.match(texto(await chamar("memory_forget", {})), /Informe memoryId ou query/);
});
