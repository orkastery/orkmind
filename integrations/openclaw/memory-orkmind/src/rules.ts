// Governanca de memoria portada do plugin Hermes (D-MA1, D-MA5, D-MA7, D-MA9).
//
// Fonte de verdade textual: integrations/hermes/orkmind/__init__.py.
// Os textos abaixo sao transcritos LITERALMENTE do Python, trocando apenas
// os nomes das tools (orkmind_recall/orkmind_search viram
// memory_recall/memory_search, que sao os nomes registrados no OpenClaw).
// Essa paridade textual e o que viabiliza a metrica M7 (fidelidade cruzada)
// do benchmark por comparacao direta de strings.
//
// Todas as funcoes deste modulo sao PURAS: nenhuma faz I/O. O acesso ao
// banco fica em database.ts e a orquestracao em index.ts.

// Separador que isola visualmente o bloco de regras do resto do system prompt.
export const RULES_SEPARATOR = "---";

// Fracao do budget de tokens reservada as regras mandatorias (D6 do plano).
export const MANDATORY_BUDGET_RATIO = 0.3;

// Estimativa grosseira: 1 token ~ 4 caracteres.
export const CHARS_PER_TOKEN = 4;

// Fracao maxima da janela real de contexto que o bloco de regras pode
// ocupar quando o runtime informa `ctx.contextTokenBudget` (D6).
export const CTX_BUDGET_SHARE = 0.15;

/**
 * D-MA5: guardrail anti-destruicao, sempre presente no system prompt.
 *
 * Reforca que o OrkMind e a fonte autoritativa e que nenhuma memoria pode
 * ser descartada para abrir espaco. Texto estatico e sem I/O: fica no espaco
 * cacheavel do prompt, com custo por turno zero.
 */
export const GUARDRAIL_ANTI_DESTRUICAO =
  "\n## Governanca de Memoria\n" +
  "IMPORTANTE: Voce NAO deve compactar, resumir destrutivamente, " +
  "deletar ou sobrescrever memorias para abrir espaco. Todo conteudo " +
  "e armazenado integralmente no OrkMind sem limite de espaco. " +
  "Se precisar de informacao, busque via memory_recall (tags + " +
  "semantica) ou memory_search (texto livre). O OrkMind e sua " +
  "fonte autoritativa de memoria - tudo que voce precisa saber " +
  "esta la, acessavel por busca.\n";

/**
 * D-MA9: bloco de alerta exibido quando o plugin ja carregou regras
 * mandatorias neste processo mas o backend ficou indisponivel.
 *
 * Melhor alertar explicitamente do que operar em silencio sem governanca.
 */
export const FALLBACK_NO_RULES =
  "\n## ALERTA: Regras de Governanca Indisponiveis\n" +
  "O OrkMind nao conseguiu carregar as regras mandatorias.\n" +
  "NAO prossiga com nenhuma operacao critica ate que as\n" +
  "regras estejam disponiveis. Tente reconectar via memory_search.\n";

/** Forma minima de uma regra mandatoria usada na formatacao. */
export interface RuleLike {
  content?: string;
  collection?: string;
  priority?: string;
  tags?: Record<string, string[]> | null;
}

/**
 * Agrupa regras pela primeira tag de dominio (fallback: "geral").
 *
 * Paridade com `_group_rules_by_domain` (__init__.py:1013-1024).
 */
export function groupRulesByDomain(
  rules: RuleLike[]
): Record<string, RuleLike[]> {
  const grouped: Record<string, RuleLike[]> = {};
  for (const rule of rules) {
    const tags = rule.tags || {};
    const domains = tags.domain || [];
    const domain = domains.length > 0 ? domains[0] : "geral";
    if (!grouped[domain]) grouped[domain] = [];
    grouped[domain].push(rule);
  }
  return grouped;
}

/**
 * Orcamento em caracteres reservado ao bloco de regras mandatorias.
 *
 * Paridade com `_mandatory_budget_chars` (__init__.py:932-934):
 * `int(token_budget * MANDATORY_BUDGET_RATIO) * CHARS_PER_TOKEN`.
 * O `Math.trunc` reproduz o `int()` do Python (truncagem, nao arredondamento).
 */
export function mandatoryBudgetChars(
  tokenBudget: number,
  ratio: number = MANDATORY_BUDGET_RATIO
): number {
  return Math.trunc(tokenBudget * ratio) * CHARS_PER_TOKEN;
}

/**
 * Budget de tokens efetivo do bloco de regras (D6).
 *
 * Quando o runtime informa a janela real (`ctx.contextTokenBudget`), aplica
 * o clamp `min(configBudget, floor(ctxBudget * CTX_BUDGET_SHARE))` para o
 * bloco nunca ocupar mais que uma fracao da janela de fato disponivel.
 * Sem informacao do runtime, vale o budget da config.
 */
export function effectiveTokenBudget(
  configBudget: number,
  ctxBudget?: number | null
): number {
  if (typeof ctxBudget !== "number" || !Number.isFinite(ctxBudget) || ctxBudget <= 0) {
    return configBudget;
  }
  return Math.min(configBudget, Math.floor(ctxBudget * CTX_BUDGET_SHARE));
}

/**
 * Formata as regras mandatorias com destaque visual (D-MA7).
 *
 * Paridade literal com `_format_rules_block` (__init__.py:960-1011): caixa
 * alta no titulo, agrupamento por dominio em ordem alfabetica, marcador
 * `[CRITICA]` / `[MANDATORIA]` por prioridade, truncagem por budget com
 * aviso explicito de omissao e separadores isolando o bloco.
 *
 * Retorna string vazia quando nada foi renderizado (nenhuma regra com
 * conteudo), reproduzindo o `if not rendered: return ""` do Python.
 */
export function formatRulesBlock(
  rules: RuleLike[],
  budgetChars: number
): string {
  const grouped = groupRulesByDomain(rules);

  const lines: string[] = [
    "",
    RULES_SEPARATOR,
    "## REGRAS MANDATORIAS (OBEDECER SEMPRE)",
    "",
  ];
  let used = 0;
  let truncated = 0;
  let rendered = 0;

  // `sorted(grouped)` do Python: ordem alfabetica das chaves.
  for (const domain of Object.keys(grouped).sort()) {
    const groupLines: string[] = [];
    for (const rule of grouped[domain]) {
      const content = String(rule.content ?? "").trim();
      if (!content) continue;
      const collection = rule.collection ?? "";
      const priority = rule.priority ?? "";
      const marker = priority === "critical" ? "[CRITICA]" : "[MANDATORIA]";
      const line = `- ${marker} [${collection}] ${content}`;
      if (used + line.length > budgetChars && rendered > 0) {
        truncated += 1;
        continue;
      }
      groupLines.push(line);
      used += line.length;
      rendered += 1;
    }
    if (groupLines.length > 0) {
      lines.push(`### DOMINIO: ${domain.toUpperCase()}`);
      lines.push(...groupLines);
      lines.push("");
    }
  }

  if (rendered === 0) return "";

  if (truncated > 0) {
    lines.push(
      `(${truncated} regra(s) omitida(s) por budget. ` +
        "Use memory_search para ver todas.)"
    );
  }
  lines.push(RULES_SEPARATOR);
  lines.push("");
  return lines.join("\n");
}
