"""Backend de storage em memoria (dicts do processo).

Degrau tecnico da DP-1: existe para que a suite de contrato rode em
qualquer maquina, sem servico externo, e para que a equivalencia de
governanca entre backends possa ser provada (prova 8.2 do plano).

R0.2 vale literalmente aqui: este adapter NAO contem decisao de
politica. Ele nao sabe o que significam `protected`, `injection_risk`,
`author_id`, `audience` ou `editors`; apenas persiste esses campos como
qualquer outro. Nao aplica ACL (declara `native_acl_filter=False`), nao
ordena por prioridade constitucional (declara
`native_constitutional_order=False`) e nao recusa escrita de agente. Tudo
isso e responsabilidade do GovernedStore, uma camada acima.

Os filtros que ele aplica sao apenas os que a propria consulta pediu
(colecao, tags, `mandatory_only`) mais a higiene de expiracao, que e
mecanica e espelha o `WHERE expires_at > now()` do backend Postgres.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np

from orkmind.core.models import MemoryEntry, Source
from orkmind.store.base import MemoryStore
from orkmind.store.capabilities import StoreCapabilities

logger = logging.getLogger(__name__)

# Tabela 3.2 do plano. `durable=False` e o unico campo que obriga aviso
# na inicializacao: este backend nunca pode virar default.
MEMORY_CAPABILITIES = StoreCapabilities(
    backend="memory",
    vector_search=True,              # cosseno em numpy sobre todos os candidatos
    accepts_external_vectors=True,
    stores_embedding=True,
    native_text_semantic=False,
    text_search="match",             # casamento de token, sem ranking
    tag_filter=True,
    unique_content_hash=True,        # dict indexado, processo unico
    versioning=True,
    snapshots=True,
    parent_id=True,
    ttl_gc=True,
    durable=False,
    native_acl_filter=False,
    native_constitutional_order=False,
    backfill=True,
)

_TOKEN_RE = re.compile(r"[0-9a-zA-Z_]+", re.UNICODE)


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _com_tz(valor: Optional[datetime]) -> Optional[datetime]:
    """Normaliza datetime ingenuo para UTC, para comparacao segura."""
    if valor is None:
        return None
    if valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


def _expirada(entry: MemoryEntry, agora: datetime) -> bool:
    expira = _com_tz(entry.expires_at)
    return expira is not None and expira <= agora


def _tokens(texto: str) -> list[str]:
    return _TOKEN_RE.findall(texto.lower())


class DuplicataDeContentHashError(ValueError):
    """Segundo insert com o mesmo (collection, content_hash).

    Espelha a UniqueViolation que o indice unico parcial do Postgres
    levanta (`uq_memories_collection_content_hash`), para que a
    idempotencia dura tenha o mesmo comportamento observavel nos dois
    backends que declaram `unique_content_hash=True`.
    """


class MemoryAdapter(MemoryStore):
    """Storage volatil em dicts. Persistencia pura, sem governanca."""

    def __init__(self, embedding_dim: int = 1024) -> None:
        self._embedding_dim = embedding_dim
        self._entries: dict[str, MemoryEntry] = {}
        self._versions: dict[str, list[MemoryEntry]] = {}
        # last_accessed_at por (memory_id, version), para o gc_versions
        self._version_acesso: dict[tuple[str, int], datetime] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._snapshot_entries: dict[str, list[dict[str, Any]]] = {}
        self._ordem_snapshot = 0
        self._avisou_volatilidade = False

    @property
    def capabilities(self) -> StoreCapabilities:
        return MEMORY_CAPABILITIES

    # --- Init / Teardown ---

    async def initialize(self) -> None:
        """Nao ha DDL. O que existe e o aviso obrigatorio de volatilidade."""
        if not self._avisou_volatilidade:
            for aviso in MEMORY_CAPABILITIES.avisos():
                logger.warning("%s", aviso)
            self._avisou_volatilidade = True

    async def close(self) -> None:
        """Nada a fechar. Os dados morrem com o processo, por desenho."""
        return None

    # --- Helpers internos ---

    @staticmethod
    def _copia(entry: MemoryEntry) -> MemoryEntry:
        """Copia profunda: nada de vazar referencia mutavel do dict."""
        return entry.model_copy(deep=True)

    def _ativas(self, agora: Optional[datetime] = None) -> list[MemoryEntry]:
        agora = agora or _agora()
        return [e for e in self._entries.values() if not _expirada(e, agora)]

    @staticmethod
    def _ordem_estavel(entries: list[MemoryEntry]) -> list[MemoryEntry]:
        """Ordem determinista e neutra: updated_at DESC, id ASC.

        Neutra de proposito: nao ha prioridade nem `mandatory` aqui. A
        ordenacao constitucional e da camada de governanca, que sabe que
        este backend declara `native_constitutional_order=False`.
        """
        return sorted(
            entries,
            key=lambda e: (-(_com_tz(e.updated_at) or _agora()).timestamp(), e.id),
        )

    def _casa_tags(self, entry: MemoryEntry, tags: dict[str, list[str]]) -> bool:
        """Contencao por dimensao, equivalente ao `tags @> %s` do JSONB."""
        for dimensao, valores in tags.items():
            presentes = entry.tags.get(dimensao) or []
            if not set(valores).issubset(set(presentes)):
                return False
        return True

    # --- CRUD ---

    async def store(self, entry: MemoryEntry) -> str:
        if entry.content_hash:
            existente = self._por_hash(entry.content_hash, entry.collection)
            if existente is not None:
                raise DuplicataDeContentHashError(
                    f"Ja existe entry na colecao '{entry.collection}' com o "
                    f"content_hash '{entry.content_hash}' (id '{existente.id}'). "
                    f"Idempotencia dura: o segundo insert e recusado."
                )
        self._entries[entry.id] = self._copia(entry)
        return entry.id

    def _por_hash(
        self, content_hash: str, collection: Optional[str]
    ) -> Optional[MemoryEntry]:
        candidatos = [
            e
            for e in self._entries.values()
            if e.content_hash == content_hash
            and (collection is None or e.collection == collection)
        ]
        if not candidatos:
            return None
        candidatos.sort(key=lambda e: ((_com_tz(e.created_at) or _agora()), e.id))
        return candidatos[0]

    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        entry = self._entries.get(entry_id)
        return self._copia(entry) if entry else None

    async def find_by_content_hash(
        self,
        content_hash: str,
        collection: Optional[str] = None,
    ) -> Optional[MemoryEntry]:
        if not content_hash:
            return None
        achada = self._por_hash(content_hash, collection)
        return self._copia(achada) if achada else None

    async def update(
        self,
        entry_id: str,
        entry: MemoryEntry,
        requester_id: Optional[str] = None,
    ) -> bool:
        """Sobrescreve a entry e arquiva a versao anterior.

        `requester_id` e ignorado aqui de forma DECLARADA: este backend
        publica `native_acl_filter=False` e a ACL de escrita vive no
        GovernedStore (R0.2 e R0.3).
        """
        atual = self._entries.get(entry_id)
        if atual is None:
            return False

        self._versions.setdefault(entry_id, []).append(self._copia(atual))
        self._version_acesso[(entry_id, atual.version)] = _agora()

        nova = self._copia(entry)
        nova.id = entry_id
        nova.created_at = atual.created_at
        nova.version = atual.version + 1
        nova.updated_at = _agora()
        self._entries[entry_id] = nova
        return True

    async def delete(
        self,
        entry_id: str,
        source: Source = "human",
        requester_id: Optional[str] = None,
    ) -> bool:
        """Remove a entry e as versoes dela (equivale ao ON DELETE CASCADE).

        `source` e `requester_id` sao ignorados de forma declarada: a
        protecao D2 e a ACL de escrita sao do GovernedStore.
        """
        if entry_id not in self._entries:
            return False
        del self._entries[entry_id]
        for versao in self._versions.pop(entry_id, []):
            self._version_acesso.pop((entry_id, versao.version), None)
        return True

    # --- Search ---

    async def search_by_tags(
        self,
        tags: dict[str, list[str]],
        collection: Optional[str] = None,
        mandatory_only: bool = False,
        limit: int = 50,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        agora = _agora()
        resultado = []
        for entry in self._ativas(agora):
            if collection and entry.collection != collection:
                continue
            if not self._casa_tags(entry, tags):
                continue
            # Filtro pedido pela propria consulta, nao decisao do adapter:
            # o chamador disse "so mandatory". Nada aqui julga o valor.
            if mandatory_only and not entry.mandatory:
                continue
            resultado.append(entry)
        ordenadas = self._ordem_estavel(resultado)[:limit]
        return [self._copia(e) for e in ordenadas]

    async def search_by_text(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Casamento de token, sem ranking (`text_search="match"`).

        Todos os tokens da consulta precisam aparecer no conteudo. Nao ha
        stemming nem `ts_rank`: a diferenca de qualidade para o pgvector
        esta declarada em capabilities e na matriz de backends.
        """
        agora = _agora()
        alvos = _tokens(query)
        if not alvos:
            return []
        resultado = []
        for entry in self._ativas(agora):
            if collection and entry.collection != collection:
                continue
            conteudo = entry.content.lower()
            if all(alvo in conteudo for alvo in alvos):
                resultado.append(entry)
        ordenadas = self._ordem_estavel(resultado)[:limit]
        return [self._copia(e) for e in ordenadas]

    async def search_semantic(
        self,
        embedding: list[float],
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Similaridade de cosseno em numpy sobre os candidatos ativos."""
        agora = _agora()
        consulta = np.asarray(embedding, dtype=float)
        norma_consulta = float(np.linalg.norm(consulta))
        if norma_consulta == 0.0:
            return []

        pontuadas: list[tuple[float, str, MemoryEntry]] = []
        for entry in self._ativas(agora):
            if collection and entry.collection != collection:
                continue
            if not entry.embedding:
                continue
            vetor = np.asarray(entry.embedding, dtype=float)
            if vetor.shape != consulta.shape:
                continue
            norma = float(np.linalg.norm(vetor))
            if norma == 0.0:
                continue
            # distancia de cosseno, igual ao operador <=> do pgvector
            distancia = 1.0 - float(np.dot(vetor, consulta) / (norma * norma_consulta))
            pontuadas.append((distancia, entry.id, entry))

        pontuadas.sort(key=lambda item: (item[0], item[1]))
        return [self._copia(e) for _, _, e in pontuadas[:limit]]

    # --- Collections ---

    async def list_collections(self) -> list[str]:
        return sorted({e.collection for e in self._entries.values()})

    async def count(self, collection: Optional[str] = None) -> int:
        if collection:
            return sum(1 for e in self._entries.values() if e.collection == collection)
        return len(self._entries)

    # --- Hierarquia ---

    async def get_children(self, parent_id: str) -> list[MemoryEntry]:
        agora = _agora()
        filhas = [e for e in self._ativas(agora) if e.parent_id == parent_id]
        filhas.sort(key=lambda e: ((_com_tz(e.created_at) or agora), e.id))
        return [self._copia(e) for e in filhas]

    # --- Backfill de embeddings (F3.6) ---

    async def list_entries_without_embedding(
        self,
        collection: Optional[str] = None,
        limit: int = 1000,
    ) -> list[MemoryEntry]:
        agora = _agora()
        pendentes = [
            e
            for e in self._ativas(agora)
            if not e.embedding and (collection is None or e.collection == collection)
        ]
        pendentes.sort(key=lambda e: ((_com_tz(e.created_at) or agora), e.id))
        return [self._copia(e) for e in pendentes[:limit]]

    async def set_embedding(self, entry_id: str, embedding: list[float]) -> bool:
        """Grava SOMENTE o vetor. Nao versiona, nao mexe em updated_at."""
        if len(embedding) != self._embedding_dim:
            raise ValueError(
                f"Embedding com {len(embedding)} dimensoes; o backend "
                f"'memory' espera {self._embedding_dim}."
            )
        entry = self._entries.get(entry_id)
        if entry is None:
            return False
        entry.embedding = list(embedding)
        return True

    # --- Versionamento ---

    async def get_history(self, entry_id: str) -> list[MemoryEntry]:
        versoes = self._versions.get(entry_id, [])
        agora = _agora()
        for versao in versoes:
            self._version_acesso[(entry_id, versao.version)] = agora
        ordenadas = sorted(versoes, key=lambda e: e.version, reverse=True)
        return [self._copia(e) for e in ordenadas]

    async def gc_versions(self, max_age_days: int = 365) -> int:
        corte = _agora() - timedelta(days=max_age_days)
        removidas = 0
        for entry_id, versoes in list(self._versions.items()):
            mantidas = []
            for versao in versoes:
                acesso = self._version_acesso.get((entry_id, versao.version))
                if acesso is not None and acesso < corte:
                    self._version_acesso.pop((entry_id, versao.version), None)
                    removidas += 1
                else:
                    mantidas.append(versao)
            if mantidas:
                self._versions[entry_id] = mantidas
            else:
                self._versions.pop(entry_id, None)
        return removidas

    # --- Snapshots ---

    async def snapshot_commit(self, label: str, message: str = "") -> str:
        snapshot_id = str(uuid.uuid4())
        self._ordem_snapshot += 1
        entries = list(self._entries.values())
        self._snapshots[snapshot_id] = {
            "id": snapshot_id,
            "label": label,
            "message": message,
            "entry_count": len(entries),
            "created_at": _agora(),
            "ordem": self._ordem_snapshot,
        }
        registros = []
        for entry in entries:
            dados = entry.model_dump(mode="json")
            if entry.embedding:
                dados["embedding"] = list(entry.embedding)
            registros.append(
                {
                    "entry_id": entry.id,
                    "content_hash": entry.content_hash,
                    "entry_data": dados,
                }
            )
        self._snapshot_entries[snapshot_id] = registros
        return snapshot_id

    async def snapshot_log(self, limit: int = 20) -> list[dict]:
        ordenados = sorted(
            self._snapshots.values(),
            key=lambda s: (s["created_at"], s["ordem"]),
            reverse=True,
        )
        return [
            {
                "id": s["id"],
                "label": s["label"],
                "message": s["message"],
                "entry_count": s["entry_count"],
                "created_at": s["created_at"],
            }
            for s in ordenados[:limit]
        ]

    async def snapshot_show(self, snapshot_id: str) -> dict:
        snap = self._snapshots.get(snapshot_id)
        if snap is None:
            raise ValueError(f"Snapshot '{snapshot_id}' nao encontrado")
        registros = self._snapshot_entries.get(snapshot_id, [])
        return {
            "id": snap["id"],
            "label": snap["label"],
            "message": snap["message"],
            "entry_count": snap["entry_count"],
            "created_at": snap["created_at"],
            "entries": [
                {
                    "entry_id": r["entry_id"],
                    "content_hash": r["content_hash"],
                    "entry_data": dict(r["entry_data"]),
                }
                for r in registros
            ],
        }

    async def snapshot_diff(self, snap_a: str, snap_b: str) -> dict:
        for sid in (snap_a, snap_b):
            if sid not in self._snapshots:
                raise ValueError(f"Snapshot '{sid}' nao encontrado")
        hashes_a = {
            r["entry_id"]: r["content_hash"] for r in self._snapshot_entries.get(snap_a, [])
        }
        hashes_b = {
            r["entry_id"]: r["content_hash"] for r in self._snapshot_entries.get(snap_b, [])
        }
        ids_a = set(hashes_a)
        ids_b = set(hashes_b)
        return {
            "added": sorted(ids_b - ids_a),
            "removed": sorted(ids_a - ids_b),
            "modified": sorted(
                eid for eid in ids_a & ids_b if hashes_a[eid] != hashes_b[eid]
            ),
        }

    async def snapshot_restore(self, snapshot_id: str) -> int:
        if snapshot_id not in self._snapshots:
            raise ValueError(f"Snapshot '{snapshot_id}' nao encontrado")
        # Auto-backup antes de qualquer escrita destrutiva (I8)
        await self.snapshot_commit(label="auto-backup-pre-restore")

        self._entries.clear()
        self._versions.clear()
        self._version_acesso.clear()

        contagem = 0
        for registro in self._snapshot_entries.get(snapshot_id, []):
            entry = MemoryEntry(**dict(registro["entry_data"]))
            await self.store(entry)
            contagem += 1
        return contagem

    # --- Maintenance ---

    async def garbage_collect(self) -> int:
        agora = _agora()
        expiradas = [e.id for e in self._entries.values() if _expirada(e, agora)]
        for entry_id in expiradas:
            self._entries.pop(entry_id, None)
            for versao in self._versions.pop(entry_id, []):
                self._version_acesso.pop((entry_id, versao.version), None)
        return len(expiradas)
