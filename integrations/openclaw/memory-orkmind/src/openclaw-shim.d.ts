// Declaracoes de tipo para o SDK do OpenClaw
// Shim minimo para compilacao sem depender do pacote instalado globalmente

declare module "openclaw/plugin-sdk/plugin-entry" {
  // Contrato real do SDK, copiado de
  // ~/.local/lib/node_modules/openclaw/dist/hook-types-DQ9eTy2x.d.ts
  // (linhas 47-66 e 389-414). Mantido minimo: so o que o plugin usa.

  export interface PluginHookBeforePromptBuildEvent {
    prompt?: string;
    /** Mensagens da sessao ja preparadas para este turno. */
    messages?: unknown[];
  }

  export interface PluginHookBeforePromptBuildResult {
    systemPrompt?: string;
    prependContext?: string;
    appendContext?: string;
    /**
     * Anexado ANTES do system prompt do agente, no espaco que os providers
     * conseguem cachear (prompt caching). E o canal correto para conteudo
     * estavel de plugin, evitando custo de token por turno.
     */
    prependSystemContext?: string;
    /** Anexado DEPOIS do system prompt do agente, tambem cacheavel. */
    appendSystemContext?: string;
  }

  export type PluginHookContextWindowSource =
    | "model"
    | "modelsConfig"
    | "agentContextTokens"
    | "default";

  export interface PluginHookAgentContext {
    runId?: string;
    jobId?: string;
    agentId?: string;
    sessionKey?: string;
    sessionId?: string;
    workspaceDir?: string;
    modelProviderId?: string;
    modelId?: string;
    messageProvider?: string;
    /** Id do canal/plugin em execucoes originadas de canal (ex: `discord`). */
    channel?: string;
    /** Id da conversa alvo em execucoes originadas de canal. */
    chatId?: string;
    /** Identidade de quem enviou, quando disponivel. */
    senderId?: string;
    trigger?: string;
    channelId?: string;
    /** Budget efetivo de tokens de contexto ja resolvido pelo runtime. */
    contextTokenBudget?: number;
    /** Origem do budget de contexto resolvido. */
    contextWindowSource?: PluginHookContextWindowSource;
    /** Janela nativa de referencia quando um teto menor prevalece. */
    contextWindowReferenceTokens?: number;
  }

  export interface PluginHookAgentEndEvent {
    success?: boolean;
    messages?: unknown[];
  }

  export interface OpenClawPluginApi {
    /**
     * Config do plugin resolvida pelo gateway a partir de
     * `plugins.entries.<id>.config`. E o unico caminho valido: o
     * `runtime.config.get()` nao existe no runtime do OpenClaw.
     */
    pluginConfig?: unknown;
    runtime?: {
      config?: {
        get?: (pluginId: string) => unknown;
      };
    };
    registrationMode?: string;
    registerMemoryCapability: (capability: {
      promptBuilder?: (params: { availableTools: Set<string> }) => string[];
      runtime?: {
        getMemorySearchManager: (params: {
          cfg: unknown;
          agentId: string;
          purpose?: string;
        }) => Promise<{
          manager: {
            search: (query: string, opts?: {
              maxResults?: number;
              minScore?: number;
            }) => Promise<Array<{
              path: string;
              startLine: number;
              endLine: number;
              score: number;
              snippet: string;
              source: "memory" | "sessions";
            }>>;
            readFile: (params: { relPath: string }) => Promise<{
              text: string;
              path: string;
              truncated?: boolean;
            }>;
            status: () => {
              backend: "builtin" | "qmd";
              provider: string;
              model?: string;
            };
            probeEmbeddingAvailability: () => Promise<{ ok: boolean; checked?: boolean }>;
            probeVectorAvailability: () => Promise<boolean>;
          } | null;
          debug?: { backend?: string };
          error?: string;
        }>;
        resolveMemoryBackendConfig: (params: {
          cfg: unknown;
          agentId: string;
        }) => { backend: "builtin" | "qmd" };
        closeAllMemorySearchManagers?: () => Promise<void>;
      };
    }) => void;
    // Assinatura real do runtime: o id da chamada vem antes dos parametros, e
    // o resultado traz `content` (o que o modelo le) e `details`.
    registerTool: (
      factory: (ctx?: unknown) => {
        name: string;
        label?: string;
        description: string;
        parameters?: unknown;
        execute: (
          toolCallId: string,
          params: Record<string, unknown>,
          signal?: AbortSignal,
          onUpdate?: (partial: unknown) => void
        ) => Promise<{
          content: Array<{ type: "text"; text: string }>;
          details: unknown;
        }>;
      },
      opts?: { names?: string[] }
    ) => void;
    on: {
      (
        hookName: "before_prompt_build",
        handler: (
          event: PluginHookBeforePromptBuildEvent,
          ctx?: PluginHookAgentContext
        ) => Promise<PluginHookBeforePromptBuildResult | void> | PluginHookBeforePromptBuildResult | void,
        opts?: { priority?: number; timeoutMs?: number }
      ): void;
      (
        hookName: "agent_end",
        handler: (
          event: PluginHookAgentEndEvent,
          ctx?: PluginHookAgentContext
        ) => Promise<void> | void,
        opts?: { priority?: number; timeoutMs?: number }
      ): void;
      (
        hookName: string,
        handler: (
          event: Record<string, unknown>,
          ctx?: PluginHookAgentContext
        ) => Promise<Record<string, unknown> | void> | void,
        opts?: { priority?: number; timeoutMs?: number }
      ): void;
    };
    registerCli: (
      registrar: (deps: { program: {
        command: (name: string) => {
          description: (desc: string) => unknown;
          option: (flags: string, desc: string, defaultVal?: string) => unknown;
          command: (name: string) => unknown;
          action: (fn: (...args: unknown[]) => unknown) => unknown;
        };
      }}) => Promise<void>,
      opts?: {
        descriptors?: Array<{
          name: string;
          description: string;
          hasSubcommands?: boolean;
        }>;
      }
    ) => void;
  }

  export function definePluginEntry(opts: {
    id: string;
    name: string;
    description: string;
    kind?: string;
    configSchema?: unknown;
    register: (api: OpenClawPluginApi) => void;
  }): unknown;
}
