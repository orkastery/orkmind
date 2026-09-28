"""Testes para busca semantica com rerank RRF (D10)."""

from __future__ import annotations

from orkmind.core.models import MemoryEntry
from orkmind.core.search import (
    RRF_K,
    RRFReranker,
    RerankProvider,
    rerank_rrf,
    rrf_score,
)


class TestRRFScore:
    def test_rrf_score_single_rank(self) -> None:
        """Score correto para rank unico."""
        score = rrf_score([1])
        expected = 1.0 / (RRF_K + 1)
        assert abs(score - expected) < 1e-10

    def test_rrf_score_multiple_ranks(self) -> None:
        """Score correto para multiplos ranks."""
        score = rrf_score([1, 2])
        expected = 1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 2)
        assert abs(score - expected) < 1e-10


class TestRerankRRF:
    def test_combines_results(self) -> None:
        """Resultados combinados e ordenados."""
        e1 = MemoryEntry(content="a", collection="fact")
        e2 = MemoryEntry(content="b", collection="fact")
        e3 = MemoryEntry(content="c", collection="fact")
        fts = [e1, e2]
        vec = [e2, e3]
        result = rerank_rrf(fts, vec)
        # e2 aparece em ambas as listas, deve ter score maior
        assert result[0].id == e2.id

    def test_deduplicates(self) -> None:
        """Entries duplicadas entre FTS e vector sao unificadas."""
        e1 = MemoryEntry(content="comum", collection="fact")
        result = rerank_rrf([e1], [e1])
        assert len(result) == 1
        assert result[0].id == e1.id

    def test_empty_inputs(self) -> None:
        """Listas vazias retornam lista vazia."""
        result = rerank_rrf([], [])
        assert result == []

    def test_single_source_fts(self) -> None:
        """So FTS funciona."""
        e1 = MemoryEntry(content="fts only", collection="fact")
        result = rerank_rrf([e1], [])
        assert len(result) == 1
        assert result[0].id == e1.id

    def test_single_source_vector(self) -> None:
        """So vector funciona."""
        e1 = MemoryEntry(content="vec only", collection="fact")
        result = rerank_rrf([], [e1])
        assert len(result) == 1
        assert result[0].id == e1.id


class TestRRFReranker:
    def test_implements_interface(self) -> None:
        """RRFReranker e instancia de RerankProvider."""
        reranker = RRFReranker()
        assert isinstance(reranker, RerankProvider)
        assert reranker.provider_name == "rrf"
