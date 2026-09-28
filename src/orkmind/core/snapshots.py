"""Orquestracao de snapshots globais para OrkMind."""

from __future__ import annotations

from orkmind.store.base import MemoryStore


class SnapshotManager:
    """Camada fina de orquestracao que delega ao store."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def commit(self, label: str, message: str = "") -> str:
        """Salva snapshot do estado atual. Label nao pode ser vazio."""
        if not label or not label.strip():
            raise ValueError("Label do snapshot nao pode ser vazio")
        return await self._store.snapshot_commit(label=label, message=message)

    async def log(self, limit: int = 20) -> list[dict]:
        """Lista snapshots ordenados por data (mais recente primeiro)."""
        return await self._store.snapshot_log(limit=limit)

    async def show(self, snapshot_id: str) -> dict:
        """Detalhes de um snapshot. Levanta ValueError se nao existir."""
        return await self._store.snapshot_show(snapshot_id)

    async def diff(self, snap_a: str, snap_b: str) -> dict:
        """Diff entre dois snapshots. Levanta ValueError se algum nao existir."""
        return await self._store.snapshot_diff(snap_a, snap_b)

    async def restore(self, snapshot_id: str) -> int:
        """Restaura estado de um snapshot. Auto-backup criado pelo adapter."""
        return await self._store.snapshot_restore(snapshot_id)
