"""Testes para snapshots globais (D7)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orkmind.core.snapshots import SnapshotManager
from orkmind.store.base import MemoryStore


def _make_mock_store() -> MemoryStore:
    store = AsyncMock(spec=MemoryStore)
    store.snapshot_commit = AsyncMock(return_value="snap-001")
    store.snapshot_log = AsyncMock(return_value=[
        {"id": "snap-002", "label": "v2", "message": "", "entry_count": 5,
         "created_at": "2026-08-27T10:00:00Z"},
        {"id": "snap-001", "label": "v1", "message": "primeiro", "entry_count": 3,
         "created_at": "2026-08-27T09:00:00Z"},
    ])
    store.snapshot_show = AsyncMock(return_value={
        "id": "snap-001", "label": "v1", "message": "primeiro",
        "entry_count": 3, "created_at": "2026-08-27T09:00:00Z",
        "entries": [
            {"entry_id": "e1", "content_hash": "h1", "entry_data": {}},
            {"entry_id": "e2", "content_hash": "h2", "entry_data": {}},
            {"entry_id": "e3", "content_hash": "h3", "entry_data": {}},
        ],
    })
    store.snapshot_diff = AsyncMock(return_value={
        "added": ["e4"], "removed": ["e1"], "modified": ["e2"],
    })
    store.snapshot_restore = AsyncMock(return_value=3)
    return store


class TestSnapshotManager:
    @pytest.mark.asyncio
    async def test_commit_returns_id(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        snap_id = await mgr.commit("v1", "primeiro snapshot")
        assert snap_id == "snap-001"
        store.snapshot_commit.assert_awaited_once_with(label="v1", message="primeiro snapshot")

    @pytest.mark.asyncio
    async def test_commit_empty_label_rejected(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        with pytest.raises(ValueError, match="vazio"):
            await mgr.commit("")
        with pytest.raises(ValueError, match="vazio"):
            await mgr.commit("   ")

    @pytest.mark.asyncio
    async def test_log_returns_list(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        result = await mgr.log()
        assert isinstance(result, list)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_log_limit(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        await mgr.log(limit=5)
        store.snapshot_log.assert_awaited_once_with(limit=5)

    @pytest.mark.asyncio
    async def test_log_ordered_by_date(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        result = await mgr.log()
        # Mais recente primeiro
        assert result[0]["id"] == "snap-002"
        assert result[1]["id"] == "snap-001"

    @pytest.mark.asyncio
    async def test_show_returns_entries(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        result = await mgr.show("snap-001")
        assert result["entry_count"] == 3
        assert len(result["entries"]) == 3

    @pytest.mark.asyncio
    async def test_show_nonexistent_raises(self) -> None:
        store = _make_mock_store()
        store.snapshot_show = AsyncMock(side_effect=ValueError("nao encontrado"))
        mgr = SnapshotManager(store)
        with pytest.raises(ValueError):
            await mgr.show("nao-existe")

    @pytest.mark.asyncio
    async def test_diff_added(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        result = await mgr.diff("snap-001", "snap-002")
        assert "e4" in result["added"]

    @pytest.mark.asyncio
    async def test_diff_removed(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        result = await mgr.diff("snap-001", "snap-002")
        assert "e1" in result["removed"]

    @pytest.mark.asyncio
    async def test_diff_modified(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        result = await mgr.diff("snap-001", "snap-002")
        assert "e2" in result["modified"]

    @pytest.mark.asyncio
    async def test_diff_identical(self) -> None:
        store = _make_mock_store()
        store.snapshot_diff = AsyncMock(return_value={
            "added": [], "removed": [], "modified": [],
        })
        mgr = SnapshotManager(store)
        result = await mgr.diff("snap-001", "snap-001")
        assert result["added"] == []
        assert result["removed"] == []
        assert result["modified"] == []

    @pytest.mark.asyncio
    async def test_restore_recreates_entries(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        count = await mgr.restore("snap-001")
        assert count == 3

    @pytest.mark.asyncio
    async def test_restore_calls_store(self) -> None:
        store = _make_mock_store()
        mgr = SnapshotManager(store)
        await mgr.restore("snap-001")
        store.snapshot_restore.assert_awaited_once_with("snap-001")

    @pytest.mark.asyncio
    async def test_restore_nonexistent_raises(self) -> None:
        store = _make_mock_store()
        store.snapshot_restore = AsyncMock(side_effect=ValueError("nao encontrado"))
        mgr = SnapshotManager(store)
        with pytest.raises(ValueError):
            await mgr.restore("nao-existe")
