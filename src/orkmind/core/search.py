"""Busca semantica com rerank RRF para OrkMind (D10)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from orkmind.core.models import MemoryEntry

RRF_K = 60
DEFAULT_CANDIDATE_LIMIT = 100


def rrf_score(ranks: list[int], k: int = RRF_K) -> float:
    """Calcula score RRF: sum(1/(k+rank) for rank in ranks)."""
    return sum(1.0 / (k + rank) for rank in ranks)


def rerank_rrf(
    fts_results: list[MemoryEntry],
    vector_results: list[MemoryEntry],
) -> list[MemoryEntry]:
    """Combina dois rankings via RRF, retorna lista unificada ordenada por score."""
    # Mapear entry_id -> ranks (1-indexed)
    scores: dict[str, float] = {}
    entries: dict[str, MemoryEntry] = {}

    for rank, entry in enumerate(fts_results, start=1):
        entries[entry.id] = entry
        scores[entry.id] = scores.get(entry.id, 0.0) + 1.0 / (RRF_K + rank)

    for rank, entry in enumerate(vector_results, start=1):
        entries[entry.id] = entry
        scores[entry.id] = scores.get(entry.id, 0.0) + 1.0 / (RRF_K + rank)

    # Ordenar por score decrescente
    sorted_ids = sorted(scores.keys(), key=lambda eid: scores[eid], reverse=True)
    return [entries[eid] for eid in sorted_ids]


class RerankProvider(ABC):
    """Interface para providers de rerank (preparada para LLM futuro)."""

    @abstractmethod
    async def rerank(self, query: str, candidates: list[MemoryEntry]) -> list[MemoryEntry]:
        """Reordena candidatos por relevancia."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Nome do provider."""


class RRFReranker(RerankProvider):
    """Implementacao deterministica de rerank via RRF (sem LLM)."""

    async def rerank(self, query: str, candidates: list[MemoryEntry]) -> list[MemoryEntry]:
        # RRFReranker usa a lista de candidatos como esta (ja combinada)
        return candidates

    @property
    def provider_name(self) -> str:
        return "rrf"
