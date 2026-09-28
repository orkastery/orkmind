// Testes do hook before_prompt_build (A6, A7, A8).
//
// Espelham as classes de teste do plugin Hermes
// (tests/unit/test_hermes_plugin.py): a garantia constitucional e a mesma
// nos dois harnesses, entao a bateria de casos tambem precisa ser.
//
// A tabela do A6 (cinco pontos de saida silenciosa mapeados pelo F1) e
// coberta integralmente aqui e serve de gabarito para a metrica M3 do
// benchmark.

import test from "node:test";
import assert from "node:assert/strict";

import { resolveConfig, type ResolvedConfig } from "../src/config.js";
import { FALLBACK_NO_RULES, GUARDRAIL_ANTI_DESTRUICAO } from "../src/rules.js";
import plugin, { __testing } from "../src/index.js";

const DSN = "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind_openclaw_test";

const REGRAS = [
  {
    id: "r1",
    content: "Nunca deletar memoria para abrir espaco.",
    collection: "rule",
    priority: "critical",
    mandatory: true,
    tags: { domain: ["governance"] },
  },
  {
    id: "r2",
    content: "Responder sempre em portugues brasileiro.",
    collection: "rule",
    priority: "critical",
    mandatory: true,
    tags: { domain: ["comunicacao"] },
  },
];

// `score` presente porque searchByVector corta pelo minScore no cliente:
// linha sem score seria descartada, e o teste da busca vetorial nao veria nada.
const MEMORIAS = [
  { id: "m1", collection: "fact", content: "O deploy usa GitHub Actions.", essence: null, score: 0.82 },
  { id: "m2", collection: "decision", content: "Postgres escolhido em 2026.", essence: "Postgres em 2026", score: 0.61 },
];

// --- Dublês -----------------------------------------------------------------

interface OpcoesPool {
  rules?: unknown[];
  memories?: unknown[];
  schemaOk?: boolean;
  erroRegras?: Error;
  atrasoRegrasMs?: number;
  erroSchema?: Error;
}

interface Chamada {
  sql: string;
  params?: unknown[];
}

function criarPool(opts: OpcoesPool = {}) {
  const chamadas: Chamada[] = [];
  const pool = {
    async query(sql: string, params?: unknown[]) {
      chamadas.push({ sql, params });
      if (sql.includes("information_schema.tables")) {
        if (opts.erroSchema) throw opts.erroSchema;
        return { rows: [{ exists: opts.schemaOk !== false }] };
      }
      if (sql.includes("pg_type")) return { rows: [{ exists: true }] };
      if (sql.includes("mandatory = true")) {
        if (opts.atrasoRegrasMs) {
          await new Promise((r) => setTimeout(r, opts.atrasoRegrasMs));
        }
        if (opts.erroRegras) throw opts.erroRegras;
        return { rows: opts.rules ?? [] };
      }
      return { rows: opts.memories ?? [] };
    },
  };
  return { pool: pool as never, chamadas };
}

interface ApiFake {
  pluginConfig: Record<string, unknown>;
  hooks: Record<string, (...args: never[]) => unknown>;
  promptBuilder?: (params: { availableTools: Set<string> }) => string[];
  registerMemoryCapability: (cap: Record<string, unknown>) => void;
  registerTool: (...args: unknown[]) => void;
  registerCli: (...args: unknown[]) => void;
  on: (nome: string, handler: (...args: never[]) => unknown) => void;
}

function criarApi(): ApiFake {
  const api: ApiFake = {
    pluginConfig: { databaseUrl: DSN },
    hooks: {},
    registerMemoryCapability(cap) {
      api.promptBuilder = cap.promptBuilder as ApiFake["promptBuilder"];
    },
    registerTool() {},
    registerCli() {},
    on(nome, handler) {
      api.hooks[nome] = handler;
    },
  };
  return api;
}

type ResultadoHook =
  | { prependSystemContext?: string; appendContext?: string }
  | undefined;

interface Montagem {
  api: ApiFake;
  chamarHook: (
    event?: Record<string, unknown>,
    ctx?: Record<string, unknown>
  ) => Promise<ResultadoHook>;
  chamarAgentEnd: (event?: Record<string, unknown>) => Promise<void>;
  chamadas: Chamada[];
}

function montar(
  opts: OpcoesPool = {},
  overrides: Partial<Parameters<typeof resolveConfig>[0]> = {},
  embedderFake: { embed: (t: string) => Promise<number[]> } | null = null
): Montagem {
  __testing.reset();
  const api = criarApi();
  (plugin as { register: (api: unknown) => void }).register(api);

  const cfg: ResolvedConfig = resolveConfig({ databaseUrl: DSN, ...overrides });
  const { pool, chamadas } = criarPool(opts);
  __testing.setConfig(cfg);
  __testing.setPool(pool);
  __testing.setEmbedder(embedderFake as never);

  return {
    api,
    chamadas,
    chamarHook: (event = { messages: mensagens("como esta o deploy?") }, ctx) =>
      api.hooks.before_prompt_build(event as never, ctx as never) as Promise<ResultadoHook>,
    chamarAgentEnd: (event = { success: true, messages: [] }) =>
      api.hooks.agent_end(event as never) as Promise<void>,
  };
}

function mensagens(...textos: string[]): unknown[] {
  return textos.map((t) => ({ role: "user", content: t }));
}

// --- Nivel 1 e 2: injecao incondicional -------------------------------------

test("regras mandatorias aparecem no system context", async () => {
  const m = montar({ rules: REGRAS });
  const out = await m.chamarHook();
  assert.ok(out?.prependSystemContext);
  assert.match(out.prependSystemContext, /## REGRAS MANDATORIAS \(OBEDECER SEMPRE\)/);
  assert.match(out.prependSystemContext, /Nunca deletar memoria para abrir espaco\./);
  assert.match(out.prependSystemContext, /Responder sempre em portugues brasileiro\./);
});

test("injecao de regras independe do embedder", async () => {
  const semEmbedder = montar({ rules: REGRAS }, {}, null);
  const out1 = await semEmbedder.chamarHook();
  assert.match(out1!.prependSystemContext!, /REGRAS MANDATORIAS/);

  const comEmbedder = montar({ rules: REGRAS }, {}, {
    embed: async () => [0.1, 0.2, 0.3],
  });
  const out2 = await comEmbedder.chamarHook();
  assert.match(out2!.prependSystemContext!, /REGRAS MANDATORIAS/);
});

test("injecao de regras independe de autoRecall", async () => {
  const m = montar({ rules: REGRAS, memories: MEMORIAS }, { autoRecall: false });
  const out = await m.chamarHook();
  assert.match(out!.prependSystemContext!, /REGRAS MANDATORIAS/);
  // autoRecall desligado suprime apenas o nivel 3.
  assert.equal(out!.appendContext, undefined);
});

test("regras sao injetadas mesmo sem mensagem de usuario (ponto 3 do mapa)", async () => {
  const m = montar({ rules: REGRAS });
  const out = await m.chamarHook({ messages: [] });
  assert.match(out!.prependSystemContext!, /REGRAS MANDATORIAS/);
  assert.equal(out!.appendContext, undefined);
});

test("regras sao injetadas mesmo com busca de contexto vazia (ponto 4 do mapa)", async () => {
  const m = montar({ rules: REGRAS, memories: [] });
  const out = await m.chamarHook();
  assert.match(out!.prependSystemContext!, /REGRAS MANDATORIAS/);
  assert.equal(out!.appendContext, undefined);
});

test("budget do runtime aperta o bloco de regras quando a janela e pequena", async () => {
  const m = montar({ rules: REGRAS }, { rulesCacheTtlMs: 0 });
  const largo = await m.chamarHook(undefined, { contextTokenBudget: 200000 });
  const apertado = await m.chamarHook(undefined, { contextTokenBudget: 400 });
  // floor(400 * 0.15) = 60 tokens -> int(60*0.3)*4 = 72 chars: so a primeira regra cabe.
  assert.ok(apertado!.prependSystemContext!.length < largo!.prependSystemContext!.length);
  assert.match(apertado!.prependSystemContext!, /omitida\(s\) por budget/);
});

// --- D-MA9: maquina de dois estados -----------------------------------------

test("sem carga previa, falha de backend NAO alerta (estado 1 da maquina)", async () => {
  const m = montar({ erroRegras: new Error("connection refused") });
  const out = await m.chamarHook();
  // O guardrail continua saindo (e constante e nao depende do banco), mas nem
  // regras nem alerta aparecem: o banco pode estar legitimamente vazio.
  assert.equal(out!.prependSystemContext, GUARDRAIL_ANTI_DESTRUICAO);
});

test("sem carga previa, backend vazio NAO alerta (banco legitimamente vazio)", async () => {
  const m = montar({ rules: [] });
  const out = await m.chamarHook();
  assert.equal(out!.prependSystemContext, GUARDRAIL_ANTI_DESTRUICAO);
});

test("apos carga bem-sucedida, falha de backend vira ALERTA (D-MA9)", async () => {
  const opts: OpcoesPool = { rules: REGRAS };
  const m = montar(opts, { rulesCacheTtlMs: 0 });

  const primeiro = await m.chamarHook();
  assert.match(primeiro!.prependSystemContext!, /REGRAS MANDATORIAS/);
  assert.equal(__testing.state().expectsMandatoryRules, true);

  opts.erroRegras = new Error("connection refused");
  const segundo = await m.chamarHook();
  assert.equal(segundo!.prependSystemContext, GUARDRAIL_ANTI_DESTRUICAO + FALLBACK_NO_RULES);
});

test("apos carga bem-sucedida, backend vazio vira ALERTA (D-MA9)", async () => {
  const opts: OpcoesPool = { rules: REGRAS };
  const m = montar(opts, { rulesCacheTtlMs: 0 });
  await m.chamarHook();

  opts.rules = [];
  const out = await m.chamarHook();
  assert.equal(out!.prependSystemContext, GUARDRAIL_ANTI_DESTRUICAO + FALLBACK_NO_RULES);
});

test("timeout na carga de regras vira ALERTA, nunca silencio (ponto 5 do mapa)", async () => {
  const opts: OpcoesPool = { rules: REGRAS };
  const m = montar(opts, { rulesCacheTtlMs: 0, rulesTimeoutMs: 250 });
  await m.chamarHook();

  opts.atrasoRegrasMs = 600;
  const out = await m.chamarHook();
  assert.equal(out!.prependSystemContext, GUARDRAIL_ANTI_DESTRUICAO + FALLBACK_NO_RULES);
});

test("sem pool, apos carga previa, vira ALERTA (ponto 1 do mapa)", async () => {
  const m = montar({ rules: REGRAS }, { rulesCacheTtlMs: 0 });
  await m.chamarHook();

  __testing.setPool(null);
  const out = await m.chamarHook();
  assert.equal(out!.prependSystemContext, GUARDRAIL_ANTI_DESTRUICAO + FALLBACK_NO_RULES);
});

test("failSafe: false suprime o alerta (opt-out explicito do dono)", async () => {
  const opts: OpcoesPool = { rules: REGRAS };
  const m = montar(opts, { rulesCacheTtlMs: 0, failSafe: false });
  await m.chamarHook();

  opts.erroRegras = new Error("caiu");
  const out = await m.chamarHook();
  assert.equal(out!.prependSystemContext, GUARDRAIL_ANTI_DESTRUICAO);
});

test("nunca devolve regras e alerta ao mesmo tempo", async () => {
  const opts: OpcoesPool = { rules: REGRAS };
  const m = montar(opts, { rulesCacheTtlMs: 0 });
  const comRegras = await m.chamarHook();
  assert.doesNotMatch(comRegras!.prependSystemContext!, /ALERTA: Regras de Governanca/);

  opts.erroRegras = new Error("caiu");
  const comAlerta = await m.chamarHook();
  assert.doesNotMatch(comAlerta!.prependSystemContext!, /REGRAS MANDATORIAS/);
});

// --- Schema (ponto 2 do mapa) -----------------------------------------------

test("schema invalido apos carga previa vira ALERTA e revalida no turno seguinte", async () => {
  const opts: OpcoesPool = { rules: REGRAS };
  const m = montar(opts, { rulesCacheTtlMs: 0 });
  await m.chamarHook();
  assert.equal(__testing.state().schemaValid, true);

  // Simula perda do schema: forca revalidacao e faz a validacao falhar.
  __testing.setSchemaValid(null);
  opts.schemaOk = false;
  const out = await m.chamarHook();
  assert.equal(out!.prependSystemContext, GUARDRAIL_ANTI_DESTRUICAO + FALLBACK_NO_RULES);
  assert.equal(__testing.state().schemaValid, false);

  // Bug corrigido: com schema invalido o plugin REVALIDA em vez de
  // prosseguir como se estivesse validado.
  opts.schemaOk = true;
  const recuperado = await m.chamarHook();
  assert.match(recuperado!.prependSystemContext!, /REGRAS MANDATORIAS/);
  assert.equal(__testing.state().schemaValid, true);
});

test("excecao na validacao de schema e tratada como falha de carga", async () => {
  const opts: OpcoesPool = { rules: REGRAS };
  const m = montar(opts, { rulesCacheTtlMs: 0 });
  await m.chamarHook();

  __testing.setSchemaValid(null);
  opts.erroSchema = new Error("permission denied");
  const out = await m.chamarHook();
  assert.equal(out!.prependSystemContext, GUARDRAIL_ANTI_DESTRUICAO + FALLBACK_NO_RULES);
});

// --- Cache por TTL (D3) -----------------------------------------------------

test("dentro do TTL nao ha nova consulta de regras ao banco", async () => {
  const m = montar({ rules: REGRAS }, { rulesCacheTtlMs: 60000 });
  await m.chamarHook();
  await m.chamarHook();
  await m.chamarHook();
  const consultas = m.chamadas.filter((c) => c.sql.includes("mandatory = true"));
  assert.equal(consultas.length, 1);
});

test("com TTL zerado, toda chamada consulta o banco", async () => {
  const m = montar({ rules: REGRAS }, { rulesCacheTtlMs: 0 });
  await m.chamarHook();
  await m.chamarHook();
  const consultas = m.chamadas.filter((c) => c.sql.includes("mandatory = true"));
  assert.equal(consultas.length, 2);
});

test("falha de carga nunca serve cache velho em silencio", async () => {
  const opts: OpcoesPool = { rules: REGRAS };
  const m = montar(opts, { rulesCacheTtlMs: 0 });
  await m.chamarHook();
  assert.ok(__testing.state().cachedRules);

  opts.erroRegras = new Error("caiu");
  const out = await m.chamarHook();
  assert.equal(out!.prependSystemContext, GUARDRAIL_ANTI_DESTRUICAO + FALLBACK_NO_RULES);
  assert.equal(__testing.state().cachedRules, null);
});

// --- Nivel 3: contexto recuperado -------------------------------------------

test("nivel 3 cai para full-text search quando nao ha embedder", async () => {
  const m = montar({ rules: REGRAS, memories: MEMORIAS }, {}, null);
  const out = await m.chamarHook();
  assert.match(out!.appendContext!, /<relevant-memories source="orkmind">/);
  assert.match(out!.appendContext!, /O deploy usa GitHub Actions\./);
  const fts = m.chamadas.filter((c) => c.sql.includes("plainto_tsquery"));
  assert.equal(fts.length, 1);
});

test("nivel 3 usa busca vetorial quando ha embedder", async () => {
  const m = montar({ rules: REGRAS, memories: MEMORIAS }, {}, {
    embed: async () => [0.1, 0.2],
  });
  const out = await m.chamarHook();
  assert.match(out!.appendContext!, /<relevant-memories source="orkmind">/);
  const vetorial = m.chamadas.filter((c) => c.sql.includes("<=> $1::vector"));
  assert.equal(vetorial.length, 1);
});

test("falha do embedder degrada para full-text sem derrubar o turno", async () => {
  const m = montar({ rules: REGRAS, memories: MEMORIAS }, {}, {
    embed: async () => {
      throw new Error("openrouter fora do ar");
    },
  });
  const out = await m.chamarHook();
  assert.match(out!.prependSystemContext!, /REGRAS MANDATORIAS/);
  assert.match(out!.appendContext!, /<relevant-memories/);
  assert.equal(m.chamadas.filter((c) => c.sql.includes("plainto_tsquery")).length, 1);
});

test("nivel 3 exclui mandatorias do contexto recuperado", async () => {
  const m = montar({ rules: REGRAS, memories: MEMORIAS });
  await m.chamarHook();
  const busca = m.chamadas.find((c) => c.sql.includes("plainto_tsquery"));
  assert.match(busca!.sql.replace(/\s+/g, " "), /AND mandatory = false/);
});

test("query do nivel 3 usa a janela de mensagens e o escopo da sessao", async () => {
  const m = montar({ rules: REGRAS, memories: MEMORIAS }, { recallWindowMessages: 3 });
  await m.chamarHook(
    { messages: mensagens("turno um", "turno dois", "turno tres", "turno quatro") },
    { workspaceDir: "/srv/OrkMind", agentId: "hermes" }
  );
  const busca = m.chamadas.find((c) => c.sql.includes("plainto_tsquery"));
  const query = String((busca!.params as unknown[])[0]);
  // Janela de 3: o turno mais antigo fica de fora, os 3 ultimos entram.
  assert.doesNotMatch(query, /turno um/);
  assert.match(query, /turno dois/);
  assert.match(query, /turno quatro/);
  assert.match(query, /\[escopo: projeto=OrkMind agente=hermes\]/);
});

test("query do nivel 3 sem ctx nao quebra e nao inventa escopo", async () => {
  const m = montar({ rules: REGRAS, memories: MEMORIAS });
  await m.chamarHook({ messages: mensagens("so uma frase") }, undefined);
  const busca = m.chamadas.find((c) => c.sql.includes("plainto_tsquery"));
  const query = String((busca!.params as unknown[])[0]);
  assert.equal(query, "so uma frase");
});

test("erro no nivel 3 nao impede a injecao das regras", async () => {
  const m = montar({ rules: REGRAS });
  __testing.setPool({
    async query(sql: string) {
      if (sql.includes("information_schema.tables")) return { rows: [{ exists: true }] };
      if (sql.includes("pg_type")) return { rows: [{ exists: true }] };
      if (sql.includes("mandatory = true")) return { rows: REGRAS };
      throw new Error("falha na busca de contexto");
    },
  } as never);
  const out = await m.chamarHook();
  assert.match(out!.prependSystemContext!, /REGRAS MANDATORIAS/);
  assert.equal(out!.appendContext, undefined);
});

// --- A7: guardrail no promptBuilder -----------------------------------------

test("guardrail esta presente mesmo sem nenhuma tool de memoria", () => {
  const m = montar({ rules: REGRAS });
  const secoes = m.api.promptBuilder!({ availableTools: new Set<string>() });
  const texto = secoes.join("\n");
  assert.ok(texto.includes(GUARDRAIL_ANTI_DESTRUICAO));
  assert.match(texto, /## Memoria \(OrkMind\)/);
  // Sem tools, as instrucoes de uso das tools nao aparecem.
  assert.doesNotMatch(texto, /Use memory_store para salvar/);
});

test("guardrail esta presente em qualquer combinacao de tools", () => {
  const m = montar({ rules: REGRAS });
  const combinacoes = [
    new Set<string>(),
    new Set(["memory_search"]),
    new Set(["memory_get"]),
    new Set(["memory_recall"]),
    new Set(["memory_search", "memory_get", "memory_recall"]),
  ];
  for (const availableTools of combinacoes) {
    const texto = m.api.promptBuilder!({ availableTools }).join("\n");
    assert.ok(
      texto.includes(GUARDRAIL_ANTI_DESTRUICAO),
      `guardrail ausente para ${[...availableTools].join(",") || "(nenhuma tool)"}`
    );
  }
});

test("instrucoes de uso das tools voltam quando ha tools", () => {
  const m = montar({ rules: REGRAS });
  const texto = m.api.promptBuilder!({
    availableTools: new Set(["memory_search"]),
  }).join("\n");
  assert.match(texto, /Use memory_store para salvar/);
  assert.match(texto, /PROIBIDO: NAO use arquivos MEMORY\.md/);
});

// --- A8: heuristica de allowPromptInjection ---------------------------------

function capturarConsoleError(): { linhas: string[]; restaurar: () => void } {
  const original = console.error;
  const linhas: string[] = [];
  console.error = (...args: unknown[]) => {
    linhas.push(args.map(String).join(" "));
  };
  return { linhas, restaurar: () => { console.error = original; } };
}

test("dois agent_end sem o hook disparar emitem alerta uma unica vez", async () => {
  const m = montar({ rules: REGRAS });
  const captura = capturarConsoleError();
  try {
    await m.chamarAgentEnd();
    assert.equal(captura.linhas.length, 0, "um turno so nao e evidencia suficiente");
    await m.chamarAgentEnd();
    await m.chamarAgentEnd();
    await m.chamarAgentEnd();
  } finally {
    captura.restaurar();
  }
  const alertas = captura.linhas.filter((l) => l.includes("allowPromptInjection"));
  assert.equal(alertas.length, 1);
  assert.match(alertas[0], /garantia constitucional do OrkMind esta DESLIGADA/);
});

test("nenhum falso positivo quando o hook dispara normalmente", async () => {
  const m = montar({ rules: REGRAS });
  await m.chamarHook();
  const captura = capturarConsoleError();
  try {
    await m.chamarAgentEnd();
    await m.chamarAgentEnd();
    await m.chamarAgentEnd();
  } finally {
    captura.restaurar();
  }
  assert.equal(
    captura.linhas.filter((l) => l.includes("allowPromptInjection")).length,
    0
  );
});

// --- D1 (contingencia medida no A11): guardrail tambem pelo hook -----------
//
// A verificacao em sessao viva de 31/08/2026 mostrou que o promptBuilder da
// capability NAO roda em subagentes, enquanto este hook roda. Sem o guardrail
// no hook, todo subagente ficaria sem a protecao anti-destruicao.

test("guardrail sai pelo hook em toda sessao, inclusive sem regras no banco", async () => {
  const m = montar({ rules: [] });
  const out = await m.chamarHook();
  assert.ok(out!.prependSystemContext!.includes(GUARDRAIL_ANTI_DESTRUICAO));
});

test("guardrail acompanha o bloco de regras quando ha regras", async () => {
  const m = montar({ rules: REGRAS });
  const out = await m.chamarHook();
  assert.ok(out!.prependSystemContext!.startsWith(GUARDRAIL_ANTI_DESTRUICAO));
  assert.match(out!.prependSystemContext!, /REGRAS MANDATORIAS/);
});

test("guardrail acompanha o alerta de fail-safe", async () => {
  const opts: OpcoesPool = { rules: REGRAS };
  const m = montar(opts, { rulesCacheTtlMs: 0 });
  await m.chamarHook();
  opts.erroRegras = new Error("caiu");
  const out = await m.chamarHook();
  assert.ok(out!.prependSystemContext!.includes(GUARDRAIL_ANTI_DESTRUICAO));
  assert.ok(out!.prependSystemContext!.includes(FALLBACK_NO_RULES));
});
