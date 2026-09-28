// Cliente de embeddings OpenAI-compatible para o plugin memory-orkmind
// Compativel com OpenRouter, OpenAI, e qualquer endpoint OpenAI-compatible
// Inclui timeout (15s) e retry (1 tentativa extra) para robustez de rede

export interface EmbeddingConfig {
  baseUrl: string;
  apiKey: string;
  model: string;
  dimensions: number;
}

export interface EmbeddingClient {
  embed(text: string): Promise<number[]>;
  embedBatch(texts: string[]): Promise<number[][]>;
  readonly dimensions: number;
}

const EMBEDDING_TIMEOUT_MS = 15000;
const MAX_RETRIES = 1;

// Verifica se o erro e transitorio (vale tentar de novo)
function isTransientError(status: number): boolean {
  return status === 429 || status === 502 || status === 503 || status === 504;
}

export function createEmbeddingClient(config: EmbeddingConfig): EmbeddingClient {
  const { baseUrl, apiKey, model, dimensions } = config;

  async function fetchWithTimeout(
    url: string,
    init: RequestInit,
    timeoutMs: number
  ): Promise<Response> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        ...init,
        signal: controller.signal,
      });
      return response;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function embedBatch(texts: string[]): Promise<number[][]> {
    if (texts.length === 0) return [];

    const requestInit: RequestInit = {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
      },
      body: JSON.stringify({ model, input: texts }),
    };

    let lastError: Error | null = null;

    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const response = await fetchWithTimeout(baseUrl, requestInit, EMBEDDING_TIMEOUT_MS);

        if (!response.ok) {
          const body = await response.text().catch(() => "");
          const err = new Error(
            `memory-orkmind: embedding API retornou ${response.status}: ${body.slice(0, 200)}`
          );

          // Retry apenas em erros transitorios
          if (isTransientError(response.status) && attempt < MAX_RETRIES) {
            console.warn(
              `[memory-orkmind] embedding: erro ${response.status}, tentando novamente (${attempt + 1}/${MAX_RETRIES})...`
            );
            lastError = err;
            continue;
          }

          throw err;
        }

        const json = (await response.json()) as {
          data?: Array<{ embedding: number[]; index: number }>;
        };

        if (!json.data || !Array.isArray(json.data)) {
          throw new Error(
            "memory-orkmind: resposta inesperada da API de embedding (sem campo 'data')"
          );
        }

        // Ordena por index para manter alinhamento com a entrada
        const sorted = [...json.data].sort((a, b) => a.index - b.index);
        return sorted.map((item) => item.embedding.map(Number));
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") {
          lastError = new Error("memory-orkmind: embedding timeout (15s)");
          if (attempt < MAX_RETRIES) {
            console.warn(
              `[memory-orkmind] embedding: timeout, tentando novamente (${attempt + 1}/${MAX_RETRIES})...`
            );
            continue;
          }
        } else if (err instanceof Error) {
          lastError = err;
          if (attempt < MAX_RETRIES && err.message.includes("fetch")) {
            console.warn(
              `[memory-orkmind] embedding: erro de rede, tentando novamente (${attempt + 1}/${MAX_RETRIES})...`
            );
            continue;
          }
        }
        throw lastError ?? err;
      }
    }

    throw lastError ?? new Error("memory-orkmind: embedding falhou apos retries");
  }

  async function embed(text: string): Promise<number[]> {
    const results = await embedBatch([text]);
    return results[0];
  }

  return { embed, embedBatch, dimensions };
}
