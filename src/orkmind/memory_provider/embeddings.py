"""Embeddings para o Memory Provider.

Reaproveita o contrato `orkmind.embeddings.provider.EmbeddingProvider` (o
mesmo do restante do OrkMind) e acrescenta o que a ingestao em lote precisa:
fatiamento em batches, concorrencia limitada, retry com backoff e conferencia
de dimensao contra o schema.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import unicodedata

from orkmind.embeddings.provider import (
    EmbeddingError,
    EmbeddingProvider,
    OpenRouterEmbeddingProvider,
)
from orkmind.memory_provider.config import MemoryProviderSettings
from orkmind.memory_provider.errors import EmbeddingDimensionError

logger = logging.getLogger(__name__)

_WORD = re.compile(r"\w+", re.UNICODE)


class HashingEmbeddingProvider(EmbeddingProvider):
    """Embedding deterministico por feature hashing. NAO e semantico.

    Serve para testes, CI e smoke local sem rede nem chave de API: textos que
    compartilham palavras ficam proximos, e so. Em producao use um modelo de
    verdade (`OpenRouterEmbeddingProvider` ou equivalente).
    """

    def __init__(self, embedding_dim: int = 1536) -> None:
        self._dim = embedding_dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        folded = unicodedata.normalize("NFKD", text.casefold())
        folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
        vector = [0.0] * self._dim
        for word in _WORD.findall(folded):
            digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dim
            vector[bucket] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            # Texto sem palavra: vetor unitario fixo, para o cosseno existir.
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]


def build_default_embedder(settings: MemoryProviderSettings) -> EmbeddingProvider:
    return OpenRouterEmbeddingProvider(
        base_url=settings.embedding_base_url,
        api_key_env=settings.embedding_api_key_env,
        model=settings.embedding_model,
        embedding_dim=settings.embedding_dim,
        request_dimensions=settings.embedding_request_dimensions,
    )


def _check_dims(vectors: list[list[float]], expected: int) -> None:
    for vector in vectors:
        if len(vector) != expected:
            raise EmbeddingDimensionError(
                f"embedding com {len(vector)} dimensoes, schema espera {expected}. "
                f"Alinhe `embedding_dim` ao modelo configurado."
            )


async def _embed_batch_with_retry(
    embedder: EmbeddingProvider, batch: list[str], max_attempts: int
) -> list[list[float]]:
    delay = 0.5
    for attempt in range(1, max_attempts + 1):
        try:
            return await embedder.embed_batch(batch)
        except EmbeddingError as exc:
            if attempt == max_attempts:
                raise
            logger.warning(
                "embedding falhou (tentativa %d/%d): %s; nova tentativa em %.1fs",
                attempt,
                max_attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
            delay *= 2
    raise AssertionError("inalcancavel")


async def embed_texts(
    embedder: EmbeddingProvider, texts: list[str], settings: MemoryProviderSettings
) -> list[list[float]]:
    """Embeddings de `texts`, na ordem, em batches concorrentes."""
    if not texts:
        return []
    size = settings.embedding_batch_size
    batches = [texts[i : i + size] for i in range(0, len(texts), size)]
    gate = asyncio.Semaphore(settings.embedding_concurrency)

    async def run(batch: list[str]) -> list[list[float]]:
        async with gate:
            return await _embed_batch_with_retry(embedder, batch, settings.embedding_max_attempts)

    results = await asyncio.gather(*(run(batch) for batch in batches))
    vectors = [vector for batch in results for vector in batch]
    if len(vectors) != len(texts):
        raise EmbeddingError(f"provider devolveu {len(vectors)} vetores para {len(texts)} textos")
    _check_dims(vectors, settings.embedding_dim)
    return vectors


async def embed_query(
    embedder: EmbeddingProvider, query: str, settings: MemoryProviderSettings
) -> list[float]:
    """Embedding da consulta, com timeout curto (estamos no meio do turno)."""
    vector = await asyncio.wait_for(
        embedder.embed(query), timeout=settings.query_embedding_timeout_s
    )
    _check_dims([vector], settings.embedding_dim)
    return vector
