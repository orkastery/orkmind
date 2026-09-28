"""Testes do modulo de embeddings (provider, integracao e modo degradado).

Todos os testes usam mocks - nenhuma chamada real a API externa.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

from orkmind.core.models import MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.embeddings.provider import (
    EmbeddingError,
    EmbeddingProvider,
    OpenRouterEmbeddingProvider,
)
from orkmind.store.base import MemoryStore

EMBEDDING_DIM = 1024


# --- Fixtures e helpers ---


class MockEmbedder(EmbeddingProvider):
    """Embedder de teste que retorna vetor deterministico de dim 1024."""

    def __init__(self, dim: int = EMBEDDING_DIM, should_fail: bool = False) -> None:
        self._dim = dim
        self._should_fail = should_fail
        self.call_count = 0

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        if self._should_fail:
            raise EmbeddingError("Falha simulada no embedder")
        self.call_count += 1
        return [0.1] * self._dim

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self._should_fail:
            raise EmbeddingError("Falha simulada no embedder batch")
        self.call_count += 1
        return [[0.1] * self._dim for _ in texts]


def _make_mock_store(entries: list[MemoryEntry] | None = None) -> MemoryStore:
    """Cria um mock store para testes de embedding."""
    store = AsyncMock(spec=MemoryStore)
    all_entries = entries or []

    async def mock_search_by_tags(
        tags: dict[str, list[str]],
        collection: Optional[str] = None,
        mandatory_only: bool = False,
        limit: int = 50,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        results = all_entries
        if collection:
            results = [e for e in results if e.collection == collection]
        if mandatory_only:
            results = [e for e in results if e.mandatory]
        return results[:limit]

    async def mock_store(entry: MemoryEntry) -> str:
        return entry.id

    store.search_by_tags = mock_search_by_tags
    store.store = mock_store
    return store


# --- Testes do provider ---


class TestEmbeddingProvider:
    def test_mock_embedder_retorna_dim_correta(self) -> None:
        embedder = MockEmbedder()
        assert embedder.dim == EMBEDDING_DIM

    @pytest.mark.asyncio
    async def test_embed_retorna_vetor_1024(self) -> None:
        embedder = MockEmbedder()
        result = await embedder.embed("texto de teste")
        assert len(result) == EMBEDDING_DIM
        assert all(isinstance(v, float) for v in result)

    @pytest.mark.asyncio
    async def test_embed_batch_retorna_lista_de_vetores(self) -> None:
        embedder = MockEmbedder()
        texts = ["texto 1", "texto 2", "texto 3"]
        results = await embedder.embed_batch(texts)
        assert len(results) == 3
        for vec in results:
            assert len(vec) == EMBEDDING_DIM

    @pytest.mark.asyncio
    async def test_embed_batch_vazio_retorna_lista_vazia(self) -> None:
        embedder = MockEmbedder()
        results = await embedder.embed_batch([])
        assert results == []


class TestOpenRouterProvider:
    def test_sem_api_key_levanta_erro(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(EmbeddingError, match="nao definida"):
            OpenRouterEmbeddingProvider()

    def test_com_api_key_cria_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-fake")
        provider = OpenRouterEmbeddingProvider()
        assert provider.dim == EMBEDDING_DIM


# --- Testes de integracao com SemanticLayer ---


class TestSemanticLayerEmbedding:
    @pytest.mark.asyncio
    async def test_add_memory_gera_embedding_quando_embedder_presente(self) -> None:
        """Entry deve ter embedding preenchido apos add_memory."""
        embedder = MockEmbedder()
        store = _make_mock_store()
        layer = SemanticLayer(store, embedder=embedder)

        entry = MemoryEntry(content="fato importante", collection="fact")
        entry_id, warnings = await layer.add_memory(entry)

        assert entry.embedding is not None
        assert len(entry.embedding) == EMBEDDING_DIM
        assert embedder.call_count == 1

    @pytest.mark.asyncio
    async def test_add_memory_sem_embedder_mantem_none(self) -> None:
        """Sem embedder, embedding permanece None."""
        store = _make_mock_store()
        layer = SemanticLayer(store)  # sem embedder

        entry = MemoryEntry(content="fato sem embedding", collection="fact")
        entry_id, warnings = await layer.add_memory(entry)

        assert entry.embedding is None

    @pytest.mark.asyncio
    async def test_add_memory_nao_sobrescreve_embedding_existente(self) -> None:
        """Se entry ja tem embedding, nao deve sobrescrever."""
        embedder = MockEmbedder()
        store = _make_mock_store()
        layer = SemanticLayer(store, embedder=embedder)

        existing_emb = [0.5] * EMBEDDING_DIM
        entry = MemoryEntry(
            content="fato com embedding",
            collection="fact",
            embedding=existing_emb,
        )
        await layer.add_memory(entry)

        assert entry.embedding == existing_emb
        assert embedder.call_count == 0

    @pytest.mark.asyncio
    async def test_add_memory_degradado_embedder_falha(self) -> None:
        """Quando embedder falha, entry e salva com embedding=None."""
        embedder = MockEmbedder(should_fail=True)
        store = _make_mock_store()
        layer = SemanticLayer(store, embedder=embedder)

        entry = MemoryEntry(content="fato que vai falhar", collection="fact")
        entry_id, warnings = await layer.add_memory(entry)

        # Entry foi salva (retornou id)
        assert entry_id == entry.id
        # Embedding permanece None (modo degradado)
        assert entry.embedding is None

    @pytest.mark.asyncio
    async def test_search_semantic_intent_gera_embedding_da_query(self) -> None:
        """Quando embedder disponivel, gera embedding da query automaticamente."""
        embedder = MockEmbedder()
        e1 = MemoryEntry(content="resultado fts", collection="fact")
        store = _make_mock_store()
        store.search_by_text = AsyncMock(return_value=[e1])
        store.search_semantic = AsyncMock(return_value=[])

        layer = SemanticLayer(store, embedder=embedder)
        await layer.search_semantic_intent(query="busca teste")

        # search_semantic deve ter sido chamado (embedding gerado)
        store.search_semantic.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_semantic_intent_sem_embedder_usa_fts_only(self) -> None:
        """Sem embedder, search_semantic nao e chamado."""
        e1 = MemoryEntry(content="resultado fts", collection="fact")
        store = _make_mock_store()
        store.search_by_text = AsyncMock(return_value=[e1])
        store.search_semantic = AsyncMock(return_value=[])

        layer = SemanticLayer(store)  # sem embedder
        results = await layer.search_semantic_intent(query="busca teste")

        assert len(results) == 1
        store.search_semantic.assert_not_awaited()


# --- Testes de configuracao ---


class TestEmbedderConfig:
    def test_config_default_embedding_dim_1024(self) -> None:
        from orkmind.core.config import OrkMindConfig
        cfg = OrkMindConfig()
        assert cfg.embedding_dim == 1024

    def test_create_embedder_sem_provider_retorna_none(self) -> None:
        from orkmind.core.config import OrkMindConfig
        from orkmind.store.factory import create_embedder
        cfg = OrkMindConfig(embedding_provider="")
        assert create_embedder(cfg) is None

    def test_create_embedder_sem_api_key_retorna_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orkmind.core.config import OrkMindConfig
        from orkmind.store.factory import create_embedder
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        cfg = OrkMindConfig(embedding_provider="openrouter")
        result = create_embedder(cfg)
        assert result is None


# --- B2: timeout curto no caminho de consulta (mata o R10) ---


class TestEmbeddingTimeout:
    """O embedding roda no meio do turno do agente.

    Um timeout de 60s ali significa um turno travado por um minuto. O
    caminho de consulta usa 8s por padrao e degrada para busca sem vetor;
    o backfill, que roda em lote fora do turno, mantem os 60s.
    """

    def test_default_do_provider_preserva_o_lote(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-fake")
        provider = OpenRouterEmbeddingProvider()
        assert provider._timeout_s == 60.0

    def test_provider_aceita_timeout_customizado(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-fake")
        provider = OpenRouterEmbeddingProvider(timeout_s=8.0)
        assert provider._timeout_s == 8.0

    def test_create_embedder_usa_o_timeout_da_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orkmind.core.config import OrkMindConfig
        from orkmind.store.factory import create_embedder

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-fake")
        cfg = OrkMindConfig(embedding_provider="openrouter")
        embedder = create_embedder(cfg)
        assert embedder is not None
        assert embedder._timeout_s == 8.0

    def test_create_embedder_aceita_override_do_backfill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orkmind.core.config import OrkMindConfig
        from orkmind.store.factory import create_embedder

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-fake")
        cfg = OrkMindConfig(embedding_provider="openrouter")
        embedder = create_embedder(cfg, timeout_s=60.0)
        assert embedder is not None
        assert embedder._timeout_s == 60.0

    @pytest.mark.asyncio
    async def test_timeout_de_rede_vira_EmbeddingError(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Transporte que estoura o tempo produz erro tipado, nao trava."""
        import httpx

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-fake")
        provider = OpenRouterEmbeddingProvider(timeout_s=0.05)

        async def _post_lento(*args: object, **kwargs: object) -> object:
            raise httpx.ReadTimeout("tempo esgotado")

        monkeypatch.setattr(httpx.AsyncClient, "post", _post_lento)
        with pytest.raises(EmbeddingError, match="Erro de rede"):
            await provider.embed("qualquer texto")

    @pytest.mark.asyncio
    async def test_semantic_layer_degrada_quando_o_embedder_estoura(self) -> None:
        """`_generate_embedding` engole a falha e devolve None (modo degradado)."""

        class EmbedderQueEstoura(EmbeddingProvider):
            @property
            def dim(self) -> int:
                return EMBEDDING_DIM

            async def embed(self, text: str) -> list[float]:
                raise EmbeddingError("timeout")

            async def embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise EmbeddingError("timeout")

        layer = SemanticLayer(_make_mock_store(), embedder=EmbedderQueEstoura())
        assert await layer._generate_embedding("texto") is None


class TestBackfillPeloContrato:
    """F3.6: o backfill nao fala mais SQL, fala contrato."""

    def test_modulo_nao_usa_mais_get_conn(self) -> None:
        """Criterio de aceite 1 de 7.6, verificado mecanicamente."""
        import inspect

        from orkmind.embeddings import backfill as modulo

        fonte = inspect.getsource(modulo)
        assert "_get_conn" not in fonte
        assert "FROM memories" not in fonte
        assert "UPDATE memories" not in fonte
        assert "type: ignore[attr-defined]" not in fonte

    def _store_falso(self, capabilities=None):
        from orkmind.store.capabilities import StoreCapabilities

        store = AsyncMock(spec=MemoryStore)
        cap = capabilities or StoreCapabilities(backend="falso")
        type(store).capabilities = property(lambda self: cap)
        store.list_entries_without_embedding.return_value = []
        return store

    @pytest.mark.asyncio
    async def test_usa_o_contrato_para_listar_e_gravar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orkmind.embeddings import backfill as modulo

        store = self._store_falso()
        pendentes = [
            MemoryEntry(content="um", collection="fact"),
            MemoryEntry(content="dois", collection="fact"),
        ]
        store.list_entries_without_embedding.return_value = pendentes
        store.set_embedding.return_value = True

        class EmbedderFalso:
            async def embed_batch(self, texts: list[str]) -> list[list[float]]:
                return [[0.1] * 4 for _ in texts]

        monkeypatch.setattr(modulo, "create_store", lambda cfg: store)
        monkeypatch.setattr(modulo, "create_embedder", lambda cfg, timeout_s=None: EmbedderFalso())
        monkeypatch.setattr(modulo, "load_config", lambda: __import__(
            "orkmind.core.config", fromlist=["OrkMindConfig"]
        ).OrkMindConfig())

        atualizadas = await modulo.backfill(limit=10)
        assert atualizadas == 2
        store.list_entries_without_embedding.assert_awaited_once_with(
            collection=None, limit=10
        )
        assert store.set_embedding.await_count == 2
        store.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aborta_quando_o_backend_nao_suporta_backfill(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R0.3: capacidade ausente vira erro nomeado, nao zero mudo."""
        import logging

        from orkmind.embeddings import backfill as modulo
        from orkmind.store.capabilities import StoreCapabilities

        store = self._store_falso(StoreCapabilities(backend="sem-backfill", backfill=False))
        monkeypatch.setattr(modulo, "create_store", lambda cfg: store)
        monkeypatch.setattr(modulo, "load_config", lambda: __import__(
            "orkmind.core.config", fromlist=["OrkMindConfig"]
        ).OrkMindConfig())

        with caplog.at_level(logging.ERROR):
            assert await modulo.backfill() == 0
        assert any("sem-backfill" in r.getMessage() for r in caplog.records)
        store.list_entries_without_embedding.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_avisa_quando_a_entry_sumiu_no_meio(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orkmind.embeddings import backfill as modulo

        store = self._store_falso()
        store.list_entries_without_embedding.return_value = [
            MemoryEntry(content="um", collection="fact")
        ]
        store.set_embedding.return_value = False

        class EmbedderFalso:
            async def embed_batch(self, texts: list[str]) -> list[list[float]]:
                return [[0.1] * 4 for _ in texts]

        monkeypatch.setattr(modulo, "create_store", lambda cfg: store)
        monkeypatch.setattr(modulo, "create_embedder", lambda cfg, timeout_s=None: EmbedderFalso())
        monkeypatch.setattr(modulo, "load_config", lambda: __import__(
            "orkmind.core.config", fromlist=["OrkMindConfig"]
        ).OrkMindConfig())

        assert await modulo.backfill() == 0
