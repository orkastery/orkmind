// Testes das consultas SQL (A4).
//
// O pool fake captura o SQL e os parametros. O objetivo nao e testar o
// PostgreSQL, e sim travar as garantias que o SQL precisa oferecer:
// filtros de seguranca presentes, ausencia de LIMIT nas mandatorias e
// ordenacao que impede corte silencioso.

import test from "node:test";
import assert from "node:assert/strict";

import {
  deleteByQuery,
  getMandatoryRules,
  searchByText,
  searchByVector,
} from "../src/database.js";

interface Chamada {
  sql: string;
  params?: unknown[];
}

function poolEspiao(rows: unknown[] = []) {
  const chamadas: Chamada[] = [];
  const pool = {
    async query(sql: string, params?: unknown[]) {
      chamadas.push({ sql, params });
      return { rows, rowCount: rows.length };
    },
  };
  return { pool: pool as never, chamadas };
}

// Normaliza espacos para os asserts nao dependerem da indentacao do template.
function normalizar(sql: string): string {
  return sql.replace(/\s+/g, " ").trim();
}

test("getMandatoryRules nao usa vetor nem embedder", async () => {
  const { pool, chamadas } = poolEspiao();
  await getMandatoryRules(pool);
  const sql = normalizar(chamadas[0].sql);
  assert.doesNotMatch(sql, /embedding/);
  assert.doesNotMatch(sql, /vector/);
  assert.equal(chamadas[0].params, undefined);
});

test("getMandatoryRules aplica os filtros de seguranca do Hermes", async () => {
  const { pool, chamadas } = poolEspiao();
  await getMandatoryRules(pool);
  const sql = normalizar(chamadas[0].sql);
  assert.match(sql, /WHERE mandatory = true/);
  assert.match(sql, /AND injection_risk = false/);
  assert.match(sql, /AND conflict = false/);
  assert.match(sql, /AND \(expires_at IS NULL OR expires_at > now\(\)\)/);
});

test("getMandatoryRules nao tem LIMIT: corte por budget nunca acontece no SQL", async () => {
  const { pool, chamadas } = poolEspiao();
  await getMandatoryRules(pool);
  assert.doesNotMatch(normalizar(chamadas[0].sql), /LIMIT/);
});

test("getMandatoryRules prioriza criticas e depois ordem de criacao", async () => {
  const { pool, chamadas } = poolEspiao();
  await getMandatoryRules(pool);
  assert.match(
    normalizar(chamadas[0].sql),
    /ORDER BY \(priority = 'critical'\) DESC, created_at ASC/
  );
});

test("searchByVector ordena mandatorias antes da distancia", async () => {
  const { pool, chamadas } = poolEspiao();
  await searchByVector(pool, [0.1, 0.2], 5);
  assert.match(
    normalizar(chamadas[0].sql),
    /ORDER BY mandatory DESC, embedding <=> \$1::vector/
  );
});

test("searchByVector so exclui mandatorias quando pedido", async () => {
  const semExclusao = poolEspiao();
  await searchByVector(semExclusao.pool, [0.1], 5);
  assert.doesNotMatch(normalizar(semExclusao.chamadas[0].sql), /mandatory = false/);

  const comExclusao = poolEspiao();
  await searchByVector(comExclusao.pool, [0.1], 5, 0.3, true);
  assert.match(normalizar(comExclusao.chamadas[0].sql), /AND mandatory = false/);
});

test("searchByText so exclui mandatorias quando pedido", async () => {
  const semExclusao = poolEspiao();
  await searchByText(semExclusao.pool, "deploy", 5);
  assert.doesNotMatch(normalizar(semExclusao.chamadas[0].sql), /mandatory = false/);
  assert.deepEqual(semExclusao.chamadas[0].params, ["deploy", 5]);

  const comExclusao = poolEspiao();
  await searchByText(comExclusao.pool, "deploy", 5, true);
  assert.match(normalizar(comExclusao.chamadas[0].sql), /AND mandatory = false/);
});

test("searchByVector serializa o vetor no formato do pgvector", async () => {
  const { pool, chamadas } = poolEspiao();
  await searchByVector(pool, [0.5, -0.25, 1], 7);
  assert.deepEqual(chamadas[0].params, ["[0.5,-0.25,1]", 7]);
});

test("searchByVector corta pelo minScore no cliente", async () => {
  const { pool } = poolEspiao([
    { id: "a", score: 0.9 },
    { id: "b", score: 0.2 },
  ]);
  const rows = await searchByVector(pool, [0.1], 5, 0.3);
  assert.deepEqual(rows.map((r) => r.id), ["a"]);
});

test("deleteByQuery elege o mais similar, nao a mandatoria que veio primeiro", async () => {
  // searchByVector ordena por `mandatory DESC` no SQL. O pool fake devolve
  // nessa ordem de proposito: sem a reordenacao por score, deleteByQuery
  // elegeria a regra mandatoria como candidata a remocao.
  const { pool } = poolEspiao([
    { id: "regra-mandatoria", score: 0.31, mandatory: true },
    { id: "memoria-alvo", score: 0.97, mandatory: false },
  ]);
  const resultado = await deleteByQuery(pool, [0.1], 5);
  assert.equal(resultado.candidates[0].id, "memoria-alvo");
});
