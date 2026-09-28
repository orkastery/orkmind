"""Tests for MCP server tool dispatch (in-process, no Postgres needed)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orkmind.core.semantic_layer import SemanticLayer
from orkmind.mcp.tools import (
    orkmind_add,
    orkmind_delete,
    orkmind_list,
    orkmind_stats,
)


def _mock_layer() -> SemanticLayer:
    store = AsyncMock()
    store.store.return_value = "test-id"
    store.search_by_tags.return_value = []
    store.list_collections.return_value = ["rule", "fact"]
    store.count.return_value = 0
    store.delete.return_value = True
    layer = SemanticLayer(store)
    return layer


class TestMCPTools:
    @pytest.mark.asyncio
    async def test_orkmind_add(self) -> None:
        layer = _mock_layer()
        result = await orkmind_add(
            layer,
            content="test rule",
            collection="rule",
            tags={"skill": ["git"]},
            mandatory=True,
        )
        assert "id" in result
        assert isinstance(result["warnings"], list)

    @pytest.mark.asyncio
    async def test_orkmind_delete(self) -> None:
        layer = _mock_layer()
        result = await orkmind_delete(layer, entry_id="some-id")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_orkmind_stats(self) -> None:
        layer = _mock_layer()
        result = await orkmind_stats(layer)
        assert "total_entries" in result
        assert "collections" in result

    @pytest.mark.asyncio
    async def test_orkmind_list(self) -> None:
        layer = _mock_layer()
        result = await orkmind_list(layer, collection="rule")
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_orkmind_guardrail_check(self) -> None:
        from orkmind.mcp.tools import orkmind_guardrail_check

        layer = _mock_layer()
        result = await orkmind_guardrail_check(
            layer, session_id="s1", context_usage_pct=0.7
        )
        assert set(result) == {"rules", "session", "fail_safe"}
        assert result["session"]["usage_source"] == "informado"
        assert result["rules"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_guardrail_check_registrado_no_servidor(self) -> None:
        from orkmind.mcp.server import _build_tools

        nomes = [t.name for t in _build_tools()]
        assert "orkmind_guardrail_check" in nomes
        assert "orkmind_handoff" in nomes

    @pytest.mark.asyncio
    async def test_orkmind_handoff_valida_e_armazena(self) -> None:
        from orkmind.mcp.tools import orkmind_handoff

        layer = _mock_layer()
        payload = {
            "progresso": "p" * 60,
            "decisoes": "d" * 60,
            "referencias_criticas": "r" * 60,
            "proximos_passos": "n" * 60,
        }
        result = await orkmind_handoff(layer, payload=payload, session_id="s1")
        assert result["status"] == "armazenado"
        assert result["handoff_id"] == "test-id"

    @pytest.mark.asyncio
    async def test_orkmind_handoff_invalido_pede_refacao(self) -> None:
        from orkmind.mcp.tools import orkmind_handoff

        layer = _mock_layer()
        result = await orkmind_handoff(
            layer, payload={"progresso": "curto"}, session_id="s1"
        )
        assert result["status"] == "refazer"
        assert "decisoes" in result["secoes_faltantes"]
