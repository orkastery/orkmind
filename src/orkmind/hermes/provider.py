"""Hermes MemoryProvider integration for OrkMind.

This module provides a MemoryProvider that Hermes can use to query
OrkMind as its memory backend. It wraps the OrkMind SemanticLayer
to provide context-aware memory retrieval with mandatory rule injection.

Usage in Hermes config:
    memory_provider: orkmind
    memory_provider_config:
        database_url: postgresql://orkmind:senha-de-teste@localhost:5432/orkmind
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from orkmind.core.config import OrkMindConfig, load_config
from orkmind.core.models import MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer

# G1: o hash D-MA10 e a primitiva central da auditoria de regras e vive
# em orkmind.guardrails. Reexportado aqui por compatibilidade com os
# consumidores existentes deste modulo.
from orkmind.guardrails.rules import compute_rules_hash  # noqa: F401
from orkmind.store.factory import create_embedder, create_store

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from orkmind.federation import FederatedMemory


class OrkMindMemoryProvider:
    """Hermes-compatible memory provider backed by OrkMind.

    Implements the essential interface that Hermes expects from a
    memory provider: recall, store, and search.
    """

    def __init__(
        self,
        config: Optional[OrkMindConfig] = None,
        layer: Optional[SemanticLayer] = None,
        federation: Optional["FederatedMemory"] = None,
    ) -> None:
        self._config = config
        self._layer = layer
        self._federation = federation
        self._initialized = False

    async def _ensure_initialized(self) -> SemanticLayer:
        if self._layer is not None:
            return self._layer
        cfg = self._config or load_config()
        store = create_store(cfg)
        await store.initialize()
        # create_embedder devolve None em modo degradado (sem provider
        # configurado ou sem chave de API), entao passar sempre e seguro:
        # onde o embedder nao existir, nada muda.
        self._layer = SemanticLayer(
            store,
            token_budget=cfg.token_budget,
            embedder=create_embedder(cfg),
            semantic_enabled=cfg.search_semantic_enabled,
            rerank_strategy=cfg.search_rerank_strategy,
        )
        self._initialized = True
        return self._layer

    @property
    def name(self) -> str:
        return "orkmind"

    async def recall(
        self,
        context: str = "",
        tags: Optional[dict[str, list[str]]] = None,
        files: Optional[list[str]] = None,
        token_budget: Optional[int] = None,
        requester_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Recall memories relevant to the current context.

        This is the primary interface for Hermes to retrieve memories.
        It uses context detection and mandatory rule injection.
        Com requester_id, aplica o filtro de acesso de leitura (F2).
        """
        layer = await self._ensure_initialized()
        entries = await layer.query_for_context(
            tags=tags,
            conversation=context,
            files=files,
            token_budget=token_budget,
            requester_id=requester_id,
        )
        return [self._format_entry(e) for e in entries]

    async def store_memory(
        self,
        content: str,
        collection: str = "fact",
        tags: Optional[dict[str, list[str]]] = None,
        priority: str = "medium",
        mandatory: bool = False,
        source: str = "agent",
        metadata: Optional[dict[str, Any]] = None,
        requester_id: Optional[str] = None,
        visibility: str = "private",
        url: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> str:
        """Store a new memory from Hermes.

        requester_id, quando informado, vira o author_id da entry (F2).
        """
        layer = await self._ensure_initialized()
        entry = MemoryEntry(
            content=content,
            collection=collection,  # type: ignore[arg-type]
            tags=tags or {},
            priority=priority,  # type: ignore[arg-type]
            mandatory=mandatory,
            source=source,  # type: ignore[arg-type]
            metadata=metadata or {},
            author_id=requester_id,
            visibility=visibility,  # type: ignore[arg-type]
            url=url,
            parent_id=parent_id,
        )
        entry_id, _ = await layer.add_memory(entry)
        return entry_id

    async def federated_recall(
        self,
        *,
        tags: dict[str, list[str]],
        requester_id: str,
        collection: Optional[str] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Recall multi-project explicitamente identificado como Hermes."""
        if self._federation is None:
            raise RuntimeError("federated memory is not configured")
        result = await self._federation.recall(
            tags=tags,
            requester_id=requester_id,
            caller="hermes",
            collection=collection,
            limit=limit,
        )
        return result.as_dict()

    async def search(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Search memories by text."""
        layer = await self._ensure_initialized()
        entries = await layer.store.search_by_text(
            query=query,
            collection=collection,
            limit=limit,
            requester_id=requester_id,
        )
        return [self._format_entry(e) for e in entries]

    async def get_rules(
        self,
        tags: Optional[dict[str, list[str]]] = None,
        requester_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get mandatory rules for injection into Hermes context.

        Entries com injection_risk ou conflict sao excluidas: regras sob
        suspeita nunca entram no contexto sem revisao humana (D-MA1).
        """
        layer = await self._ensure_initialized()
        entries = await layer.get_mandatory_rules(tags=tags, requester_id=requester_id)
        safe = [e for e in entries if not e.injection_risk and not e.conflict]
        return [self._format_entry(e) for e in safe]

    async def get_rules_hash(
        self,
        tags: Optional[dict[str, list[str]]] = None,
        requester_id: Optional[str] = None,
    ) -> str:
        """Hash SHA-256 do conjunto de regras mandatorias ativas (D-MA10).

        Serve para auditoria e para invalidacao de cache no plugin:
        se o hash nao mudou, as regras em cache continuam validas.
        Retorna string vazia quando nao ha regras.
        """
        layer = await self._ensure_initialized()
        entries = await layer.get_mandatory_rules(tags=tags, requester_id=requester_id)
        safe = [e for e in entries if not e.injection_risk and not e.conflict]
        return compute_rules_hash(e.content for e in safe)

    async def submit_handoff(
        self,
        payload: dict[str, Any],
        session_id: str,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        refacao: int = 0,
        requester_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Valida e armazena um pacote de handoff (G3, D-MA12).

        Encapsula orkmind.guardrails.handoff.submeter_handoff: o runtime
        decide quando rotacionar; o OrkMind so valida e guarda.
        """
        from orkmind.guardrails.handoff import submeter_handoff

        layer = await self._ensure_initialized()
        return await submeter_handoff(
            layer,
            session_id=session_id,
            payload=payload,
            requester_id=requester_id,
            origin=origin,
            destination=destination,
            refacao=refacao,
        )

    async def get_handoff_for_session(
        self,
        session_id: str,
        requester_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Handoff destinado a sessao, para injecao no primeiro turno (G3)."""
        from orkmind.guardrails.handoff import handoff_para_sessao

        layer = await self._ensure_initialized()
        return await handoff_para_sessao(
            layer, session_id, requester_id=requester_id
        )

    async def get_stats(self) -> dict[str, int]:
        """Contagem de entries por colecao (D-MA3).

        Alimenta o manifesto de disponibilidade injetado no contexto:
        o agente sabe o que existe na base sem precisar carregar tudo.
        """
        layer = await self._ensure_initialized()
        collections = await layer.store.list_collections()
        stats: dict[str, int] = {}
        for collection in collections:
            stats[collection] = await layer.store.count(collection=collection)
        return stats

    async def close(self) -> None:
        """Release resources."""
        if self._layer and self._initialized:
            await self._layer.store.close()

    def _format_entry(self, entry: MemoryEntry) -> dict[str, Any]:
        return {
            "id": entry.id,
            "content": entry.content,
            "collection": entry.collection,
            "tags": entry.tags,
            "priority": entry.priority,
            "mandatory": entry.mandatory,
            "scope": entry.scope,
            "source": entry.source,
            "injection_risk": entry.injection_risk,
            "conflict": entry.conflict,
            "visibility": entry.visibility,
            "author_id": entry.author_id,
            "parent_id": entry.parent_id,
            "url": entry.url,
        }
