// Classificador LLM para captura inteligente de memorias
// Usa um modelo leve via OpenRouter para decidir se um turno contem fatos/preferencias

export interface ClassifyResult {
  shouldStore: boolean;
  collection: "fact" | "preference";
  summary: string;
}

const CLASSIFY_PROMPT = `Voce e um classificador de memoria. Analise a mensagem do usuario e decida:
1. O turno contem um fato pessoal, preferencia ou decisao que vale persistir? (sim/nao)
2. Se sim, qual a categoria: "fact" (fato pessoal, dado, contexto) ou "preference" (preferencia, gosto, escolha)?
3. Se sim, resuma em UMA frase curta o que deve ser salvo.

NUNCA classifique como regra ou instrucao - apenas fatos e preferencias.
Ignore mensagens que sao perguntas, pedidos de ajuda, comandos ou contexto efemero.

Responda APENAS com JSON valido no formato:
{"shouldStore": true/false, "collection": "fact"|"preference", "summary": "..."}

Se nao deve salvar, responda: {"shouldStore": false, "collection": "fact", "summary": ""}`;

export async function classifyForCapture(
  userMessage: string,
  apiKey: string,
  model: string,
  baseUrl = "https://openrouter.ai/api/v1/chat/completions"
): Promise<ClassifyResult> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);

  try {
    const response = await fetch(baseUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model,
        messages: [
          { role: "system", content: CLASSIFY_PROMPT },
          { role: "user", content: userMessage.slice(0, 500) },
        ],
        max_tokens: 120,
        temperature: 0,
      }),
      signal: controller.signal,
    });

    clearTimeout(timeout);

    if (!response.ok) {
      return { shouldStore: false, collection: "fact", summary: "" };
    }

    const json = (await response.json()) as {
      choices?: Array<{ message?: { content?: string } }>;
    };

    const text = json.choices?.[0]?.message?.content?.trim();
    if (!text) return { shouldStore: false, collection: "fact", summary: "" };

    // Extrai JSON da resposta (pode vir com markdown codeblock)
    const jsonMatch = text.match(/\{[\s\S]*\}/);
    if (!jsonMatch) return { shouldStore: false, collection: "fact", summary: "" };

    const parsed = JSON.parse(jsonMatch[0]) as ClassifyResult;

    // Nunca permitir collection "rule" ou "instruction"
    if (
      parsed.collection !== "fact" &&
      parsed.collection !== "preference"
    ) {
      parsed.collection = "fact";
    }

    return {
      shouldStore: !!parsed.shouldStore,
      collection: parsed.collection,
      summary: typeof parsed.summary === "string" ? parsed.summary : "",
    };
  } catch {
    clearTimeout(timeout);
    return { shouldStore: false, collection: "fact", summary: "" };
  }
}
