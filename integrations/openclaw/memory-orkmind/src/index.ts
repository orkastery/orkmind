// Plugin de memoria OrkMind para OpenClaw
// Ocupa o slot oficial de memoria (plugins.slots.memory)
// Usa PostgreSQL + pgvector como backend de busca vetorial semantica

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import type {
  PluginHookAgentContext,
  PluginHookAgentEndEvent,
  PluginHookBeforePromptBuildEvent,
  PluginHookBeforePromptBuildResult,
} from "openclaw/plugin-sdk/plugin-entry";
import { resolveConfig, type OrkMindPluginConfig, type ResolvedConfig } from "./config.js";
import { createEmbeddingClient, type EmbeddingClient } from "./embeddings.js";
import {
  getPool,
  closePool,
  searchByVector,
  searchByText,
  getMandatoryRules,
  countMemories,
  listMemories,
  validateSchema,
  type MemoryRow,
} from "./database.js";
import {
  FALLBACK_NO_RULES,
  GUARDRAIL_ANTI_DESTRUICAO,
  effectiveTokenBudget,
  formatRulesBlock,
  mandatoryBudgetChars,
} from "./rules.js";
import {
  createMemoryRecallTool,
  createMemoryStoreTool,
  createMemoryForgetTool,
  createMemorySearchTool,
  createMemoryGetTool,
} from "./tools.js";
import type pg from "pg";

// Estado global do plugin (inicializado no register, compartilhado entre hooks e tools)
let dbPool: pg.Pool | null = null;
let embedder: EmbeddingClient | null = null;
let pluginConfig: ResolvedConfig | null = null;

// Resultado da validacao de schema. `null` = ainda nao validado.
// Guardamos o RESULTADO, nao apenas "ja tentou": a versao anterior marcava
// `schemaValidated = true` mesmo com schema invalido e o turno seguinte
// prosseguia sem validacao nenhuma.
let schemaValid: boolean | null = null;

// --- Estado da governanca de memoria (N1/N2) ---

// D-MA9, maquina de dois estados: comeca `false` e so vira `true` apos a
// PRIMEIRA carga bem-sucedida com regras nao vazias. A partir dai, ausencia
// de regras deixa de ser "banco vazio" e passa a ser falha que merece alerta.
// Banco legitimamente vazio que nunca carregou nada segue em silencio.
let expectsMandatoryRules = false;

// Cache de regras por TTL (D3). Evita ida ao banco a cada turno.
let cachedRules: MemoryRow[] | null = null;
let cachedRulesAt = 0;

// D10: heuristica de deteccao de `hooks.allowPromptInjection: false`.
let hookFireCount = 0;
let agentEndCount = 0;
let injectionWarningEmitted = false;

// Score minimo do contexto recuperado (nivel 3), igual ao das tools.
const RECALL_MIN_SCORE = 0.3;

// Timeout do nivel 3. Degrada em SILENCIO por definicao do nivel (D4);
// os niveis 1 e 2 tem timeout proprio que termina em ALERTA.
const RECALL_TIMEOUT_MS = 5000;

/** Zera todo o estado do modulo (parada do gateway e testes). */
function resetPluginState(): void {
  dbPool = null;
  embedder = null;
  pluginConfig = null;
  schemaValid = null;
  expectsMandatoryRules = false;
  cachedRules = null;
  cachedRulesAt = 0;
  hookFireCount = 0;
  agentEndCount = 0;
  injectionWarningEmitted = false;
}

// Inicializa conexoes sob demanda
function ensureInitialized(rawConfig: OrkMindPluginConfig) {
  if (pluginConfig) return;
  pluginConfig = resolveConfig(rawConfig);
  dbPool = getPool(pluginConfig.databaseUrl);
  if (pluginConfig.embedding.apiKey) {
    embedder = createEmbeddingClient(pluginConfig.embedding);
  }
}

// Extrai texto da ultima mensagem do usuario a partir do array de mensagens
function extractLastUserMessage(messages: unknown[]): string | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i] as Record<string, unknown> | undefined;
    if (msg?.role === "user") {
      if (typeof msg.content === "string") return msg.content;
      if (Array.isArray(msg.content)) {
        const textParts = (msg.content as Array<Record<string, unknown>>)
          .filter((p) => p.type === "text" && typeof p.text === "string")
          .map((p) => p.text as string);
        if (textParts.length > 0) return textParts.join("\n");
      }
    }
  }
  return null;
}

// Extrai as ultimas `limit` mensagens de usuario, em ordem cronologica.
// Ancora a query do nivel 3 no escopo da sessao e nao apenas na ultima
// frase (D7). Le direto de `event.messages`, que o runtime ja entrega
// completo: nao ha estado por sessao para ficar velho ou vazar entre elas.
function extractLastUserMessages(messages: unknown[], limit: number): string[] {
  const found: string[] = [];
  for (let i = messages.length - 1; i >= 0 && found.length < limit; i--) {
    const texto = extractUserText(messages[i]);
    if (texto) found.push(texto);
  }
  return found.reverse();
}

// Texto de uma unica mensagem, se ela for do usuario.
function extractUserText(message: unknown): string | null {
  const msg = message as Record<string, unknown> | undefined;
  if (msg?.role !== "user") return null;
  if (typeof msg.content === "string") return msg.content;
  if (Array.isArray(msg.content)) {
    const textParts = (msg.content as Array<Record<string, unknown>>)
      .filter((p) => p.type === "text" && typeof p.text === "string")
      .map((p) => p.text as string);
    if (textParts.length > 0) return textParts.join("\n");
  }
  return null;
}

// Verifica se o texto contem frases de captura
function matchesCaptureTrigger(text: string, triggers: string[]): boolean {
  const lower = text.toLowerCase();
  return triggers.some((t) => lower.includes(t.toLowerCase()));
}

// Corrida entre uma promessa e um timeout, sem vazar o timer.
function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const limite = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(label)), ms);
  });
  return Promise.race([
    promise.finally(() => {
      if (timer !== undefined) clearTimeout(timer);
    }),
    limite,
  ]);
}

/**
 * D10: denuncia `hooks.allowPromptInjection: false` no gateway.
 *
 * O plugin nao enxerga a config de hooks do gateway, entao nao ha como
 * consultar a flag diretamente. A heuristica e barata: se o agente ja
 * terminou 2 ou mais turnos e o `before_prompt_build` NUNCA disparou, a
 * injecao de prompt esta desligada e a garantia constitucional deste plugin
 * esta inativa. Silenciar isso seria pior do que o proprio bug.
 *
 * Emite uma unica vez por processo para nao poluir o log.
 */
function maybeWarnPromptInjectionDisabled(): void {
  if (injectionWarningEmitted) return;
  if (agentEndCount < 2 || hookFireCount > 0) return;
  injectionWarningEmitted = true;
  console.error(
    "[memory-orkmind] ALERTA: o hook before_prompt_build nao disparou em " +
      `${agentEndCount} turnos concluidos. As regras mandatorias NAO estao ` +
      "sendo injetadas e a garantia constitucional do OrkMind esta DESLIGADA. " +
      "Causa provavel: hooks.allowPromptInjection: false na config do gateway."
  );
}

// --- Nivel 1 e 2: regras mandatorias no system prompt (D-MA1) --------------

/**
 * Decide o que fazer quando as regras mandatorias nao puderam ser carregadas.
 *
 * D-MA9: se este processo JA carregou regras alguma vez e o fail-safe esta
 * ligado, devolve o alerta explicito. Sem carga previa, o banco pode estar
 * legitimamente vazio e o silencio e o comportamento correto (estado 1 da
 * maquina de dois estados).
 *
 * Nunca serve cache velho: a staleness maxima e o TTL, e falha de consulta
 * vira alerta, jamais regra desatualizada passada como atual (D3).
 */
function onRulesLoadFailure(motivo: string): string {
  cachedRules = null;
  if (expectsMandatoryRules && pluginConfig?.failSafe) {
    console.warn(
      `[memory-orkmind] regras mandatorias indisponiveis (${motivo}) - ` +
        "acionando fail-safe (D-MA9)"
    );
    return FALLBACK_NO_RULES;
  }
  return "";
}

/**
 * Monta o bloco de regras mandatorias do system prompt. NUNCA lanca.
 *
 * Independe de embedder e de `autoRecall` (D5): a garantia constitucional
 * nao pode depender de configuracao opcional de recuperacao.
 */
async function buildRulesSystemBlock(
  ctx?: PluginHookAgentContext
): Promise<string> {
  const cfg = pluginConfig;
  if (!cfg) return "";

  const budgetTokens = effectiveTokenBudget(
    cfg.tokenBudget,
    ctx?.contextTokenBudget
  );
  const budgetChars = mandatoryBudgetChars(budgetTokens, cfg.mandatoryBudgetRatio);

  // Cache dentro do TTL: zero ida ao banco por turno (D3).
  const agora = Date.now();
  if (
    cachedRules &&
    cfg.rulesCacheTtlMs > 0 &&
    agora - cachedRulesAt < cfg.rulesCacheTtlMs
  ) {
    return formatRulesBlock(cachedRules, budgetChars);
  }

  if (!dbPool) return onRulesLoadFailure("banco nao conectado");
  const pool = dbPool;

  // Schema: valida no primeiro uso e a cada turno enquanto estiver invalido.
  if (schemaValid !== true) {
    let valido = false;
    try {
      valido = await validateSchema(pool);
    } catch (err) {
      console.warn("[memory-orkmind] falha ao validar schema:", err);
      valido = false;
    }
    schemaValid = valido;
    if (!valido) return onRulesLoadFailure("schema invalido");
  }

  let rules: MemoryRow[];
  try {
    rules = await withTimeout(
      getMandatoryRules(pool),
      cfg.rulesTimeoutMs,
      "RULES_TIMEOUT"
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return onRulesLoadFailure(
      msg === "RULES_TIMEOUT" ? `timeout de ${cfg.rulesTimeoutMs}ms` : msg
    );
  }

  if (!rules || rules.length === 0) {
    return onRulesLoadFailure("backend devolveu zero regras");
  }

  cachedRules = rules;
  cachedRulesAt = agora;
  // D-MA9: a existencia de regras esta provada neste processo. Daqui em
  // diante, ausencia delas e falha, nao estado normal.
  expectsMandatoryRules = true;
  return formatRulesBlock(rules, budgetChars);
}

// --- Nivel 3: contexto recuperado (busca semantica) ------------------------

/**
 * Monta a query do nivel 3 a partir da janela de mensagens e do escopo (D7).
 *
 * O sufixo de escopo (projeto e agente) puxa o resultado para o contexto de
 * trabalho corrente em vez de depender so da ultima frase digitada.
 */
function buildRecallQuery(
  messages: unknown[],
  cfg: ResolvedConfig,
  ctx?: PluginHookAgentContext
): string {
  const janela = extractLastUserMessages(messages, cfg.recallWindowMessages);
  if (janela.length === 0) return "";

  const escopo: string[] = [];
  const workspace = ctx?.workspaceDir;
  if (workspace) {
    const projeto = workspace.replace(/\/+$/, "").split("/").pop();
    if (projeto) escopo.push(`projeto=${projeto}`);
  }
  if (ctx?.agentId) escopo.push(`agente=${ctx.agentId}`);

  const base = janela.join("\n").slice(0, cfg.recallMaxChars);
  return escopo.length > 0 ? `${base}\n[escopo: ${escopo.join(" ")}]` : base;
}

/**
 * Monta o bloco de contexto recuperado. NUNCA lanca; degrada em silencio.
 *
 * Exclui regras mandatorias do resultado: elas ja vao integralmente no
 * system prompt, reinjeta-las duplicaria custo sem ganho (D7).
 */
async function buildRecallBlock(
  event: PluginHookBeforePromptBuildEvent,
  ctx?: PluginHookAgentContext
): Promise<string> {
  const cfg = pluginConfig;
  if (!cfg || !dbPool) return "";
  const pool = dbPool;

  const query = buildRecallQuery(event.messages ?? [], cfg, ctx);
  if (!query) return "";

  try {
    const rows = await withTimeout(
      (async () => {
        if (embedder) {
          try {
            const vetor = await embedder.embed(query);
            return await searchByVector(
              pool,
              vetor,
              cfg.recallMaxResults,
              RECALL_MIN_SCORE,
              true
            );
          } catch (err) {
            console.warn(
              "[memory-orkmind] embedding falhou no recall, usando FTS:",
              err
            );
          }
        }
        // Sem embedder, ou com falha de embed: full-text search (D5).
        return await searchByText(pool, query, cfg.recallMaxResults, true);
      })(),
      RECALL_TIMEOUT_MS,
      "RECALL_TIMEOUT"
    );

    if (!rows || rows.length === 0) return "";

    const memoryContext = rows
      .map((r) => `- [${r.collection}] ${r.essence || r.content.slice(0, 200)}`)
      .join("\n");
    return `<relevant-memories source="orkmind">\n${memoryContext}\n</relevant-memories>`;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg === "RECALL_TIMEOUT") {
      console.warn("[memory-orkmind] auto-recall timeout, continuando sem recall");
    } else {
      console.error("[memory-orkmind] auto-recall falhou:", err);
    }
    return "";
  }
}

/**
 * Superficie de injecao usada pelos testes (A9) e pelo harness do benchmark.
 *
 * NAO faz parte do contrato do plugin com o gateway: o runtime do OpenClaw
 * nunca toca nisto. Existe porque o estado de governanca vive no modulo (o
 * processo do gateway e longevo, e a maquina de dois estados do D-MA9 depende
 * disso), e testar esse estado exige poder zera-lo e injetar dependencias.
 */
export const __testing = {
  reset: resetPluginState,
  setPool(p: pg.Pool | null): void {
    dbPool = p;
  },
  setEmbedder(e: EmbeddingClient | null): void {
    embedder = e;
  },
  setConfig(c: ResolvedConfig | null): void {
    pluginConfig = c;
  },
  setSchemaValid(v: boolean | null): void {
    schemaValid = v;
  },
  state() {
    return {
      expectsMandatoryRules,
      cachedRules,
      cachedRulesAt,
      hookFireCount,
      agentEndCount,
      schemaValid,
      injectionWarningEmitted,
    };
  },
};

export default definePluginEntry({
  id: "memory-orkmind",
  name: "Memory (OrkMind)",
  description: "Memoria semantica OrkMind via PostgreSQL + pgvector com busca vetorial",
  kind: "memory",

  register(api: any) {
    // api.pluginConfig entrega plugins.entries.<id>.config diretamente
    // (api.runtime.config.get() NAO existe no runtime do OpenClaw)
    const rawConfig = (api.pluginConfig ?? {}) as OrkMindPluginConfig;

    // Inicializacao lazy - sera feita no primeiro uso
    const init = () => {
      try {
        ensureInitialized(rawConfig);
      } catch (err) {
        console.error("[memory-orkmind]", err);
      }
    };

    // Registra a capability de memoria (declara posse do slot)
    api.registerMemoryCapability({
      promptBuilder: ({ availableTools }) => {
        const hasSearch = availableTools.has("memory_search");
        const hasGet = availableTools.has("memory_get");
        const hasRecall = availableTools.has("memory_recall");

        const sections: string[] = [];

        // D-MA5: cabecalho e guardrail anti-destruicao sao INCONDICIONAIS.
        // A guarda por availableTools vale apenas para as instrucoes de uso
        // das tools: sem ela, um agente sem tools de memoria ficaria sem
        // nenhuma protecao contra compactar ou apagar memoria. Texto estatico
        // e sem I/O, entao fica no espaco cacheavel com custo por turno zero.
        sections.push("## Memoria (OrkMind)");
        sections.push(
          "OrkMind (PostgreSQL + pgvector) e o provedor OFICIAL e UNICO de memoria deste agente. " +
          "TODA informacao persistente (fatos, preferencias, decisoes, pessoas, datas, tarefas) " +
          "esta armazenada no OrkMind. NAO existe outro backend de memoria."
        );
        sections.push(GUARDRAIL_ANTI_DESTRUICAO);

        if (hasSearch) {
          sections.push(
            "Antes de responder QUALQUER pergunta sobre trabalho anterior, decisoes, datas, " +
            "pessoas, preferencias ou tarefas: execute memory_search com uma query relevante. " +
            "Se a confianca for baixa apos a busca, diga que verificou."
          );
        }

        if (hasGet) {
          sections.push(
            "Use memory_get para ler o conteudo completo de uma memoria especifica " +
            "(pelo caminho orkmind://<id> retornado por memory_search)."
          );
        }

        if (hasRecall) {
          sections.push(
            "memory_recall e um alias de busca semantica (mesmo backend que memory_search)."
          );
        }

        if (hasSearch || hasGet || hasRecall) {
          sections.push(
            "Use memory_store para salvar fatos, preferencias e decisoes importantes para conversas futuras."
          );

          sections.push(
            "Use memory_forget para remover memorias incorretas ou desatualizadas."
          );

          sections.push(
            "PROIBIDO: NAO use arquivos MEMORY.md, memory/*.md, ou qualquer arquivo de texto como " +
            "sistema de memoria. NUNCA crie, leia ou edite esses arquivos para fins de memoria. " +
            "TODA memoria DEVE passar exclusivamente pelas tools memory_search, memory_get, " +
            "memory_store e memory_forget."
          );

          sections.push(
            "Sempre execute memory_search ANTES de afirmar que nao possui informacoes sobre o usuario."
          );
        }

        sections.push("");
        return sections;
      },
      runtime: {
        getMemorySearchManager: async (params) => {
          init();
          if (!dbPool) return { manager: null, error: "Banco nao conectado" };
          const pool = dbPool;
          const emb = embedder;
          return {
            manager: {
              async search(query, opts) {
                const limit = opts?.maxResults ?? 10;
                const minScore = opts?.minScore ?? 0.3;
                if (emb) {
                  try {
                    const embedding = await emb.embed(query);
                    const rows = await searchByVector(pool, embedding, limit, minScore);
                    return rows.map((r) => ({
                      path: `orkmind://${r.id}`,
                      startLine: 0,
                      endLine: 0,
                      score: r.score ?? 0,
                      snippet: r.essence || r.content,
                      source: "memory" as const,
                    }));
                  } catch {
                    // Fallback
                  }
                }
                return [];
              },
              async readFile(params) {
                return {
                  text: "",
                  path: params.relPath,
                  truncated: false,
                };
              },
              status() {
                return {
                  backend: "builtin" as const,
                  provider: "orkmind-pgvector",
                  model: pluginConfig?.embedding.model,
                };
              },
              async probeEmbeddingAvailability() {
                return { ok: !!emb, checked: true };
              },
              async probeVectorAvailability() {
                return !!emb;
              },
            },
            debug: { backend: "builtin" as const },
          };
        },
        resolveMemoryBackendConfig: () => ({ backend: "builtin" as const }),
        closeAllMemorySearchManagers: async () => {
          await closePool();
          resetPluginState();
        },
      },
    });

    // Registra as ferramentas de memoria
    // memory_search e memory_get: contrato obrigatorio do OpenClaw (runtime espera esses nomes)
    api.registerTool(
      () => {
        init();
        return createMemorySearchTool(dbPool!, embedder, pluginConfig!);
      },
      { names: ["memory_search"] }
    );

    api.registerTool(
      () => {
        init();
        return createMemoryGetTool(dbPool!, embedder, pluginConfig!);
      },
      { names: ["memory_get"] }
    );

    // memory_recall, memory_store, memory_forget: tools proprias do OrkMind (compat AGENTS.md)
    api.registerTool(
      () => {
        init();
        return createMemoryRecallTool(dbPool!, embedder, pluginConfig!);
      },
      { names: ["memory_recall"] }
    );

    api.registerTool(
      () => {
        init();
        return createMemoryStoreTool(dbPool!, embedder, pluginConfig!);
      },
      { names: ["memory_store"] }
    );

    api.registerTool(
      () => {
        init();
        return createMemoryForgetTool(dbPool!, embedder, pluginConfig!);
      },
      { names: ["memory_forget"] }
    );

    // Hook: injecao de governanca + auto-recall antes do prompt.
    //
    // Dois caminhos INDEPENDENTES:
    //   N1/N2 (regras mandatorias) - incondicional, so precisa de pool.
    //                                Falha termina em ALERTA, nunca em silencio.
    //   N3 (contexto recuperado)   - condicionado a autoRecall e pool.
    //                                Degrada em silencio, por definicao do nivel.
    api.on(
      "before_prompt_build",
      async (
        event: PluginHookBeforePromptBuildEvent,
        ctx?: PluginHookAgentContext
      ): Promise<PluginHookBeforePromptBuildResult | undefined> => {
        init();
        hookFireCount++;
        if (!pluginConfig) return undefined;

        const out: PluginHookBeforePromptBuildResult = {};

        // N1/N2: independe de embedder e de autoRecall (D5).
        // prependSystemContext e o campo que o SDK declara cacheavel pelo
        // provider: bloco estavel, custo por turno proximo de zero (D1).
        //
        // O guardrail D-MA5 sai por AQUI, e nao apenas pelo promptBuilder da
        // capability. Motivo medido na verificacao A11 de 31/08/2026: o
        // promptBuilder NAO roda em subagentes (`sessions_spawn`), enquanto
        // este hook roda. Era exatamente a contingencia prevista no D1 do
        // plano. Ele continua tambem no promptBuilder porque la ele sobrevive
        // ao caso inverso, `hooks.allowPromptInjection: false`, em que este
        // hook nunca dispara (D10). As duas vias cobrem falhas opostas; um
        // guardrail constitucional duplicado em espaco cacheavel custa quase
        // nada perto de ficar ausente numa delas.
        const rulesBlock = await buildRulesSystemBlock(ctx);
        out.prependSystemContext = GUARDRAIL_ANTI_DESTRUICAO + rulesBlock;

        // N3: melhor esforco.
        if (pluginConfig.autoRecall && dbPool) {
          const recallBlock = await buildRecallBlock(event, ctx);
          if (recallBlock) out.appendContext = recallBlock;
        }

        return out.prependSystemContext || out.appendContext ? out : undefined;
      }
    );

    // Hook: auto-capture apos resposta do agente (agent_end)
    api.on("agent_end", async (event: PluginHookAgentEndEvent) => {
      init();
      agentEndCount++;
      maybeWarnPromptInjectionDisabled();
      if (!pluginConfig?.autoCapture || !dbPool) return;
      const ev = event;
      if (!ev.success) return;

      const messages = ev.messages ?? [];
      const userMessage = extractLastUserMessage(messages);
      if (!userMessage) return;
      if (userMessage.length > pluginConfig.captureMaxChars) return;

      // Verifica se parece com contexto ja injetado
      if (userMessage.includes("<relevant-memories")) return;

      let shouldCapture = false;
      let captureCollection = "preference";
      let captureContent = userMessage;

      if (pluginConfig.autoCapture === "smart") {
        // Modo inteligente: classificador LLM decide se vale capturar
        const apiKey = pluginConfig.embedding.apiKey;
        if (apiKey) {
          try {
            const { classifyForCapture } = await import("./classifier.js");
            const result = await classifyForCapture(
              userMessage,
              apiKey,
              pluginConfig.smartCaptureModel
            );
            shouldCapture = result.shouldStore;
            captureCollection = result.collection;
            captureContent = result.summary || userMessage;
          } catch (err) {
            console.error("[memory-orkmind] classificador smart falhou, usando triggers:", err);
            // Fallback para triggers literais
            shouldCapture = matchesCaptureTrigger(userMessage, pluginConfig.captureTriggers);
          }
        } else {
          // Sem apiKey, fallback para triggers literais
          shouldCapture = matchesCaptureTrigger(userMessage, pluginConfig.captureTriggers);
        }
      } else {
        // Modo booleano: usa triggers literais
        shouldCapture = matchesCaptureTrigger(userMessage, pluginConfig.captureTriggers);
      }

      if (!shouldCapture) return;

      const { randomUUID } = await import("node:crypto");

      let embedding: number[] | null = null;
      if (embedder) {
        try {
          embedding = await embedder.embed(captureContent);
        } catch {
          // Continua sem embedding
        }
      }

      try {
        const { insertMemory } = await import("./database.js");
        await insertMemory(dbPool, {
          id: randomUUID(),
          content: captureContent,
          collection: captureCollection,
          tags: {},
          priority: "medium",
          source: "agent",
          embedding,
        });
        console.log(
          `[memory-orkmind] auto-capture: [${captureCollection}] ${captureContent.slice(0, 80)}`
        );
      } catch (err) {
        console.error("[memory-orkmind] auto-capture falhou:", err);
      }
    });

    // Hook: limpeza na parada do gateway
    api.on("gateway_stop", async () => {
      await closePool();
      resetPluginState();
    });

    // Registra CLI para operacoes de memoria
    api.registerCli(
      async ({ program }: { program: any }) => {
        const ltm = program
          .command("ltm")
          .description("Gerenciar memoria OrkMind (PostgreSQL + pgvector)");

        ltm
          .command("stats")
          .description("Estatisticas da memoria")
          .action(async () => {
            init();
            if (!dbPool) {
              console.log("Erro: banco nao conectado");
              return;
            }
            const count = await countMemories(dbPool);
            console.log(`Memorias ativas: ${count}`);
            console.log(`Backend: PostgreSQL + pgvector`);
            console.log(`Modelo de embedding: ${pluginConfig?.embedding.model ?? "nao configurado"}`);
            console.log(`Dimensoes: ${pluginConfig?.embedding.dimensions ?? "N/A"}`);
          });

        ltm
          .command("list")
          .description("Listar memorias recentes")
          .option("--limit <n>", "Numero maximo de resultados", "10")
          .option("--order-by-created-at", "Ordenar por data de criacao")
          .action(async (opts: { limit: string; orderByCreatedAt?: boolean }) => {
            init();
            if (!dbPool) {
              console.log("Erro: banco nao conectado");
              return;
            }
            const rows = await listMemories(
              dbPool,
              parseInt(opts.limit, 10),
              opts.orderByCreatedAt
            );
            if (rows.length === 0) {
              console.log("Nenhuma memoria encontrada.");
              return;
            }
            for (const row of rows) {
              console.log(
                `[${row.id}] [${row.collection}/${row.priority}] ${row.content.slice(0, 120)}`
              );
            }
          });

        ltm
          .command("search <query>")
          .description("Buscar memorias por similaridade vetorial")
          .option("--limit <n>", "Numero maximo de resultados", "5")
          .action(async (query: string, opts: { limit: string }) => {
            init();
            if (!dbPool) {
              console.log("Erro: banco nao conectado");
              return;
            }
            const limit = parseInt(opts.limit, 10);
            if (embedder) {
              try {
                const embedding = await embedder.embed(query);
                const rows = await searchByVector(dbPool, embedding, limit);
                if (rows.length === 0) {
                  console.log("Nenhuma memoria encontrada.");
                  return;
                }
                for (const row of rows) {
                  const score = row.score?.toFixed(3) ?? "?";
                  console.log(
                    `[${row.id}] (score: ${score}) [${row.collection}] ${row.content.slice(0, 120)}`
                  );
                }
                return;
              } catch (err) {
                console.error("Busca vetorial falhou, usando FTS:", err);
              }
            }
            const { searchByText } = await import("./database.js");
            const rows = await searchByText(dbPool, query, limit);
            if (rows.length === 0) {
              console.log("Nenhuma memoria encontrada.");
              return;
            }
            for (const row of rows) {
              console.log(
                `[${row.id}] [${row.collection}] ${row.content.slice(0, 120)}`
              );
            }
          });

        // Subcomando: seed (semeadura de memorias)
        const seed = ltm
          .command("seed")
          .description("Semear memorias a partir de arquivos do workspace ou JSON externo");

        seed
          .command("from-agents")
          .description("Extrair fatos de USER.md, SOUL.md e IDENTITY.md do workspace")
          .option("--workspace <path>", "Caminho do workspace", process.env.HOME + "/.openclaw/workspace")
          .action(async (opts: { workspace: string }) => {
            init();
            if (!dbPool) {
              console.log("Erro: banco nao conectado");
              return;
            }
            const { seedFromAgents } = await import("./seed.js");
            console.log(`Semeando a partir de: ${opts.workspace}`);
            const result = await seedFromAgents(dbPool, embedder, opts.workspace);
            console.log(
              `\nSemeadura concluida: ${result.inserted} inseridas, ${result.skipped} ignoradas (total: ${result.total})`
            );
          });

        seed
          .command("from-file <path>")
          .description("Importar memorias de um arquivo JSON")
          .action(async (filePath: string) => {
            init();
            if (!dbPool) {
              console.log("Erro: banco nao conectado");
              return;
            }
            const { seedFromFile } = await import("./seed.js");
            console.log(`Importando de: ${filePath}`);
            const result = await seedFromFile(dbPool, embedder, filePath);
            console.log(
              `\nImportacao concluida: ${result.inserted} inseridas, ${result.skipped} ignoradas (total: ${result.total})`
            );
          });

        seed
          .command("clean")
          .description("Remover todas as memorias inseridas por seed (source=bootstrap)")
          .action(async () => {
            init();
            if (!dbPool) {
              console.log("Erro: banco nao conectado");
              return;
            }
            const { removeSeedEntries } = await import("./seed.js");
            const count = await removeSeedEntries(dbPool);
            console.log(`Removidas ${count} memorias de bootstrap.`);
          });
      },
      {
        descriptors: [
          {
            name: "ltm",
            description: "Gerenciar memoria OrkMind (PostgreSQL + pgvector)",
            hasSubcommands: true,
          },
        ],
      }
    );
  },
});
