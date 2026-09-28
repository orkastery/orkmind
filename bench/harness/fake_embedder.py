"""Embedder fake deterministico (lado Python).

Existe para o benchmark medir a MECANICA do pipeline sem custo de rede e
sem nao-determinismo. Ele nao mede qualidade semantica: dois textos com o
mesmo sentido produzem vetores nao relacionados. Isso e proposital e esta
declarado no README do bench.

O algoritmo e identico ao de `fake_embedder.mjs`, e a paridade e verificada
por vetores de referencia gravados em `datasets/constitutional.json`.

Algoritmo:
  1. blocos `sha256(utf8(texto + ":" + i))` com i = 0, 1, 2, ...
  2. cada byte b vira `(b / 255) * 2 - 1`
  3. corta em `dim` valores e normaliza L2
"""

from __future__ import annotations

import hashlib
import math

DEFAULT_DIM = 1024


def fake_embedding(text: str, dim: int = DEFAULT_DIM) -> list[float]:
    """Vetor deterministico de dimensao `dim` para `text`."""
    valores: list[float] = []
    bloco = 0
    while len(valores) < dim:
        digest = hashlib.sha256(f"{text}:{bloco}".encode("utf-8")).digest()
        for byte in digest:
            valores.append((byte / 255.0) * 2.0 - 1.0)
            if len(valores) == dim:
                break
        bloco += 1
    norma = math.sqrt(sum(v * v for v in valores))
    if norma == 0.0:
        return valores
    return [v / norma for v in valores]


class FakeEmbedder:
    """Interface compativel com `orkmind.embeddings.provider.EmbeddingProvider`."""

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        return fake_embedding(text, self._dim)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [fake_embedding(t, self._dim) for t in texts]
