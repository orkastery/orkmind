"""Factory para criacao de MemoryStore e EmbeddingProvider.

Registry explicito de backends (secao 6 do plano de storage plugavel).
Nenhum import de adapter acontece no topo deste modulo: importar
`orkmind.store.factory` nao pode arrastar `psycopg`, `pgvector` nem
`qdrant_client`. As dependencias continuam obrigatorias no
pyproject.toml (DP-7); o que muda e o custo de import e o acoplamento.
"""

from __future__ import annotations

import logging
from typing import Callable

from orkmind.core.config import OrkMindConfig
from orkmind.store.base import MemoryStore
from orkmind.store.errors import BackendDesconhecidoError, BackendMalConfiguradoError

logger = logging.getLogger(__name__)

BackendBuilder = Callable[[OrkMindConfig], MemoryStore]


def _build_pgvector(config: OrkMindConfig) -> MemoryStore:
    from orkmind.store.postgres_adapter import PostgresAdapter  # import LAZY

    if not config.database_url:
        raise BackendMalConfiguradoError(
            "Backend 'pgvector' exige uma URL de banco. Defina "
            "ORKMIND_DATABASE_URL ou [store].database_url em ~/.orkmind/config.toml."
        )
    return PostgresAdapter(
        database_url=config.database_url,
        embedding_dim=config.embedding_dim,
    )


def _build_memory(config: OrkMindConfig) -> MemoryStore:
    from orkmind.store.memory_adapter import MemoryAdapter  # import LAZY

    logger.warning(
        "Backend 'memory' selecionado: os dados NAO sobrevivem ao fim do "
        "processo. Use apenas em teste e desenvolvimento."
    )
    return MemoryAdapter(embedding_dim=config.embedding_dim)


def _build_qdrant(config: OrkMindConfig) -> MemoryStore:
    try:
        from orkmind.store.qdrant_adapter import QdrantAdapter  # import LAZY
    except ImportError as e:
        raise BackendMalConfiguradoError(
            "Backend 'qdrant' selecionado, mas o cliente nao esta instalado. "
            'Rode: pip install "orkmind[qdrant]"'
        ) from e

    return QdrantAdapter(options=config.store_options, embedding_dim=config.embedding_dim)


STORE_BACKENDS: dict[str, BackendBuilder] = {
    "pgvector": _build_pgvector,
    "postgres": _build_pgvector,   # alias de compatibilidade (DP-9)
    "memory": _build_memory,
    "qdrant": _build_qdrant,
}


def create_store(config: OrkMindConfig) -> MemoryStore:
    """Cria o MemoryStore do backend configurado, ja governado.

    Nao ha fallback silencioso: escolher um backend inexistente falha na
    hora, com a lista dos disponiveis e o lugar onde configurar. E nao ha
    caminho sem governanca: quem passa por aqui recebe um GovernedStore.
    """
    nome = (config.store_backend or "pgvector").strip().lower()
    builder = STORE_BACKENDS.get(nome)
    if builder is None:
        disponiveis = ", ".join(sorted(set(STORE_BACKENDS)))
        raise BackendDesconhecidoError(
            f"Backend de storage '{nome}' desconhecido. Disponiveis: "
            f"{disponiveis}. Configure [store].backend ou ORKMIND_STORE_BACKEND."
        )
    adapter = builder(config)
    # F3.4: governanca sempre por cima, sem excecao.
    from orkmind.store.governed import GovernedStore, montar_profile_store

    return GovernedStore(
        adapter,
        profiles=montar_profile_store(nome, adapter),
        candidate_overfetch=int(config.store_options.get("candidate_overfetch", 500)),
    )


def create_embedder(config: OrkMindConfig, timeout_s: float | None = None):  # type: ignore[no-untyped-def]
    """Cria um EmbeddingProvider a partir da configuracao.

    Retorna None se nao houver provider configurado ou se faltar a chave de API.
    Nunca levanta excecao - retorna None em modo degradado.

    `timeout_s` sobrescreve `config.embedding_timeout_s`. O backfill usa isso
    para manter o timeout longo de lote sem afetar o caminho de consulta.
    """
    from orkmind.embeddings.provider import (
        EmbeddingError,
        OpenRouterEmbeddingProvider,
    )

    if not config.embedding_provider:
        return None

    try:
        return OpenRouterEmbeddingProvider(
            base_url=config.embedding_base_url,
            api_key_env=config.embedding_api_key_env,
            model=config.embedding_model,
            embedding_dim=config.embedding_dim,
            timeout_s=timeout_s if timeout_s is not None else config.embedding_timeout_s,
        )
    except EmbeddingError as e:
        logger.warning("Embedder nao disponivel (modo degradado): %s", e)
        return None
