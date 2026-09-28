// Testes das chaves de configuracao da governanca (A5).
//
// Criterio de pronto do A5: a config viva atual segue valida sem nenhuma
// mudanca, ou seja, todas as chaves novas sao opcionais com default.

import test from "node:test";
import assert from "node:assert/strict";

import { resolveConfig } from "../src/config.js";

const DSN = "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind_openclaw";

test("defaults das chaves de governanca", () => {
  const cfg = resolveConfig({ databaseUrl: DSN });
  assert.equal(cfg.tokenBudget, 4000);
  assert.equal(cfg.mandatoryBudgetRatio, 0.3);
  assert.equal(cfg.failSafe, true);
  assert.equal(cfg.rulesCacheTtlMs, 60000);
  assert.equal(cfg.rulesTimeoutMs, 2000);
  assert.equal(cfg.recallWindowMessages, 3);
});

test("config sem nenhuma chave nova continua valida (compatibilidade)", () => {
  const cfg = resolveConfig({
    databaseUrl: DSN,
    autoRecall: true,
    autoCapture: "smart",
    recallMaxChars: 1200,
  });
  assert.equal(cfg.autoRecall, true);
  assert.equal(cfg.autoCapture, "smart");
  assert.equal(cfg.recallMaxChars, 1200);
  assert.equal(cfg.failSafe, true);
});

test("clamp inferior das chaves de governanca", () => {
  const cfg = resolveConfig({
    databaseUrl: DSN,
    tokenBudget: 1,
    mandatoryBudgetRatio: 0.0001,
    rulesCacheTtlMs: -5000,
    rulesTimeoutMs: 1,
    recallWindowMessages: 0,
  });
  assert.equal(cfg.tokenBudget, 500);
  assert.equal(cfg.mandatoryBudgetRatio, 0.1);
  assert.equal(cfg.rulesCacheTtlMs, 0);
  assert.equal(cfg.rulesTimeoutMs, 250);
  assert.equal(cfg.recallWindowMessages, 1);
});

test("clamp superior das chaves de governanca", () => {
  const cfg = resolveConfig({
    databaseUrl: DSN,
    tokenBudget: 999999,
    mandatoryBudgetRatio: 5,
    rulesCacheTtlMs: 99999999,
    rulesTimeoutMs: 60000,
    recallWindowMessages: 999,
  });
  assert.equal(cfg.tokenBudget, 100000);
  assert.equal(cfg.mandatoryBudgetRatio, 0.9);
  assert.equal(cfg.rulesCacheTtlMs, 3600000);
  assert.equal(cfg.rulesTimeoutMs, 10000);
  assert.equal(cfg.recallWindowMessages, 10);
});

test("valores validos passam intactos", () => {
  const cfg = resolveConfig({
    databaseUrl: DSN,
    tokenBudget: 8000,
    mandatoryBudgetRatio: 0.5,
    failSafe: false,
    rulesCacheTtlMs: 30000,
    rulesTimeoutMs: 1500,
    recallWindowMessages: 5,
  });
  assert.equal(cfg.tokenBudget, 8000);
  assert.equal(cfg.mandatoryBudgetRatio, 0.5);
  assert.equal(cfg.failSafe, false);
  assert.equal(cfg.rulesCacheTtlMs, 30000);
  assert.equal(cfg.rulesTimeoutMs, 1500);
  assert.equal(cfg.recallWindowMessages, 5);
});

test("rulesCacheTtlMs = 0 e valido e significa consultar sempre", () => {
  const cfg = resolveConfig({ databaseUrl: DSN, rulesCacheTtlMs: 0 });
  assert.equal(cfg.rulesCacheTtlMs, 0);
});

test("valores nao numericos caem no limite inferior em vez de virar NaN", () => {
  const cfg = resolveConfig({
    databaseUrl: DSN,
    tokenBudget: "muito" as unknown as number,
    rulesTimeoutMs: Number.NaN,
  });
  assert.equal(cfg.tokenBudget, 500);
  assert.equal(cfg.rulesTimeoutMs, 250);
});

test("databaseUrl ausente e erro explicito, nao silencio", () => {
  const anterior = process.env.ORKMIND_DATABASE_URL;
  delete process.env.ORKMIND_DATABASE_URL;
  try {
    assert.throws(() => resolveConfig({}), /databaseUrl nao configurado/);
  } finally {
    if (anterior !== undefined) process.env.ORKMIND_DATABASE_URL = anterior;
  }
});
