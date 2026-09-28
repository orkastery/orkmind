// Testes do modulo de governanca (A3).
//
// O criterio de pronto do A3 e paridade textual com o plugin Hermes: dado o
// mesmo conjunto de regras, o bloco produzido pelo TS e o produzido pelo
// Python devem ser identicos, exceto pelos nomes das tools. Os arquivos
// `test/fixtures/*.golden.txt` foram GERADOS pelo `_format_rules_block` do
// Python (integrations/hermes/orkmind/__init__.py) e sao a especificacao.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  CHARS_PER_TOKEN,
  CTX_BUDGET_SHARE,
  FALLBACK_NO_RULES,
  GUARDRAIL_ANTI_DESTRUICAO,
  MANDATORY_BUDGET_RATIO,
  RULES_SEPARATOR,
  effectiveTokenBudget,
  formatRulesBlock,
  groupRulesByDomain,
  mandatoryBudgetChars,
  type RuleLike,
} from "../src/rules.js";

const FIXTURES = fileURLToPath(new URL("../../test/fixtures/", import.meta.url));

function lerFixture(nome: string): string {
  return readFileSync(FIXTURES + nome, "utf8");
}

const REGRAS: RuleLike[] = JSON.parse(lerFixture("rules-fixture.json"));

// A unica divergencia autorizada entre Python e TS sao os nomes das tools:
// o Hermes expoe orkmind_rules/orkmind_recall/orkmind_search, o OpenClaw
// expoe memory_search/memory_recall.
function normalizarNomesDeTool(texto: string): string {
  return texto
    .replace(/orkmind_rules/g, "memory_search")
    .replace(/orkmind_recall/g, "memory_recall")
    .replace(/orkmind_search/g, "memory_search");
}

test("formatRulesBlock reproduz o bloco do Hermes byte a byte", () => {
  const esperado = normalizarNomesDeTool(lerFixture("rules-block.golden.txt"));
  const obtido = formatRulesBlock(REGRAS, mandatoryBudgetChars(4000));
  assert.equal(obtido, esperado);
});

test("truncagem por budget reproduz o bloco do Hermes byte a byte", () => {
  const esperado = normalizarNomesDeTool(
    lerFixture("rules-block-truncado.golden.txt")
  );
  const obtido = formatRulesBlock(REGRAS, mandatoryBudgetChars(60));
  assert.equal(obtido, esperado);
});

test("truncagem avisa quantas regras foram omitidas, nunca descarta em silencio", () => {
  const bloco = formatRulesBlock(REGRAS, mandatoryBudgetChars(60));
  assert.match(bloco, /\(3 regra\(s\) omitida\(s\) por budget\./);
  // A primeira regra sempre entra, mesmo estourando o budget: um bloco de
  // regras vazio seria pior do que um bloco maior que o previsto.
  assert.match(bloco, /## REGRAS MANDATORIAS \(OBEDECER SEMPRE\)/);
});

test("titulo, separadores e marcadores por prioridade", () => {
  const bloco = formatRulesBlock(REGRAS, mandatoryBudgetChars(4000));
  const linhas = bloco.split("\n");
  assert.equal(linhas[0], "");
  assert.equal(linhas[1], RULES_SEPARATOR);
  assert.equal(linhas[2], "## REGRAS MANDATORIAS (OBEDECER SEMPRE)");
  assert.match(bloco, /- \[CRITICA\] \[rule\] Nunca rodar rm -rf/);
  assert.match(bloco, /- \[MANDATORIA\] \[instruction\] Revisar toda migracao/);
  assert.equal(linhas[linhas.length - 2], RULES_SEPARATOR);
});

test("dominios saem em ordem alfabetica e em caixa alta", () => {
  const bloco = formatRulesBlock(REGRAS, mandatoryBudgetChars(4000));
  const dominios = [...bloco.matchAll(/### DOMINIO: (\w+)/g)].map((m) => m[1]);
  assert.deepEqual(dominios, ["COMUNICACAO", "DATABASE", "GERAL", "INFRA"]);
});

test("groupRulesByDomain usa a primeira tag domain e cai em geral", () => {
  const agrupado = groupRulesByDomain([
    { content: "a", tags: { domain: ["infra", "backend"] } },
    { content: "b", tags: {} },
    { content: "c", tags: null },
    { content: "d" },
  ]);
  assert.deepEqual(Object.keys(agrupado).sort(), ["geral", "infra"]);
  assert.equal(agrupado.infra.length, 1);
  assert.equal(agrupado.geral.length, 3);
});

test("regras sem conteudo sao ignoradas e lista vazia devolve string vazia", () => {
  assert.equal(formatRulesBlock([], 4800), "");
  assert.equal(formatRulesBlock([{ content: "   " }], 4800), "");
  assert.equal(formatRulesBlock([{ content: "" }, { content: null as never }], 4800), "");
});

test("mandatoryBudgetChars trunca como o int() do Python", () => {
  // int(4000 * 0.3) * 4 = 1200 * 4
  assert.equal(mandatoryBudgetChars(4000), 4800);
  assert.equal(MANDATORY_BUDGET_RATIO, 0.3);
  assert.equal(CHARS_PER_TOKEN, 4);
  // int(1001 * 0.3) = int(300.3) = 300, nao 301
  assert.equal(mandatoryBudgetChars(1001), 1200);
  // ratio customizado
  assert.equal(mandatoryBudgetChars(1000, 0.5), 2000);
});

test("effectiveTokenBudget usa a config quando o runtime nao informa a janela", () => {
  assert.equal(effectiveTokenBudget(4000), 4000);
  assert.equal(effectiveTokenBudget(4000, undefined), 4000);
  assert.equal(effectiveTokenBudget(4000, null), 4000);
  assert.equal(effectiveTokenBudget(4000, 0), 4000);
  assert.equal(effectiveTokenBudget(4000, Number.NaN), 4000);
});

test("effectiveTokenBudget aplica o clamp sobre a janela real do runtime", () => {
  assert.equal(CTX_BUDGET_SHARE, 0.15);
  // janela pequena manda: floor(10000 * 0.15) = 1500 < 4000
  assert.equal(effectiveTokenBudget(4000, 10000), 1500);
  // janela grande nao aumenta o budget alem da config
  assert.equal(effectiveTokenBudget(4000, 200000), 4000);
});

test("textos constitucionais preservam a paridade com o Hermes", () => {
  assert.match(GUARDRAIL_ANTI_DESTRUICAO, /^\n## Governanca de Memoria\n/);
  assert.match(GUARDRAIL_ANTI_DESTRUICAO, /NAO deve compactar, resumir destrutivamente/);
  // Nomes de tool do OpenClaw, nao do Hermes.
  assert.match(GUARDRAIL_ANTI_DESTRUICAO, /memory_recall/);
  assert.match(GUARDRAIL_ANTI_DESTRUICAO, /memory_search/);
  assert.doesNotMatch(GUARDRAIL_ANTI_DESTRUICAO, /orkmind_/);

  assert.match(FALLBACK_NO_RULES, /^\n## ALERTA: Regras de Governanca Indisponiveis\n/);
  assert.match(FALLBACK_NO_RULES, /NAO prossiga com nenhuma operacao critica/);
  assert.doesNotMatch(FALLBACK_NO_RULES, /orkmind_/);
});
