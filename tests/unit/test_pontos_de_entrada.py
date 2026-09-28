"""B1: os pontos de entrada constroem a SemanticLayer com embedder.

Antes deste ciclo, `OrkMindMemoryProvider._ensure_initialized` e o servidor
MCP criavam a `SemanticLayer` SEM embedder. Consequencia: os dois harnesses
Python de producao rodavam a busca em modo degradado, sem vetor, mesmo com
a config de embedding preenchida e paga.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orkmind.core.config import OrkMindConfig
from orkmind.hermes.provider import OrkMindMemoryProvider
from orkmind.store.base import MemoryStore


@pytest.fixture(autouse=True)
def _store_falso(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evita qualquer conexao real de banco nestes testes."""
    store = AsyncMock(spec=MemoryStore)
    monkeypatch.setattr(
        "orkmind.hermes.provider.create_store", lambda cfg: store
    )


class TestProviderHermes:
    @pytest.mark.asyncio
    async def test_constroi_a_layer_com_embedder_quando_ha_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-fake")
        cfg = OrkMindConfig(
            database_url="postgresql://x/y_test",
            embedding_provider="openrouter",
        )
        layer = await OrkMindMemoryProvider(config=cfg)._ensure_initialized()
        assert layer._embedder is not None

    @pytest.mark.asyncio
    async def test_layer_sem_embedder_quando_nao_ha_provider(self) -> None:
        cfg = OrkMindConfig(database_url="postgresql://x/y_test")
        layer = await OrkMindMemoryProvider(config=cfg)._ensure_initialized()
        assert layer._embedder is None

    @pytest.mark.asyncio
    async def test_layer_sem_embedder_quando_falta_a_chave_de_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Modo degradado: sem chave, nada quebra, so nao ha vetor."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        cfg = OrkMindConfig(
            database_url="postgresql://x/y_test",
            embedding_provider="openrouter",
        )
        layer = await OrkMindMemoryProvider(config=cfg)._ensure_initialized()
        assert layer._embedder is None

    @pytest.mark.asyncio
    async def test_gate_do_hibrido_vem_da_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """search_semantic_enabled deixa de ser config morta."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-fake")
        cfg = OrkMindConfig(
            database_url="postgresql://x/y_test",
            embedding_provider="openrouter",
            search_semantic_enabled=True,
        )
        layer = await OrkMindMemoryProvider(config=cfg)._ensure_initialized()
        assert layer._semantic_enabled is True

    @pytest.mark.asyncio
    async def test_gate_do_hibrido_e_desligado_por_padrao(self) -> None:
        cfg = OrkMindConfig(database_url="postgresql://x/y_test")
        layer = await OrkMindMemoryProvider(config=cfg)._ensure_initialized()
        assert layer._semantic_enabled is False
