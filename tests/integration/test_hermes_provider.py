"""Tests for Hermes MemoryProvider (in-process, mock store)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orkmind.core.models import MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.hermes.provider import OrkMindMemoryProvider


def _make_provider() -> OrkMindMemoryProvider:
    store = AsyncMock()
    store.store.return_value = "test-id"
    store.search_by_tags.return_value = [
        MemoryEntry(
            content="mandatory rule",
            collection="rule",
            mandatory=True,
            tags={"skill": ["deploy"]},
            priority="critical",
        )
    ]
    store.search_by_text.return_value = []
    layer = SemanticLayer(store)
    return OrkMindMemoryProvider(layer=layer)


class TestHermesProvider:
    def test_name(self) -> None:
        provider = _make_provider()
        assert provider.name == "orkmind"

    @pytest.mark.asyncio
    async def test_recall(self) -> None:
        provider = _make_provider()
        results = await provider.recall(
            context="deploying the app",
            tags={"skill": ["deploy"]},
        )
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_store_memory(self) -> None:
        provider = _make_provider()
        entry_id = await provider.store_memory(
            content="new learning",
            collection="learning",
            tags={"skill": ["git"]},
        )
        assert entry_id == "test-id"

    @pytest.mark.asyncio
    async def test_get_rules(self) -> None:
        provider = _make_provider()
        rules = await provider.get_rules(tags={"skill": ["deploy"]})
        assert isinstance(rules, list)
        assert len(rules) == 1
        assert rules[0]["mandatory"] is True
