"""Providers de embedding para busca semantica vetorial.

Implementa interface base e provider OpenAI-compatible (OpenRouter).
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Erro ao gerar embedding via API."""


class EmbeddingProvider(ABC):
    """Interface base para provedores de embedding."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Gera vetor de embedding para um texto."""

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Gera vetores de embedding para uma lista de textos."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Dimensao dos vetores gerados."""


class OpenRouterEmbeddingProvider(EmbeddingProvider):
    """Provider OpenAI-compatible para gerar embeddings via OpenRouter.

    Usa httpx para chamadas HTTP ao endpoint /embeddings.
    A API key e lida de variavel de ambiente, nunca hardcoded.

    Com `request_dimensions=True` o tamanho do vetor vai no corpo da chamada.
    Verificado em 2026-09-20 contra o OpenRouter: `qwen/qwen3-embedding-8b`
    (nativo 4096), `openai/text-embedding-3-small` (1536) e
    `perplexity/pplx-embed-v1-0.6b` (1024) aceitam o parametro e devolvem o
    vetor ja normalizado (norma 1.0).
    """

    def __init__(
        self,
        base_url: str = "https://openrouter.ai/api/v1/embeddings",
        api_key_env: str = "OPENROUTER_API_KEY",
        model: str = "perplexity/pplx-embed-v1-0.6b",
        embedding_dim: int = 1024,
        timeout_s: float = 60.0,
        request_dimensions: bool = False,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._dim = embedding_dim
        # Timeout da chamada HTTP. O default alto preserva o comportamento de
        # lote do backfill; o caminho de consulta usa um valor curto, porque
        # ali o embedding esta no meio do turno do agente e travar 60s e pior
        # do que degradar para busca sem vetor.
        self._timeout_s = timeout_s
        # Pede ao endpoint o vetor JA no tamanho desejado. Modelos Matryoshka
        # (Qwen3, text-embedding-3) truncam e RENORMALIZAM do lado deles, o que
        # e o unico jeito de usar um modelo de 4096 dims com o HNSW do pgvector,
        # que para em 2000. Default desligado para nao mudar quem ja usa.
        self._request_dimensions = request_dimensions
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise EmbeddingError(
                f"Variavel de ambiente '{api_key_env}' nao definida. "
                f"Configure a chave de API para gerar embeddings."
            )
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        """Gera embedding para um unico texto."""
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Gera embeddings para uma lista de textos via POST na API.

        Body: {"model": model, "input": texts}
        Resposta esperada: {"data": [{"embedding": [...], "index": N}, ...]}
        """
        if not texts:
            return []

        payload: dict[str, object] = {
            "model": self._model,
            "input": texts,
        }
        if self._request_dimensions:
            payload["dimensions"] = self._dim

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(
                    self._base_url,
                    json=payload,
                    headers=self._headers,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise EmbeddingError(
                f"API retornou erro HTTP {e.response.status_code}: {e.response.text[:200]}"
            ) from e
        except httpx.RequestError as e:
            raise EmbeddingError(f"Erro de rede ao chamar API de embeddings: {e}") from e

        data = response.json()
        if "data" not in data:
            raise EmbeddingError(
                f"Resposta inesperada da API (sem campo 'data'): {str(data)[:200]}"
            )

        # Ordenar por index para garantir alinhamento com a entrada
        items = sorted(data["data"], key=lambda x: x["index"])
        # Converter todos os valores para float (API pode retornar mix int/float)
        return [[float(v) for v in item["embedding"]] for item in items]
