"""Abstract MemoryStore contract for OrkMind."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from orkmind.core.models import MemoryEntry, Source
from orkmind.store.capabilities import StoreCapabilities
from orkmind.store.errors import CapacidadeIndisponivelError


class MemoryStore(ABC):
    """Abstract base class for OrkMind storage backends."""

    # --- Capacidades declaradas (F3.2) ---

    @property
    def capabilities(self) -> StoreCapabilities:
        """O que este backend sabe fazer. Default otimista.

        Propriedade sincrona, sem I/O, constante por instancia. Toda
        degradacao de um backend passa por aqui: quem nao declara se
        comporta como o comportamento historico (R0.3).
        """
        return StoreCapabilities()

    # --- CRUD ---

    @abstractmethod
    async def store(self, entry: MemoryEntry) -> str:
        """Store a memory entry. Returns the entry id."""

    @abstractmethod
    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve a memory entry by id."""

    @abstractmethod
    async def find_by_content_hash(
        self,
        content_hash: str,
        collection: Optional[str] = None,
    ) -> Optional[MemoryEntry]:
        """Busca a entry existente com o content_hash informado.

        Base da idempotencia da spool: o drainer consulta por hash antes
        de gravar e, se ja houver entry, reaproveita o entry_id real em
        vez de inserir uma duplicata. Quando `collection` e informada, a
        busca fica restrita aquela colecao (par que o indice unico
        parcial protege). Retorna a entry mais antiga, ou None.
        """

    @abstractmethod
    async def update(
        self,
        entry_id: str,
        entry: MemoryEntry,
        requester_id: Optional[str] = None,
    ) -> bool:
        """Update an existing entry. Returns True if found and updated.

        Com requester_id, aplica o filtro de acesso de escrita (F2).
        """

    @abstractmethod
    async def delete(
        self,
        entry_id: str,
        source: Source = "human",
        requester_id: Optional[str] = None,
    ) -> bool:
        """Delete an entry by id. Returns True if found and deleted.

        Com requester_id, aplica o filtro de acesso de escrita (F2).
        """

    # --- Search ---

    @abstractmethod
    async def search_by_tags(
        self,
        tags: dict[str, list[str]],
        collection: Optional[str] = None,
        mandatory_only: bool = False,
        limit: int = 50,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Search entries by exact tag match.

        Entries with mandatory=True that match the tags are ALWAYS included.
        Com requester_id, aplica o filtro de acesso de leitura (F2).
        """

    @abstractmethod
    async def search_by_text(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Full-text search on entry content."""

    @abstractmethod
    async def search_semantic(
        self,
        embedding: list[float],
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Semantic search using vector similarity (pgvector)."""

    async def search_semantic_text(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Busca semantica a partir do TEXTO, para backends que nao aceitam
        vetor externo (capabilities.accepts_external_vectors=False) ou que
        so expoem busca por texto (capabilities.native_text_semantic=True).

        Implementacao padrao: levanta CapacidadeIndisponivelError. Nunca
        retorna [] em silencio (R0.3).
        """
        raise CapacidadeIndisponivelError(
            f"Backend '{self.capabilities.backend}' nao implementa "
            f"search_semantic_text. Use search_semantic com vetor externo."
        )

    # --- Collections ---

    @abstractmethod
    async def list_collections(self) -> list[str]:
        """List all collections that have at least one entry."""

    @abstractmethod
    async def count(self, collection: Optional[str] = None) -> int:
        """Count entries, optionally filtered by collection."""

    # --- Hierarquia (F1) ---

    async def get_children(self, parent_id: str) -> list[MemoryEntry]:
        """Entries filhas de uma entry (campo parent_id).

        Implementacao padrao vazia para backends que ainda nao suportam
        hierarquia; adaptadores devem sobrescrever.
        """
        return []

    # --- Backfill de embeddings (F3.2 declara, F3.6 implementa) ---

    async def list_entries_without_embedding(
        self,
        collection: Optional[str] = None,
        limit: int = 1000,
    ) -> list[MemoryEntry]:
        """Entries persistidas sem vetor, para o backfill.

        Ordem estavel e determinista (created_at ASC, id ASC). Nao aplica
        ACL nem filtro de injection_risk: e operacao de manutencao do
        operador, nao consulta de agente. Entries expiradas sao excluidas.

        Implementacao padrao: CapacidadeIndisponivelError (capabilities.backfill=False).
        """
        raise CapacidadeIndisponivelError(
            f"Backend '{self.capabilities.backend}' nao implementa "
            f"list_entries_without_embedding (capabilities.backfill=False)."
        )

    async def set_embedding(self, entry_id: str, embedding: list[float]) -> bool:
        """Grava SOMENTE o vetor de uma entry existente. True se encontrou.

        NAO arquiva versao, NAO incrementa version, NAO altera updated_at,
        NAO passa por protecao D2 nem por ACL: backfill nao e edicao de
        conteudo.

        Levanta ValueError se len(embedding) != embedding_dim do backend.
        """
        raise CapacidadeIndisponivelError(
            f"Backend '{self.capabilities.backend}' nao implementa "
            f"set_embedding (capabilities.backfill=False)."
        )

    # --- Maintenance ---

    @abstractmethod
    async def get_history(self, entry_id: str) -> list[MemoryEntry]:
        """Retorna todas as versoes anteriores de uma entry."""

    @abstractmethod
    async def gc_versions(self, max_age_days: int = 365) -> int:
        """Apaga versoes antigas sem acesso ha mais de max_age_days."""

    @abstractmethod
    async def garbage_collect(self) -> int:
        """Remove expired entries. Returns number of entries removed."""

    # --- Snapshots ---

    @abstractmethod
    async def snapshot_commit(self, label: str, message: str = "") -> str:
        """Salva snapshot global do estado atual. Retorna snapshot_id."""

    @abstractmethod
    async def snapshot_log(self, limit: int = 20) -> list[dict]:
        """Lista snapshots (id, label, message, entry_count, created_at)."""

    @abstractmethod
    async def snapshot_show(self, snapshot_id: str) -> dict:
        """Detalhes de um snapshot + lista de entry_ids."""

    @abstractmethod
    async def snapshot_diff(self, snap_a: str, snap_b: str) -> dict:
        """Diff entre dois snapshots: added, removed, modified."""

    @abstractmethod
    async def snapshot_restore(self, snapshot_id: str) -> int:
        """Restaura estado de um snapshot. Cria auto-backup antes. Retorna count."""

    # --- Init / Teardown ---

    @abstractmethod
    async def initialize(self) -> None:
        """Prepara o backend (DDL, colecoes, indices, namespaces)."""

    @abstractmethod
    async def close(self) -> None:
        """Close connections and release resources."""
