"""Memory Provider nativo sobre PostgreSQL + pgvector.

Memoria de agente para qualquer pessoa, empresa ou stack agentica: tres
camadas - Core, Recall e Wiki - mais um meta-indice navegavel, sem runtime
externo de memoria e sem compactacao destrutiva. Ver `docs/memory-provider/`.

Depende do extra `orkmind[memory-provider]` (asyncpg).
"""

from orkmind.memory_provider.config import ChunkingOptions, MemoryProviderSettings
from orkmind.memory_provider.embeddings import HashingEmbeddingProvider
from orkmind.memory_provider.errors import (
    ClassificationOutOfReachError,
    CoreMemoryMissingError,
    DocumentNotFoundError,
    EmbeddingDimensionError,
    IngestionQueueFullError,
    MemoryProviderError,
    SchemaMismatchError,
    SlugUnavailableError,
    WriteOutOfScopeError,
)
from orkmind.memory_provider.models.schemas import (
    AccessLevel,
    ChatRole,
    ChunkMetadata,
    CoreBlockLabel,
    CoreMemoryBlock,
    DocType,
    DocumentIngestRequest,
    ExecutiveScopeGuard,
    IngestResult,
    IngestStatus,
    MemoryScopeFilter,
    SearchResponse,
    SearchResult,
    SystemContext,
    TurnContext,
    UserContext,
)
from orkmind.memory_provider.provider import NativeMemoryProvider

__all__ = [
    "AccessLevel",
    "ChatRole",
    "ChunkMetadata",
    "ChunkingOptions",
    "ClassificationOutOfReachError",
    "CoreBlockLabel",
    "CoreMemoryBlock",
    "CoreMemoryMissingError",
    "DocType",
    "DocumentIngestRequest",
    "DocumentNotFoundError",
    "EmbeddingDimensionError",
    "ExecutiveScopeGuard",
    "HashingEmbeddingProvider",
    "IngestResult",
    "IngestStatus",
    "IngestionQueueFullError",
    "MemoryProviderError",
    "MemoryProviderSettings",
    "MemoryScopeFilter",
    "NativeMemoryProvider",
    "SchemaMismatchError",
    "SearchResponse",
    "SearchResult",
    "SlugUnavailableError",
    "SystemContext",
    "TurnContext",
    "UserContext",
    "WriteOutOfScopeError",
]
