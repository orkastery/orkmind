"""Backend Qdrant: primeiro backend de produto novo do OrkMind (F3.5).

Persistencia pura, zero politica (R0.2). Este adapter nao sabe o que
significam `protected`, `mandatory`, `author_id`, `audience`, `editors`
ou `injection_risk`: ele guarda esses campos como guarda qualquer outro.
A governanca vive no `GovernedStore`, uma camada acima.

Desenho:

- **Uma colecao** (default `orkmind_memories`, configuravel em
  `[store.options].collection`), com o payload discriminado por `kind`:
  `entry`, `version`, `snapshot` e `snapshot_entry`. Todo acesso passa
  por `_filtro_base(kind)`, para que esquecer o discriminador seja
  impossivel por construcao.
- **Ids** derivados por `uuid5` a partir de `(kind, chave logica)`. O
  Qdrant so aceita UUID ou inteiro como id de ponto, e os ids do OrkMind
  sao strings livres; derivar resolve os dois casos com uma regra so, e
  o id real continua no payload.
- **Vetor** nomeado `content`, dimensao `embedding_dim`, distancia
  cosseno. Entry sem embedding e gravada com `vector={}`: o cliente
  aceita ponto sem vetor nomeado e a busca vetorial simplesmente nao o
  devolve. O caminho alternativo previsto no plano (vetor zero mais
  filtro) NAO foi necessario, e isso esta registrado no log do ciclo.
- **`unique_content_hash=False`** (DP-6): nao ha indice unico por
  `(collection, content_hash)`. O `initialize()` avisa, e o
  `GovernedStore` faz a consulta por hash antes de gravar. Nenhum teste
  afirma paridade com o indice unico do Postgres.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from qdrant_client import AsyncQdrantClient, models

from orkmind.core.models import MemoryEntry, Source
from orkmind.store.base import MemoryStore
from orkmind.store.capabilities import StoreCapabilities
from orkmind.store.errors import BackendMalConfiguradoError

logger = logging.getLogger(__name__)

# Tabela 3.2 do plano.
QDRANT_CAPABILITIES = StoreCapabilities(
    backend="qdrant",
    vector_search=True,              # HNSW nativo
    accepts_external_vectors=True,
    stores_embedding=True,
    native_text_semantic=False,
    text_search="match",             # MatchText, sem ranking
    tag_filter=True,
    unique_content_hash=False,       # DP-6: aviso obrigatorio no initialize
    versioning=True,                 # pontos com kind="version"
    snapshots=True,                  # pontos com kind="snapshot*"
    parent_id=True,
    ttl_gc=True,
    durable=True,
    native_acl_filter=False,
    native_constitutional_order=False,
    backfill=True,
)

COLECAO_PADRAO = "orkmind_memories"
VETOR = "content"

KIND_ENTRY = "entry"
KIND_VERSION = "version"
KIND_SNAPSHOT = "snapshot"
KIND_SNAPSHOT_ENTRY = "snapshot_entry"

# Namespace fixo para derivar ids de ponto. Nao e segredo nem chave: e
# so uma constante para que a derivacao seja estavel entre execucoes.
NAMESPACE_ORKMIND = uuid.UUID("6f1f5d2a-6f0a-5b7e-9c3d-0a1b2c3d4e5f")

# Teto de varredura por pagina do scroll.
PAGINA = 256

_CAMPOS_MODELO = set(MemoryEntry.model_fields)


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _com_tz(valor: Optional[datetime]) -> Optional[datetime]:
    if valor is None:
        return None
    if valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


def _timestamp(valor: Optional[datetime]) -> Optional[float]:
    normalizado = _com_tz(valor)
    return normalizado.timestamp() if normalizado else None


def _ponto_id(kind: str, *partes: str) -> str:
    """Id de ponto determinista a partir da chave logica."""
    return str(uuid.uuid5(NAMESPACE_ORKMIND, ":".join([kind, *partes])))


def _entry_para_payload(entry: MemoryEntry) -> dict[str, Any]:
    """Payload de uma entry. O embedding fica no vetor, nao no payload."""
    dados = entry.model_dump(mode="json")
    dados.pop("embedding", None)
    dados["kind"] = KIND_ENTRY
    dados["expires_at_ts"] = _timestamp(entry.expires_at)
    dados["created_at_ts"] = _timestamp(entry.created_at)
    dados["updated_at_ts"] = _timestamp(entry.updated_at)
    dados["has_embedding"] = bool(entry.embedding)
    return dados


def _payload_para_entry(
    payload: dict[str, Any], vetor: Any = None
) -> MemoryEntry:
    dados = {k: v for k, v in payload.items() if k in _CAMPOS_MODELO}
    embedding = None
    if isinstance(vetor, dict):
        embedding = vetor.get(VETOR)
    elif isinstance(vetor, list):
        embedding = vetor
    dados["embedding"] = list(embedding) if embedding else None
    return MemoryEntry(**dados)


class QdrantAdapter(MemoryStore):
    """Storage sobre Qdrant. Le e escreve bytes, nao decide politica."""

    def __init__(
        self,
        options: Optional[dict[str, Any]] = None,
        embedding_dim: int = 1024,
    ) -> None:
        opcoes = dict(options or {})
        url = str(opcoes.get("url") or "").strip()
        if not url:
            raise BackendMalConfiguradoError(
                "Backend 'qdrant' exige a URL da instancia. Defina "
                "[store.options].url em ~/.orkmind/config.toml ou "
                'ORKMIND_STORE_OPTIONS=\'{"url": "http://localhost:6333"}\'.'
            )
        self._url = url
        self._colecao = str(opcoes.get("collection") or COLECAO_PADRAO)
        self._embedding_dim = embedding_dim
        self._timeout = float(opcoes.get("timeout_s", 30.0))
        chave_env = opcoes.get("api_key_env")
        self._api_key = os.environ.get(str(chave_env)) if chave_env else None
        self._cliente: Optional[AsyncQdrantClient] = None
        self._ordem_snapshot = 0

    @property
    def capabilities(self) -> StoreCapabilities:
        return QDRANT_CAPABILITIES

    @property
    def collection_name(self) -> str:
        return self._colecao

    # --- Cliente ---

    def _obter_cliente(self) -> AsyncQdrantClient:
        if self._cliente is None:
            self._cliente = AsyncQdrantClient(
                location=self._url, api_key=self._api_key, timeout=int(self._timeout)
            )
        return self._cliente

    # --- Init / Teardown ---

    async def initialize(self) -> None:
        """Cria a colecao e os indices. Idempotente."""
        cliente = self._obter_cliente()
        try:
            existe = await cliente.collection_exists(self._colecao)
            if not existe:
                await cliente.create_collection(
                    collection_name=self._colecao,
                    vectors_config={
                        VETOR: models.VectorParams(
                            size=self._embedding_dim,
                            distance=models.Distance.COSINE,
                        )
                    },
                )
            await self._criar_indices(cliente)
        except BackendMalConfiguradoError:
            raise
        except Exception as e:
            # Fail-safe (8.5): backend fora do ar nunca vira contexto
            # vazio silencioso. A excecao sobe nomeando o backend.
            raise RuntimeError(
                f"Backend 'qdrant' em '{self._url}' nao respondeu ao "
                f"initialize da colecao '{self._colecao}': {e}"
            ) from e

        # DP-6: a protecao contra duplicata concorrente e mais fraca aqui.
        logger.warning(
            "Backend 'qdrant' nao tem indice unico (collection, content_hash): "
            "a protecao contra duplicata concorrente e best-effort, feita "
            "por consulta antes da gravacao. Nao ha paridade com o indice "
            "unico do Postgres."
        )

    async def _criar_indices(self, cliente: AsyncQdrantClient) -> None:
        indices: list[tuple[str, Any]] = [
            ("kind", models.PayloadSchemaType.KEYWORD),
            ("collection", models.PayloadSchemaType.KEYWORD),
            ("content_hash", models.PayloadSchemaType.KEYWORD),
            ("mandatory", models.PayloadSchemaType.BOOL),
            ("parent_id", models.PayloadSchemaType.KEYWORD),
            ("memory_id", models.PayloadSchemaType.KEYWORD),
            ("snapshot_id", models.PayloadSchemaType.KEYWORD),
            ("expires_at_ts", models.PayloadSchemaType.FLOAT),
            ("has_embedding", models.PayloadSchemaType.BOOL),
            ("last_accessed_ts", models.PayloadSchemaType.FLOAT),
            (
                "content",
                models.TextIndexParams(
                    type=models.TextIndexType.TEXT,
                    tokenizer=models.TokenizerType.WORD,
                    lowercase=True,
                ),
            ),
        ]
        for campo, schema in indices:
            try:
                await cliente.create_payload_index(
                    collection_name=self._colecao,
                    field_name=campo,
                    field_schema=schema,
                )
            except Exception as e:
                # Indice ja existente ou nao suportado (o modo local do
                # cliente ignora indices) nao pode derrubar o initialize.
                logger.debug("Indice de payload '%s' nao criado: %s", campo, e)

    async def close(self) -> None:
        if self._cliente is not None:
            await self._cliente.close()
            self._cliente = None

    # --- Filtros ---

    @staticmethod
    def _filtro_base(kind: str) -> models.Filter:
        """Todo acesso comeca por aqui: o discriminador nunca e esquecido."""
        return models.Filter(
            must=[
                models.FieldCondition(key="kind", match=models.MatchValue(value=kind))
            ]
        )

    def _filtro_entries(
        self,
        collection: Optional[str] = None,
        apenas_ativas: bool = True,
        extras: Optional[list[models.Condition]] = None,
    ) -> models.Filter:
        # Mesmo discriminador de `_filtro_base`, ja como lista mutavel
        # para receber as condicoes extras da consulta.
        must: list[models.Condition] = [
            models.FieldCondition(
                key="kind", match=models.MatchValue(value=KIND_ENTRY)
            )
        ]
        if collection:
            must.append(
                models.FieldCondition(
                    key="collection", match=models.MatchValue(value=collection)
                )
            )
        if extras:
            must.extend(extras)
        should: Optional[list[models.Condition]] = None
        if apenas_ativas:
            # Higiene mecanica, nao politica: espelha o
            # `WHERE expires_at IS NULL OR expires_at > now()` do SQL.
            should = [
                models.IsNullCondition(
                    is_null=models.PayloadField(key="expires_at_ts")
                ),
                models.FieldCondition(
                    key="expires_at_ts",
                    range=models.Range(gt=_agora().timestamp()),
                ),
            ]
        return models.Filter(must=must, should=should)

    # --- Scroll ---

    async def _scroll(
        self,
        filtro: models.Filter,
        limite: Optional[int] = None,
        com_vetor: bool = False,
    ) -> list[Any]:
        cliente = self._obter_cliente()
        coletados: list[Any] = []
        offset: Any = None
        while True:
            pagina = PAGINA if limite is None else min(PAGINA, limite - len(coletados))
            if pagina <= 0:
                break
            pontos, offset = await cliente.scroll(
                collection_name=self._colecao,
                scroll_filter=filtro,
                limit=pagina,
                offset=offset,
                with_payload=True,
                with_vectors=com_vetor,
            )
            coletados.extend(pontos)
            if offset is None or not pontos:
                break
        return coletados

    @staticmethod
    def _ordem_estavel(entries: list[MemoryEntry]) -> list[MemoryEntry]:
        """Ordem determinista e neutra: updated_at DESC, id ASC.

        Neutra de proposito. Este backend declara
        `native_constitutional_order=False`: quem ordena por prioridade e
        `mandatory` e a camada de governanca.
        """
        referencia = _agora()
        return sorted(
            entries,
            key=lambda e: (-(_com_tz(e.updated_at) or referencia).timestamp(), e.id),
        )

    # --- CRUD ---

    async def store(self, entry: MemoryEntry) -> str:
        cliente = self._obter_cliente()
        await cliente.upsert(
            collection_name=self._colecao,
            points=[
                models.PointStruct(
                    id=_ponto_id(KIND_ENTRY, entry.id),
                    vector={VETOR: list(entry.embedding)} if entry.embedding else {},
                    payload=_entry_para_payload(entry),
                )
            ],
        )
        return entry.id

    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        cliente = self._obter_cliente()
        pontos = await cliente.retrieve(
            collection_name=self._colecao,
            ids=[_ponto_id(KIND_ENTRY, entry_id)],
            with_payload=True,
            with_vectors=True,
        )
        for ponto in pontos:
            payload = ponto.payload or {}
            if payload.get("kind") == KIND_ENTRY:
                return _payload_para_entry(payload, ponto.vector)
        return None

    async def find_by_content_hash(
        self,
        content_hash: str,
        collection: Optional[str] = None,
    ) -> Optional[MemoryEntry]:
        if not content_hash:
            return None
        filtro = self._filtro_entries(
            collection=collection,
            apenas_ativas=False,
            extras=[
                models.FieldCondition(
                    key="content_hash", match=models.MatchValue(value=content_hash)
                )
            ],
        )
        pontos = await self._scroll(filtro, com_vetor=True)
        entries = [_payload_para_entry(p.payload or {}, p.vector) for p in pontos]
        if not entries:
            return None
        entries.sort(key=lambda e: ((_com_tz(e.created_at) or _agora()), e.id))
        return entries[0]

    async def update(
        self,
        entry_id: str,
        entry: MemoryEntry,
        requester_id: Optional[str] = None,
    ) -> bool:
        """Sobrescreve a entry e arquiva a versao anterior.

        `requester_id` e ignorado de forma DECLARADA: este backend publica
        `native_acl_filter=False`; a ACL de escrita e do GovernedStore.
        """
        atual = await self.retrieve(entry_id)
        if atual is None:
            return False

        await self._arquivar_versao(entry_id, atual)

        nova = entry.model_copy(deep=True)
        nova.id = entry_id
        nova.created_at = atual.created_at
        nova.version = atual.version + 1
        nova.updated_at = _agora()
        await self.store(nova)
        return True

    async def _arquivar_versao(self, entry_id: str, atual: MemoryEntry) -> None:
        cliente = self._obter_cliente()
        agora = _agora().timestamp()
        payload = atual.model_dump(mode="json")
        payload.pop("embedding", None)
        payload.update(
            {
                "kind": KIND_VERSION,
                "memory_id": entry_id,
                "archived_ts": agora,
                "last_accessed_ts": agora,
            }
        )
        await cliente.upsert(
            collection_name=self._colecao,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid4()), vector={}, payload=payload
                )
            ],
        )

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
        if await self.retrieve(entry_id) is None:
            return False
        cliente = self._obter_cliente()
        await cliente.delete(
            collection_name=self._colecao,
            points_selector=models.PointIdsList(
                points=[_ponto_id(KIND_ENTRY, entry_id)]
            ),
        )
        await cliente.delete(
            collection_name=self._colecao,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="kind", match=models.MatchValue(value=KIND_VERSION)
                        ),
                        models.FieldCondition(
                            key="memory_id", match=models.MatchValue(value=entry_id)
                        ),
                    ]
                )
            ),
        )
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
        extras: list[models.Condition] = []
        # Contencao por dimensao: cada valor pedido vira uma condicao AND,
        # equivalente ao `tags @> %s` do JSONB.
        for dimensao, valores in tags.items():
            for valor in valores:
                extras.append(
                    models.FieldCondition(
                        key=f"tags.{dimensao}", match=models.MatchValue(value=valor)
                    )
                )
        if mandatory_only:
            # Filtro pedido pela propria consulta, nao decisao do adapter.
            extras.append(
                models.FieldCondition(
                    key="mandatory", match=models.MatchValue(value=True)
                )
            )
        filtro = self._filtro_entries(collection=collection, extras=extras)
        pontos = await self._scroll(filtro, limite=limit, com_vetor=True)
        entries = [_payload_para_entry(p.payload or {}, p.vector) for p in pontos]
        return self._ordem_estavel(entries)[:limit]

    async def search_by_text(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """`MatchText` sobre o conteudo, sem ranking (`text_search="match"`)."""
        if not query.strip():
            return []
        filtro = self._filtro_entries(
            collection=collection,
            extras=[
                models.FieldCondition(
                    key="content", match=models.MatchText(text=query)
                )
            ],
        )
        pontos = await self._scroll(filtro, limite=limit, com_vetor=True)
        entries = [_payload_para_entry(p.payload or {}, p.vector) for p in pontos]
        return self._ordem_estavel(entries)[:limit]

    async def search_semantic(
        self,
        embedding: list[float],
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        cliente = self._obter_cliente()
        filtro = self._filtro_entries(collection=collection)
        resultado = await cliente.query_points(
            collection_name=self._colecao,
            query=list(embedding),
            using=VETOR,
            query_filter=filtro,
            limit=limit,
            with_payload=True,
            with_vectors=True,
        )
        # Pontos sem vetor nomeado nao entram na busca vetorial: o proprio
        # motor os ignora. A ordem devolvida e a de similaridade.
        return [
            _payload_para_entry(p.payload or {}, p.vector) for p in resultado.points
        ]

    # --- Collections ---

    async def list_collections(self) -> list[str]:
        pontos = await self._scroll(self._filtro_entries(apenas_ativas=False))
        return sorted(
            {
                str((p.payload or {}).get("collection"))
                for p in pontos
                if (p.payload or {}).get("collection")
            }
        )

    async def count(self, collection: Optional[str] = None) -> int:
        cliente = self._obter_cliente()
        resultado = await cliente.count(
            collection_name=self._colecao,
            count_filter=self._filtro_entries(
                collection=collection, apenas_ativas=False
            ),
            exact=True,
        )
        return int(resultado.count)

    # --- Hierarquia ---

    async def get_children(self, parent_id: str) -> list[MemoryEntry]:
        filtro = self._filtro_entries(
            extras=[
                models.FieldCondition(
                    key="parent_id", match=models.MatchValue(value=parent_id)
                )
            ]
        )
        pontos = await self._scroll(filtro, com_vetor=True)
        entries = [_payload_para_entry(p.payload or {}, p.vector) for p in pontos]
        entries.sort(key=lambda e: ((_com_tz(e.created_at) or _agora()), e.id))
        return entries

    # --- Backfill de embeddings (F3.6) ---

    async def list_entries_without_embedding(
        self,
        collection: Optional[str] = None,
        limit: int = 1000,
    ) -> list[MemoryEntry]:
        filtro = self._filtro_entries(
            collection=collection,
            extras=[
                models.FieldCondition(
                    key="has_embedding", match=models.MatchValue(value=False)
                )
            ],
        )
        pontos = await self._scroll(filtro)
        entries = [_payload_para_entry(p.payload or {}) for p in pontos]
        entries.sort(key=lambda e: ((_com_tz(e.created_at) or _agora()), e.id))
        return entries[:limit]

    async def set_embedding(self, entry_id: str, embedding: list[float]) -> bool:
        """Grava SOMENTE o vetor. Nao versiona, nao mexe em updated_at."""
        if len(embedding) != self._embedding_dim:
            raise ValueError(
                f"Embedding com {len(embedding)} dimensoes; o backend "
                f"'qdrant' espera {self._embedding_dim}."
            )
        if await self.retrieve(entry_id) is None:
            return False
        cliente = self._obter_cliente()
        ponto = _ponto_id(KIND_ENTRY, entry_id)
        await cliente.update_vectors(
            collection_name=self._colecao,
            points=[models.PointVectors(id=ponto, vector={VETOR: list(embedding)})],
        )
        await cliente.set_payload(
            collection_name=self._colecao,
            payload={"has_embedding": True},
            points=[ponto],
        )
        return True

    # --- Versionamento ---

    async def get_history(self, entry_id: str) -> list[MemoryEntry]:
        filtro = models.Filter(
            must=[
                models.FieldCondition(
                    key="kind", match=models.MatchValue(value=KIND_VERSION)
                ),
                models.FieldCondition(
                    key="memory_id", match=models.MatchValue(value=entry_id)
                ),
            ]
        )
        pontos = await self._scroll(filtro)
        if pontos:
            cliente = self._obter_cliente()
            await cliente.set_payload(
                collection_name=self._colecao,
                payload={"last_accessed_ts": _agora().timestamp()},
                points=[p.id for p in pontos],
            )
        entries = [_payload_para_entry(p.payload or {}) for p in pontos]
        return sorted(entries, key=lambda e: e.version, reverse=True)

    async def gc_versions(self, max_age_days: int = 365) -> int:
        cliente = self._obter_cliente()
        corte = (_agora() - timedelta(days=max_age_days)).timestamp()
        filtro = models.Filter(
            must=[
                models.FieldCondition(
                    key="kind", match=models.MatchValue(value=KIND_VERSION)
                ),
                models.FieldCondition(
                    key="last_accessed_ts", range=models.Range(lt=corte)
                ),
            ]
        )
        quantas = await cliente.count(
            collection_name=self._colecao, count_filter=filtro, exact=True
        )
        if quantas.count:
            await cliente.delete(
                collection_name=self._colecao,
                points_selector=models.FilterSelector(filter=filtro),
            )
        return int(quantas.count)

    # --- Snapshots ---

    async def snapshot_commit(self, label: str, message: str = "") -> str:
        cliente = self._obter_cliente()
        snapshot_id = str(uuid.uuid4())
        self._ordem_snapshot += 1
        pontos = await self._scroll(
            self._filtro_entries(apenas_ativas=False), com_vetor=True
        )
        entries = [_payload_para_entry(p.payload or {}, p.vector) for p in pontos]

        registros = [
            models.PointStruct(
                id=_ponto_id(KIND_SNAPSHOT, snapshot_id),
                vector={},
                payload={
                    "kind": KIND_SNAPSHOT,
                    "id": snapshot_id,
                    "label": label,
                    "message": message,
                    "entry_count": len(entries),
                    "created_at": _agora().isoformat(),
                    "created_ts": _agora().timestamp(),
                    "ordem": self._ordem_snapshot,
                },
            )
        ]
        for entry in entries:
            dados = entry.model_dump(mode="json")
            if entry.embedding:
                dados["embedding"] = list(entry.embedding)
            registros.append(
                models.PointStruct(
                    id=_ponto_id(KIND_SNAPSHOT_ENTRY, snapshot_id, entry.id),
                    vector={},
                    payload={
                        "kind": KIND_SNAPSHOT_ENTRY,
                        "snapshot_id": snapshot_id,
                        "entry_id": entry.id,
                        "content_hash": entry.content_hash,
                        "entry_data": dados,
                    },
                )
            )
        await cliente.upsert(collection_name=self._colecao, points=registros)
        return snapshot_id

    async def _snapshot_payload(self, snapshot_id: str) -> dict[str, Any]:
        cliente = self._obter_cliente()
        pontos = await cliente.retrieve(
            collection_name=self._colecao,
            ids=[_ponto_id(KIND_SNAPSHOT, snapshot_id)],
            with_payload=True,
        )
        for ponto in pontos:
            payload = ponto.payload or {}
            if payload.get("kind") == KIND_SNAPSHOT:
                return payload
        raise ValueError(f"Snapshot '{snapshot_id}' nao encontrado")

    async def _snapshot_entries(self, snapshot_id: str) -> list[dict[str, Any]]:
        filtro = models.Filter(
            must=[
                models.FieldCondition(
                    key="kind", match=models.MatchValue(value=KIND_SNAPSHOT_ENTRY)
                ),
                models.FieldCondition(
                    key="snapshot_id", match=models.MatchValue(value=snapshot_id)
                ),
            ]
        )
        pontos = await self._scroll(filtro)
        return [dict(p.payload or {}) for p in pontos]

    async def snapshot_log(self, limit: int = 20) -> list[dict]:
        pontos = await self._scroll(self._filtro_base(KIND_SNAPSHOT))
        payloads = [dict(p.payload or {}) for p in pontos]
        payloads.sort(
            key=lambda s: (float(s.get("created_ts") or 0.0), int(s.get("ordem") or 0)),
            reverse=True,
        )
        return [
            {
                "id": s.get("id"),
                "label": s.get("label"),
                "message": s.get("message", ""),
                "entry_count": s.get("entry_count", 0),
                "created_at": s.get("created_at"),
            }
            for s in payloads[:limit]
        ]

    async def snapshot_show(self, snapshot_id: str) -> dict:
        snap = await self._snapshot_payload(snapshot_id)
        registros = await self._snapshot_entries(snapshot_id)
        return {
            "id": snap.get("id"),
            "label": snap.get("label"),
            "message": snap.get("message", ""),
            "entry_count": snap.get("entry_count", 0),
            "created_at": snap.get("created_at"),
            "entries": [
                {
                    "entry_id": r.get("entry_id"),
                    "content_hash": r.get("content_hash"),
                    "entry_data": dict(r.get("entry_data") or {}),
                }
                for r in registros
            ],
        }

    async def snapshot_diff(self, snap_a: str, snap_b: str) -> dict:
        for sid in (snap_a, snap_b):
            await self._snapshot_payload(sid)
        hashes_a = {
            r["entry_id"]: r.get("content_hash")
            for r in await self._snapshot_entries(snap_a)
        }
        hashes_b = {
            r["entry_id"]: r.get("content_hash")
            for r in await self._snapshot_entries(snap_b)
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
        await self._snapshot_payload(snapshot_id)
        # Auto-backup antes de qualquer escrita destrutiva (I8)
        await self.snapshot_commit(label="auto-backup-pre-restore")

        cliente = self._obter_cliente()
        registros = await self._snapshot_entries(snapshot_id)
        await cliente.delete(
            collection_name=self._colecao,
            points_selector=models.FilterSelector(
                filter=self._filtro_base(KIND_ENTRY)
            ),
        )
        await cliente.delete(
            collection_name=self._colecao,
            points_selector=models.FilterSelector(
                filter=self._filtro_base(KIND_VERSION)
            ),
        )

        contagem = 0
        for registro in registros:
            entry = MemoryEntry(**dict(registro.get("entry_data") or {}))
            await self.store(entry)
            contagem += 1
        return contagem

    # --- Maintenance ---

    async def garbage_collect(self) -> int:
        cliente = self._obter_cliente()
        filtro = models.Filter(
            must=[
                models.FieldCondition(
                    key="kind", match=models.MatchValue(value=KIND_ENTRY)
                ),
                models.FieldCondition(
                    key="expires_at_ts",
                    range=models.Range(lte=_agora().timestamp()),
                ),
            ]
        )
        quantas = await cliente.count(
            collection_name=self._colecao, count_filter=filtro, exact=True
        )
        if quantas.count:
            await cliente.delete(
                collection_name=self._colecao,
                points_selector=models.FilterSelector(filter=filtro),
            )
        return int(quantas.count)
