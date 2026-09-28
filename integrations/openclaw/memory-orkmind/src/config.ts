// Configuracao do plugin memory-orkmind
// Valores padrao e resolucao de variaveis de ambiente

export interface OrkMindPluginConfig {
  databaseUrl?: string;
  embedding?: {
    provider?: string;
    model?: string;
    baseUrl?: string;
    apiKey?: string;
    dimensions?: number;
  };
  autoRecall?: boolean;
  autoCapture?: boolean | "smart";
  recallMaxChars?: number;
  captureMaxChars?: number;
  recallMaxResults?: number;
  customTriggers?: string[];
  smartCaptureModel?: string;
  // --- Governanca de memoria (N1/N2), portada do plugin Hermes ---
  tokenBudget?: number;
  mandatoryBudgetRatio?: number;
  failSafe?: boolean;
  rulesCacheTtlMs?: number;
  rulesTimeoutMs?: number;
  recallWindowMessages?: number;
}

export interface ResolvedConfig {
  databaseUrl: string;
  embedding: {
    baseUrl: string;
    apiKey: string;
    model: string;
    dimensions: number;
  };
  autoRecall: boolean;
  autoCapture: boolean | "smart";
  recallMaxChars: number;
  captureMaxChars: number;
  recallMaxResults: number;
  captureTriggers: string[];
  smartCaptureModel: string;
  /** Budget de tokens do plugin; base do calculo do bloco de regras (D6). */
  tokenBudget: number;
  /** Fracao do budget reservada as regras mandatorias (D6). */
  mandatoryBudgetRatio: number;
  /** D-MA9: alerta explicito quando as regras somem apos uma carga previa. */
  failSafe: boolean;
  /** TTL do cache de regras; dentro dele nao ha ida ao banco por turno (D3). */
  rulesCacheTtlMs: number;
  /** Timeout proprio de N1/N2; estourar vira ALERTA, nunca silencio (D4). */
  rulesTimeoutMs: number;
  /** Janela de mensagens de usuario que ancora a query do nivel 3 (D7). */
  recallWindowMessages: number;
}

// Frases que indicam intencao de memorizar (pt-BR, en, etc)
const DEFAULT_TRIGGERS = [
  "lembre-se",
  "lembra",
  "memorize",
  "prefiro",
  "minha preferencia",
  "remember",
  "prefer",
  "my preference",
  "don't forget",
  "nao esqueca",
  "anote",
  "guarde isso",
];

// Expande referencias ${ENV_VAR} para valores do ambiente
function expandEnvVars(value: string | undefined): string {
  if (!value) return "";
  return value.replace(/\$\{([^}]+)\}/g, (_, name) => {
    return process.env[name] ?? "";
  });
}

// Prende um numero na faixa [min, max]. Valores nao numericos (undefined,
// NaN, string vinda de config malformada) caem no default informado pelo
// chamador via `??`, entao aqui basta tratar NaN.
function clamp(value: number, min: number, max: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return min;
  return Math.min(Math.max(value, min), max);
}

export function resolveConfig(raw: OrkMindPluginConfig): ResolvedConfig {
  const databaseUrl =
    expandEnvVars(raw.databaseUrl) ||
    process.env.ORKMIND_DATABASE_URL ||
    "";

  if (!databaseUrl) {
    throw new Error(
      "memory-orkmind: databaseUrl nao configurado. " +
        "Defina config.databaseUrl ou a variavel ORKMIND_DATABASE_URL."
    );
  }

  // Resolucao da apiKey com fallback em cadeia:
  // 1. Valor expandido do config (ex: "${OPENROUTER_API_KEY}")
  // 2. Variavel de ambiente OPENROUTER_API_KEY
  // 3. Vazio (plugin opera em FTS fallback, sem crash)
  const expandedApiKey = expandEnvVars(raw.embedding?.apiKey);
  const embeddingApiKey =
    expandedApiKey ||
    process.env.OPENROUTER_API_KEY ||
    "";

  if (!embeddingApiKey) {
    console.warn(
      "[memory-orkmind] apiKey de embedding nao encontrada. " +
        "Tentou: config, env OPENROUTER_API_KEY. " +
        "Plugin operara em modo FTS (sem busca vetorial)."
    );
  }

  const embeddingBaseUrl =
    expandEnvVars(raw.embedding?.baseUrl) ||
    "https://openrouter.ai/api/v1/embeddings";

  const embeddingModel = raw.embedding?.model || "perplexity/pplx-embed-v1-0.6b";
  const embeddingDimensions = raw.embedding?.dimensions || 1024;

  const customTriggers = raw.customTriggers ?? [];
  const captureTriggers = [...DEFAULT_TRIGGERS, ...customTriggers];

  const autoCapture = raw.autoCapture ?? false;
  if (autoCapture === "smart") {
    console.log(
      "[memory-orkmind] autoCapture=smart ativado: 1 chamada LLM extra por turno para classificacao."
    );
  }

  return {
    databaseUrl,
    embedding: {
      baseUrl: embeddingBaseUrl,
      apiKey: embeddingApiKey,
      model: embeddingModel,
      dimensions: embeddingDimensions,
    },
    autoRecall: raw.autoRecall ?? true,
    autoCapture,
    recallMaxChars: Math.min(Math.max(raw.recallMaxChars ?? 1000, 100), 10000),
    captureMaxChars: Math.min(Math.max(raw.captureMaxChars ?? 500, 100), 10000),
    recallMaxResults: Math.min(Math.max(raw.recallMaxResults ?? 5, 1), 50),
    captureTriggers,
    smartCaptureModel: raw.smartCaptureModel || "z-ai/glm-5.3-flash",
    tokenBudget: clamp(raw.tokenBudget ?? 4000, 500, 100000),
    mandatoryBudgetRatio: clamp(raw.mandatoryBudgetRatio ?? 0.3, 0.1, 0.9),
    failSafe: raw.failSafe ?? true,
    rulesCacheTtlMs: clamp(raw.rulesCacheTtlMs ?? 60000, 0, 3600000),
    rulesTimeoutMs: clamp(raw.rulesTimeoutMs ?? 2000, 250, 10000),
    recallWindowMessages: clamp(raw.recallWindowMessages ?? 3, 1, 10),
  };
}
