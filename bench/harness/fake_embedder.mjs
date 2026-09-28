// Embedder fake deterministico (lado Node).
//
// Algoritmo IDENTICO ao de `fake_embedder.py`. A paridade entre as duas
// linguagens e verificada contra os vetores de referencia gravados em
// `datasets/constitutional.json`: se as duas implementacoes divergirem, as
// metricas dos dois runners deixam de ser comparaveis.
//
// Ele nao mede qualidade semantica, mede a mecanica do pipeline.

import { createHash } from "node:crypto";

export const DEFAULT_DIM = 1024;

export function fakeEmbedding(text, dim = DEFAULT_DIM) {
  const valores = [];
  let bloco = 0;
  while (valores.length < dim) {
    const digest = createHash("sha256").update(`${text}:${bloco}`, "utf8").digest();
    for (const byte of digest) {
      valores.push((byte / 255) * 2 - 1);
      if (valores.length === dim) break;
    }
    bloco += 1;
  }
  let soma = 0;
  for (const v of valores) soma += v * v;
  const norma = Math.sqrt(soma);
  if (norma === 0) return valores;
  return valores.map((v) => v / norma);
}

/** Cliente compativel com a interface `EmbeddingClient` do plugin. */
export function criarEmbedderFake(dim = DEFAULT_DIM) {
  return { async embed(texto) { return fakeEmbedding(texto, dim); } };
}
