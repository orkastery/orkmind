"""PostgreSQL + pgvector adapter for OrkMind MemoryStore."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from pgvector.psycopg import register_vector_async  # type: ignore[import-untyped]
from psycopg.rows import dict_row

from orkmind.core.models import MemoryEntry, Profile, Source
from orkmind.core.ontology import (
    PERMISSION_MSG_WRITE,
    PROTECTION_MSG_DELETE,
    PROTECTION_MSG_UPDATE,
    ProtectionError,
)
from orkmind.store.base import MemoryStore
from orkmind.store.capabilities import StoreCapabilities

logger = logging.getLogger(__name__)

# F3.2: capacidades declaradas do backend pgvector. Valores da tabela 3.2
# do plano. E o unico backend com native_acl_filter e
# native_constitutional_order True, e e isso que garante que a camada de
# governanca nao faz over-fetch aqui: o SQL enviado continua identico ao
# de antes do ciclo (nao-regressao do default, DP-9).
PGVECTOR_CAPABILITIES = StoreCapabilities(
    backend="pgvector",
    vector_search=True,
    accepts_external_vectors=True,
    stores_embedding=True,
    native_text_semantic=False,
    text_search="ranked",
    tag_filter=True,
    unique_content_hash=True,
    versioning=True,
    snapshots=True,
    parent_id=True,
    ttl_gc=True,
    durable=True,
    native_acl_filter=True,
    native_constitutional_order=True,
    backfill=True,
)

SCHEMA_SQL = """\
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    collection TEXT NOT NULL,
    tags JSONB NOT NULL DEFAULT '{}',
    priority TEXT NOT NULL DEFAULT 'medium',
    mandatory BOOLEAN NOT NULL DEFAULT false,
    scope TEXT NOT NULL DEFAULT 'global',
    source TEXT NOT NULL DEFAULT 'human',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    embedding vector(1024),
    metadata JSONB NOT NULL DEFAULT '{}',
    protected BOOLEAN NOT NULL DEFAULT false,
    content_hash TEXT,
    injection_risk BOOLEAN NOT NULL DEFAULT false,
    conflict BOOLEAN NOT NULL DEFAULT false,
    url TEXT,
    visibility TEXT NOT NULL DEFAULT 'private',
    author_id TEXT,
    parent_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_memories_collection ON memories(collection);
CREATE INDEX IF NOT EXISTS idx_memories_mandatory ON memories(mandatory);
CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_memories_fts ON memories USING GIN (
    to_tsvector('english', content)
);

CREATE TABLE IF NOT EXISTS memory_versions (
    version_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    collection TEXT NOT NULL,
    tags JSONB NOT NULL DEFAULT '{}',
    priority TEXT NOT NULL DEFAULT 'medium',
    mandatory BOOLEAN NOT NULL DEFAULT false,
    scope TEXT NOT NULL DEFAULT 'global',
    source TEXT NOT NULL DEFAULT 'human',
    version INTEGER NOT NULL,
    content_hash TEXT,
    protected BOOLEAN NOT NULL DEFAULT false,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mv_memory_id ON memory_versions(memory_id);
CREATE INDEX IF NOT EXISTS idx_mv_last_accessed ON memory_versions(last_accessed_at);
"""

MIGRATION_SQL = """\
ALTER TABLE memories ADD COLUMN IF NOT EXISTS protected BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS injection_risk BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS conflict BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS idx_memories_protected ON memories(protected);
CREATE INDEX IF NOT EXISTS idx_memories_injection_risk ON memories(injection_risk);

-- D6: camadas de contexto
ALTER TABLE memories ADD COLUMN IF NOT EXISTS essence TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS structure TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS layer_generated_at TIMESTAMPTZ;

-- D7: snapshots globais
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    entry_count INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS snapshot_entries (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    entry_id TEXT NOT NULL,
    content_hash TEXT,
    entry_data JSONB NOT NULL,
    PRIMARY KEY (snapshot_id, entry_id)
);

CREATE INDEX IF NOT EXISTS idx_se_snapshot ON snapshot_entries(snapshot_id);

-- D8: encryption at-rest
ALTER TABLE memories ADD COLUMN IF NOT EXISTS encrypted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS encryption_meta JSONB;

-- F1: ontologia expandida
ALTER TABLE memories ADD COLUMN IF NOT EXISTS url TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private';
ALTER TABLE memories ADD COLUMN IF NOT EXISTS author_id TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS parent_id TEXT;

CREATE INDEX IF NOT EXISTS idx_memories_visibility ON memories(visibility);
CREATE INDEX IF NOT EXISTS idx_memories_author_id ON memories(author_id);
CREATE INDEX IF NOT EXISTS idx_memories_parent_id ON memories(parent_id);

-- F3: migrar embedding de vector(1536) para vector(1024)
DROP INDEX IF EXISTS idx_memories_embedding;
ALTER TABLE memories ALTER COLUMN embedding TYPE vector(1024)
    USING CASE
        WHEN embedding IS NULL THEN NULL
        ELSE embedding::text::vector(1024)
    END;

-- F2: perfis e controle de acesso
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    profile_type TEXT NOT NULL CHECK (profile_type IN (
        'person', 'agent', 'system', 'company', 'group'
    )),
    parent_id TEXT,
    permissions JSONB NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profiles_type ON profiles(profile_type);

-- F4: apoio ao lookup por content_hash (idempotencia da spool)
CREATE INDEX IF NOT EXISTS idx_memories_content_hash
ON memories (content_hash) WHERE content_hash IS NOT NULL;
"""

HNSW_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_memories_embedding
ON memories USING hnsw (embedding vector_cosine_ops);
"""

# F4: idempotencia dura. Fica separado de MIGRATION_SQL porque a criacao
# FALHA de proposito quando o banco ja tem duplicatas (collection,
# content_hash). Nesse caso apenas avisamos: resolver duplicata e decisao
# humana (a regra constitucional proibe DELETE automatico de memoria).
# O arquivo migrations/f4_content_hash_idempotencia.sql traz a consulta
# de diagnostico e e o caminho deliberado para bancos existentes.
IDEMPOTENCY_INDEX_SQL = """\
CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_collection_content_hash
ON memories (collection, content_hash) WHERE content_hash IS NOT NULL;
"""


def _coerce_embedding(value: Any) -> Optional[list[float]]:
    """Converte o valor de embedding (Vector, list, str, None) para list[float]."""
    if value is None:
        return None
    # pgvector Vector - expoe to_list()
    if hasattr(value, "to_list"):
        return value.to_list()
    # numpy array ou similar - expoe tolist()
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            return [float(x) for x in value[1:-1].split(",") if x.strip()]
    return None


def _row_to_entry(row: dict[str, Any]) -> MemoryEntry:
    tags = row["tags"] if isinstance(row["tags"], dict) else json.loads(row["tags"])
    meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
    embedding = _coerce_embedding(row.get("embedding"))
    return MemoryEntry(
        id=row["id"],
        content=row["content"],
        collection=row["collection"],
        tags=tags,
        priority=row["priority"],
        mandatory=row["mandatory"],
        scope=row["scope"],
        source=row["source"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row.get("expires_at"),
        embedding=embedding,
        metadata=meta,
        protected=row.get("protected", False),
        content_hash=row.get("content_hash"),
        injection_risk=row.get("injection_risk", False),
        conflict=row.get("conflict", False),
        essence=row.get("essence"),
        structure=row.get("structure"),
        layer_generated_at=row.get("layer_generated_at"),
        encrypted=row.get("encrypted", False),
        encryption_meta=row.get("encryption_meta"),
        url=row.get("url"),
        visibility=row.get("visibility") or "private",
        author_id=row.get("author_id"),
        parent_id=row.get("parent_id"),
    )



# F2: condicao SQL de controle de acesso de leitura.
# Uma entry e visivel ao requester quando e publica, quando ele e o
# autor, ou quando e restrita e ele consta na dimensao de tag `audience`.
_READ_ACL_SQL = """(
    visibility = 'public'
    OR (author_id IS NOT NULL AND author_id = ANY(%s))
    OR (
        visibility = 'restricted'
        AND jsonb_typeof(tags -> 'audience') = 'array'
        AND EXISTS (
            SELECT 1 FROM jsonb_array_elements_text(tags -> 'audience') AS aud
            WHERE aud = ANY(%s)
        )
    )
)"""


def _row_to_profile(row: dict[str, Any]) -> Profile:
    """Converte um row da tabela profiles em Profile."""
    permissions = (
        row["permissions"]
        if isinstance(row["permissions"], dict)
        else json.loads(row["permissions"])
    )
    metadata = (
        row["metadata"]
        if isinstance(row["metadata"], dict)
        else json.loads(row["metadata"])
    )
    return Profile(
        id=row["id"],
        display_name=row["display_name"],
        profile_type=row["profile_type"],
        parent_id=row.get("parent_id"),
        permissions=permissions,
        metadata=metadata,
        created_at=row["created_at"],
    )


class PostgresAdapter(MemoryStore):
    """PostgreSQL + pgvector storage adapter."""

    def __init__(self, database_url: str, embedding_dim: int = 1024) -> None:
        self._database_url = database_url
        self._embedding_dim = embedding_dim
        self._conn: Optional[psycopg.AsyncConnection[Any]] = None

    @property
    def capabilities(self) -> StoreCapabilities:
        return PGVECTOR_CAPABILITIES

    async def _get_conn(self) -> psycopg.AsyncConnection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(
                self._database_url, row_factory=dict_row, autocommit=True
            )
            await register_vector_async(self._conn)
        return self._conn

    async def initialize(self) -> None:
        conn = await self._get_conn()
        # Schema completo (para instalacoes novas)
        await conn.execute(SCHEMA_SQL)  # type: ignore[arg-type]
        # Migracao para instalacoes existentes (ADD COLUMN IF NOT EXISTS)
        await conn.execute(MIGRATION_SQL)  # type: ignore[arg-type]
        try:
            await conn.execute(HNSW_INDEX_SQL)  # type: ignore[arg-type]
        except psycopg.errors.UndefinedObject:
            logger.warning("HNSW index creation skipped (pgvector may need data first)")
        # F4: idempotencia dura por (collection, content_hash). Nunca
        # derruba o initialize: se ja houver duplicatas no banco, apenas
        # avisa e segue sem o indice (a desduplicacao e decisao humana).
        try:
            await conn.execute(IDEMPOTENCY_INDEX_SQL)  # type: ignore[arg-type]
        except psycopg.errors.UniqueViolation:
            logger.warning(
                "Indice unico (collection, content_hash) nao criado: ja existem "
                "duplicatas no banco. Rode a consulta de diagnostico em "
                "migrations/f4_content_hash_idempotencia.sql e resolva "
                "manualmente - nada e apagado automaticamente."
            )

    async def close(self) -> None:
        if self._conn and not self._conn.closed:
            await self._conn.close()
            self._conn = None

    # --- CRUD ---

    async def store(self, entry: MemoryEntry) -> str:
        conn = await self._get_conn()
        embedding_val = entry.embedding if entry.embedding else None
        await conn.execute(
            """
            INSERT INTO memories (id, content, collection, tags, priority,
                mandatory, scope, source, version, created_at, updated_at,
                expires_at, embedding, metadata,
                protected, content_hash, injection_risk, conflict,
                essence, structure, layer_generated_at,
                encrypted, encryption_meta,
                url, visibility, author_id, parent_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s)
            """,
            (
                entry.id,
                entry.content,
                entry.collection,
                json.dumps(entry.tags),
                entry.priority,
                entry.mandatory,
                entry.scope,
                entry.source,
                entry.version,
                entry.created_at,
                entry.updated_at,
                entry.expires_at,
                embedding_val,
                json.dumps(entry.metadata),
                entry.protected,
                entry.content_hash,
                entry.injection_risk,
                entry.conflict,
                entry.essence,
                entry.structure,
                entry.layer_generated_at,
                entry.encrypted,
                json.dumps(entry.encryption_meta) if entry.encryption_meta else None,
                entry.url,
                entry.visibility,
                entry.author_id,
                entry.parent_id,
            ),
        )
        return entry.id

    async def find_by_content_hash(
        self,
        content_hash: str,
        collection: Optional[str] = None,
    ) -> Optional[MemoryEntry]:
        """Busca a entry existente com o content_hash informado.

        Usada pelo drainer da spool para idempotencia: se ja existe
        entry com aquele hash, o item da fila e fechado com o entry_id
        real em vez de gerar uma duplicata.
        """
        if not content_hash:
            return None
        conn = await self._get_conn()
        if collection:
            cur = await conn.execute(
                "SELECT * FROM memories WHERE content_hash = %s AND collection = %s "
                "ORDER BY created_at LIMIT 1",
                (content_hash, collection),
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM memories WHERE content_hash = %s "
                "ORDER BY created_at LIMIT 1",
                (content_hash,),
            )
        row = await cur.fetchone()
        return _row_to_entry(row) if row else None

    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        conn = await self._get_conn()
        cur = await conn.execute(
            "SELECT * FROM memories WHERE id = %s", (entry_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_entry(row)

    async def update(
        self,
        entry_id: str,
        entry: MemoryEntry,
        requester_id: Optional[str] = None,
    ) -> bool:
        conn = await self._get_conn()
        # Verificar protecao da entry atual
        current = await self.retrieve(entry_id)
        if current and (current.protected or current.priority == "critical"):
            if entry.source == "agent":
                raise ProtectionError(
                    PROTECTION_MSG_UPDATE.format(id=entry_id)
                )
        # F2: ACL de escrita, avaliada apos a protecao D2
        if current:
            await self._assert_can_write(current, requester_id)
        # Arquivar versao atual em memory_versions (append-only)
        if current:
            await conn.execute(
                """
                INSERT INTO memory_versions (version_id, memory_id, content,
                    collection, tags, priority, mandatory, scope, source,
                    version, content_hash, protected, metadata,
                    created_at, archived_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                """,
                (
                    str(uuid.uuid4()),
                    entry_id,
                    current.content,
                    current.collection,
                    json.dumps(current.tags),
                    current.priority,
                    current.mandatory,
                    current.scope,
                    current.source,
                    current.version,
                    current.content_hash,
                    current.protected,
                    json.dumps(current.metadata),
                    current.created_at,
                ),
            )
        now = datetime.now(timezone.utc)
        embedding_val = entry.embedding if entry.embedding else None
        cur = await conn.execute(
            """
            UPDATE memories
            SET content = %s, collection = %s, tags = %s, priority = %s,
                mandatory = %s, scope = %s, source = %s,
                version = version + 1, updated_at = %s,
                expires_at = %s, embedding = %s, metadata = %s,
                protected = %s, content_hash = %s,
                injection_risk = %s, conflict = %s,
                essence = %s, structure = %s, layer_generated_at = %s,
                encrypted = %s, encryption_meta = %s,
                url = %s, visibility = %s, author_id = %s, parent_id = %s
            WHERE id = %s
            """,
            (
                entry.content,
                entry.collection,
                json.dumps(entry.tags),
                entry.priority,
                entry.mandatory,
                entry.scope,
                entry.source,
                now,
                entry.expires_at,
                embedding_val,
                json.dumps(entry.metadata),
                entry.protected,
                entry.content_hash,
                entry.injection_risk,
                entry.conflict,
                entry.essence,
                entry.structure,
                entry.layer_generated_at,
                entry.encrypted,
                json.dumps(entry.encryption_meta) if entry.encryption_meta else None,
                entry.url,
                entry.visibility,
                entry.author_id,
                entry.parent_id,
                entry_id,
            ),
        )
        return cur.rowcount > 0

    async def delete(
        self,
        entry_id: str,
        source: Source = "human",
        requester_id: Optional[str] = None,
    ) -> bool:
        conn = await self._get_conn()
        # Verificar protecao
        current = await self.retrieve(entry_id)
        if current and (current.protected or current.priority == "critical"):
            if source == "agent":
                raise ProtectionError(
                    PROTECTION_MSG_DELETE.format(id=entry_id)
                )
        # F2: ACL de escrita, avaliada apos a protecao D2
        if current:
            await self._assert_can_write(current, requester_id)
        cur = await conn.execute(
            "DELETE FROM memories WHERE id = %s", (entry_id,)
        )
        return cur.rowcount > 0

    # --- Controle de acesso (F2) ---

    async def _is_member_of(self, requester_id: str, group_id: str) -> bool:
        """Verifica se o requester e membro de um grupo (1 nivel).

        Grupos aninhados nao sao resolvidos: a associacao e direta,
        lida de metadata.members do perfil do grupo.
        """
        profile = await self.profile_get(group_id)
        if not profile or profile.profile_type != "group":
            return False
        members = profile.metadata.get("members", [])
        if not isinstance(members, list):
            return False
        return requester_id in members

    async def resolve_identities(self, requester_id: str) -> list[str]:
        """Identidades efetivas do requester: ele mesmo e seus grupos.

        A tabela profiles e pequena, entao os grupos sao varridos em
        memoria (1 nivel, sem recursao).
        """
        identities = [requester_id]
        try:
            grupos = await self.profile_list(profile_type="group")
        except Exception as e:
            logger.debug("Falha ao resolver grupos de '%s': %s", requester_id, e)
            return identities
        for grupo in grupos:
            members = grupo.metadata.get("members", [])
            if isinstance(members, list) and requester_id in members:
                identities.append(grupo.id)
        return identities

    async def _read_acl(
        self, requester_id: Optional[str]
    ) -> tuple[str, list[Any]]:
        """Monta a condicao SQL de leitura para um requester.

        Sem requester_id devolve condicao vazia: o comportamento
        anterior a F2 e preservado e a query retorna tudo.
        """
        if not requester_id:
            return "", []
        identities = await self.resolve_identities(requester_id)
        return _READ_ACL_SQL, [identities, identities]

    def can_read(
        self,
        entry: MemoryEntry,
        requester_id: Optional[str],
        identities: Optional[list[str]] = None,
    ) -> bool:
        """Avalia em memoria a mesma regra de leitura aplicada no SQL."""
        if not requester_id:
            return True
        if entry.visibility == "public":
            return True
        efetivas = set(identities or [requester_id])
        if entry.author_id and entry.author_id in efetivas:
            return True
        if entry.visibility == "restricted":
            audience = entry.tags.get("audience") or []
            if efetivas & set(audience):
                return True
        return False

    async def _assert_can_write(
        self, entry: MemoryEntry, requester_id: Optional[str]
    ) -> None:
        """Valida a permissao de escrita do requester sobre uma entry.

        Regras (F2): o autor sempre pode escrever; quem estiver na
        dimensao de tag `editors` tambem pode; o coringa '*' libera
        para todos. Sem requester_id o comportamento anterior e
        preservado.

        A protecao D2 (protected/critical) e verificada ANTES e
        PREVALECE: estar em editors nao autoriza alterar entry
        protegida.
        """
        if not requester_id:
            return
        identities = set(await self.resolve_identities(requester_id))
        if entry.author_id and entry.author_id in identities:
            return
        editors = entry.tags.get("editors") or []
        if "*" in editors or identities & set(editors):
            return
        raise PermissionError(
            PERMISSION_MSG_WRITE.format(requester=requester_id, id=entry.id)
        )

    # --- Search ---

    async def search_by_tags(
        self,
        tags: dict[str, list[str]],
        collection: Optional[str] = None,
        mandatory_only: bool = False,
        limit: int = 50,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        conn = await self._get_conn()
        conditions: list[str] = []
        params: list[Any] = []

        # F2: filtro de acesso de leitura. Sem requester_id o
        # comportamento anterior e preservado (retorna tudo).
        acl_condition, acl_params = await self._read_acl(requester_id)
        if acl_condition:
            conditions.append(acl_condition)
            params.extend(acl_params)

        # Build tag containment conditions (exact match)
        for dim, values in tags.items():
            conditions.append("tags @> %s")
            params.append(json.dumps({dim: values}))

        if collection:
            conditions.append("collection = %s")
            params.append(collection)

        if mandatory_only:
            conditions.append("mandatory = true")

        # Excluir entries com risco de injection (so aparecem em busca explicita)
        conditions.append("injection_risk = false")

        # Always include non-expired
        conditions.append("(expires_at IS NULL OR expires_at > now())")

        where = " AND ".join(conditions) if conditions else "TRUE"
        query = f"""
            SELECT * FROM memories
            WHERE {where}
            ORDER BY
                mandatory DESC,
                CASE priority
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                END,
                updated_at DESC,
                id ASC
            LIMIT %s
        """
        params.append(limit)
        cur = await conn.execute(query, params)
        rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def search_by_text(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        conn = await self._get_conn()
        conditions = [
            "to_tsvector('english', content) @@ plainto_tsquery('english', %s)",
            "(expires_at IS NULL OR expires_at > now())",
        ]
        params: list[Any] = [query]

        acl_condition, acl_params = await self._read_acl(requester_id)
        if acl_condition:
            conditions.append(acl_condition)
            params.extend(acl_params)

        if collection:
            conditions.append("collection = %s")
            params.append(collection)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT *, ts_rank(
                to_tsvector('english', content),
                plainto_tsquery('english', %s)
            ) AS rank
            FROM memories
            WHERE {where}
            ORDER BY mandatory DESC, rank DESC, id ASC
            LIMIT %s
        """
        params_full = [query] + params + [limit]
        cur = await conn.execute(sql, params_full)
        rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def search_semantic(
        self,
        embedding: list[float],
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        conn = await self._get_conn()
        conditions = [
            "embedding IS NOT NULL",
            "(expires_at IS NULL OR expires_at > now())",
        ]
        params: list[Any] = []

        acl_condition, acl_params = await self._read_acl(requester_id)
        if acl_condition:
            conditions.append(acl_condition)
            params.extend(acl_params)

        if collection:
            conditions.append("collection = %s")
            params.append(collection)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT *, embedding <=> %s::vector AS distance
            FROM memories
            WHERE {where}
            ORDER BY mandatory DESC, distance ASC, id ASC
            LIMIT %s
        """
        params_full: list[Any] = [embedding] + params + [limit]
        cur = await conn.execute(sql, params_full)
        rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    # --- Collections ---

    async def list_collections(self) -> list[str]:
        conn = await self._get_conn()
        cur = await conn.execute(
            "SELECT DISTINCT collection FROM memories ORDER BY collection"
        )
        rows = await cur.fetchall()
        return [r["collection"] for r in rows]

    async def count(self, collection: Optional[str] = None) -> int:
        conn = await self._get_conn()
        if collection:
            cur = await conn.execute(
                "SELECT COUNT(*) AS cnt FROM memories WHERE collection = %s",
                (collection,),
            )
        else:
            cur = await conn.execute("SELECT COUNT(*) AS cnt FROM memories")
        row = await cur.fetchone()
        return row["cnt"] if row else 0

    # --- Perfis (F2) ---

    async def profile_create(self, profile: Profile) -> str:
        """Cria (ou substitui) um perfil. Retorna o id."""
        conn = await self._get_conn()
        await conn.execute(
            """
            INSERT INTO profiles (id, display_name, profile_type, parent_id,
                permissions, metadata, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                profile_type = EXCLUDED.profile_type,
                parent_id = EXCLUDED.parent_id,
                permissions = EXCLUDED.permissions,
                metadata = EXCLUDED.metadata
            """,
            (
                profile.id,
                profile.display_name,
                profile.profile_type,
                profile.parent_id,
                json.dumps(profile.permissions),
                json.dumps(profile.metadata),
                profile.created_at,
            ),
        )
        return profile.id

    async def profile_get(self, profile_id: str) -> Optional[Profile]:
        """Busca um perfil por id."""
        conn = await self._get_conn()
        cur = await conn.execute(
            "SELECT * FROM profiles WHERE id = %s", (profile_id,)
        )
        row = await cur.fetchone()
        return _row_to_profile(row) if row else None

    async def profile_list(
        self, profile_type: Optional[str] = None
    ) -> list[Profile]:
        """Lista perfis, opcionalmente filtrados por tipo."""
        conn = await self._get_conn()
        if profile_type:
            cur = await conn.execute(
                "SELECT * FROM profiles WHERE profile_type = %s ORDER BY id",
                (profile_type,),
            )
        else:
            cur = await conn.execute("SELECT * FROM profiles ORDER BY id")
        rows = await cur.fetchall()
        return [_row_to_profile(r) for r in rows]

    async def profile_update(self, profile_id: str, profile: Profile) -> bool:
        """Atualiza um perfil existente. Retorna False se nao existir."""
        conn = await self._get_conn()
        cur = await conn.execute(
            """
            UPDATE profiles
            SET display_name = %s, profile_type = %s, parent_id = %s,
                permissions = %s, metadata = %s
            WHERE id = %s
            """,
            (
                profile.display_name,
                profile.profile_type,
                profile.parent_id,
                json.dumps(profile.permissions),
                json.dumps(profile.metadata),
                profile_id,
            ),
        )
        return cur.rowcount > 0

    async def profile_delete(self, profile_id: str) -> bool:
        """Remove um perfil. Retorna False se nao existir."""
        conn = await self._get_conn()
        cur = await conn.execute(
            "DELETE FROM profiles WHERE id = %s", (profile_id,)
        )
        return cur.rowcount > 0

    async def profile_get_members(self, group_id: str) -> list[str]:
        """Ids dos membros de um grupo (metadata.members)."""
        profile = await self.profile_get(group_id)
        if not profile or profile.profile_type != "group":
            return []
        members = profile.metadata.get("members", [])
        if not isinstance(members, list):
            return []
        return [str(m) for m in members]

    # --- Hierarquia (F1) ---

    async def get_children(self, parent_id: str) -> list[MemoryEntry]:
        """Entries cujo parent_id aponta para a entry informada.

        Permite navegar a hierarquia criada pelo campo parent_id
        (ex: pacotes semanticos vinculados a uma sessao).
        """
        conn = await self._get_conn()
        cur = await conn.execute(
            """
            SELECT * FROM memories
            WHERE parent_id = %s
              AND (expires_at IS NULL OR expires_at > now())
            ORDER BY created_at ASC, id ASC
            """,
            (parent_id,),
        )
        rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    # --- Backfill de embeddings (F3.6) ---

    async def list_entries_without_embedding(
        self,
        collection: Optional[str] = None,
        limit: int = 1000,
    ) -> list[MemoryEntry]:
        """Entries sem vetor, em ordem determinista (created_at, id).

        Operacao de manutencao do operador: nao aplica ACL nem filtro de
        injection_risk. Entries expiradas ficam de fora.
        """
        conn = await self._get_conn()
        conditions = [
            "embedding IS NULL",
            "(expires_at IS NULL OR expires_at > now())",
        ]
        params: list[Any] = []
        if collection:
            conditions.append("collection = %s")
            params.append(collection)
        where = " AND ".join(conditions)
        params.append(limit)
        cur = await conn.execute(
            f"""
            SELECT * FROM memories
            WHERE {where}
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            """,
            params,
        )
        rows = await cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    async def set_embedding(self, entry_id: str, embedding: list[float]) -> bool:
        """Grava SOMENTE o vetor. Nao versiona, nao mexe em updated_at.

        Preserva o comportamento que o backfill tinha quando falava SQL
        direto: backfill nao e edicao de conteudo.
        """
        if len(embedding) != self._embedding_dim:
            raise ValueError(
                f"Embedding com {len(embedding)} dimensoes; o backend "
                f"'pgvector' espera {self._embedding_dim}."
            )
        conn = await self._get_conn()
        cur = await conn.execute(
            "UPDATE memories SET embedding = %s WHERE id = %s",
            (embedding, entry_id),
        )
        return cur.rowcount > 0

    # --- Versionamento ---

    async def get_history(self, entry_id: str) -> list[MemoryEntry]:
        conn = await self._get_conn()
        cur = await conn.execute(
            """
            SELECT * FROM memory_versions
            WHERE memory_id = %s
            ORDER BY version DESC
            """,
            (entry_id,),
        )
        rows = await cur.fetchall()
        if rows:
            # Atualizar last_accessed_at das versoes retornadas
            await conn.execute(
                "UPDATE memory_versions SET last_accessed_at = now() WHERE memory_id = %s",
                (entry_id,),
            )
        return [self._version_row_to_entry(r) for r in rows]

    def _version_row_to_entry(self, row: dict[str, Any]) -> MemoryEntry:
        """Converte um row de memory_versions para MemoryEntry."""
        tags = row["tags"] if isinstance(row["tags"], dict) else json.loads(row["tags"])
        meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        return MemoryEntry(
            id=row["memory_id"],
            content=row["content"],
            collection=row["collection"],
            tags=tags,
            priority=row["priority"],
            mandatory=row["mandatory"],
            scope=row["scope"],
            source=row["source"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row.get("archived_at", row["created_at"]),
            content_hash=row.get("content_hash"),
            protected=row.get("protected", False),
            metadata=meta,
        )

    async def gc_versions(self, max_age_days: int = 365) -> int:
        conn = await self._get_conn()
        cur = await conn.execute(
            "DELETE FROM memory_versions WHERE last_accessed_at < now() - make_interval(days => %s)",
            (max_age_days,),
        )
        return cur.rowcount

    # --- Snapshots ---

    async def snapshot_commit(self, label: str, message: str = "") -> str:
        conn = await self._get_conn()
        snapshot_id = str(uuid.uuid4())
        # Buscar todas as entries ativas
        cur = await conn.execute("SELECT * FROM memories")
        rows = await cur.fetchall()
        entry_count = len(rows)

        await conn.execute(
            """
            INSERT INTO snapshots (id, label, message, entry_count, created_at)
            VALUES (%s, %s, %s, %s, now())
            """,
            (snapshot_id, label, message, entry_count),
        )

        for row in rows:
            entry = _row_to_entry(row)
            entry_data = entry.model_dump(mode="json")
            # Preservar embedding como lista no JSONB
            if entry.embedding:
                entry_data["embedding"] = entry.embedding
            await conn.execute(
                """
                INSERT INTO snapshot_entries (snapshot_id, entry_id, content_hash, entry_data)
                VALUES (%s, %s, %s, %s)
                """,
                (snapshot_id, entry.id, entry.content_hash, json.dumps(entry_data)),
            )
        return snapshot_id

    async def snapshot_log(self, limit: int = 20) -> list[dict]:
        conn = await self._get_conn()
        cur = await conn.execute(
            """
            SELECT id, label, message, entry_count, created_at
            FROM snapshots ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def snapshot_show(self, snapshot_id: str) -> dict:
        conn = await self._get_conn()
        cur = await conn.execute(
            "SELECT * FROM snapshots WHERE id = %s", (snapshot_id,)
        )
        snap_row = await cur.fetchone()
        if snap_row is None:
            raise ValueError(f"Snapshot '{snapshot_id}' nao encontrado")

        cur = await conn.execute(
            "SELECT entry_id, content_hash, entry_data FROM snapshot_entries WHERE snapshot_id = %s",
            (snapshot_id,),
        )
        entry_rows = await cur.fetchall()
        entries = []
        for r in entry_rows:
            data = r["entry_data"] if isinstance(r["entry_data"], dict) else json.loads(r["entry_data"])
            entries.append({
                "entry_id": r["entry_id"],
                "content_hash": r["content_hash"],
                "entry_data": data,
            })
        return {
            "id": snap_row["id"],
            "label": snap_row["label"],
            "message": snap_row["message"],
            "entry_count": snap_row["entry_count"],
            "created_at": snap_row["created_at"],
            "entries": entries,
        }

    async def snapshot_diff(self, snap_a: str, snap_b: str) -> dict:
        conn = await self._get_conn()
        # Verificar existencia
        for sid in (snap_a, snap_b):
            cur = await conn.execute("SELECT id FROM snapshots WHERE id = %s", (sid,))
            if await cur.fetchone() is None:
                raise ValueError(f"Snapshot '{sid}' nao encontrado")

        # Buscar entries de cada snapshot
        cur_a = await conn.execute(
            "SELECT entry_id, content_hash FROM snapshot_entries WHERE snapshot_id = %s",
            (snap_a,),
        )
        entries_a = {r["entry_id"]: r["content_hash"] for r in await cur_a.fetchall()}

        cur_b = await conn.execute(
            "SELECT entry_id, content_hash FROM snapshot_entries WHERE snapshot_id = %s",
            (snap_b,),
        )
        entries_b = {r["entry_id"]: r["content_hash"] for r in await cur_b.fetchall()}

        ids_a = set(entries_a.keys())
        ids_b = set(entries_b.keys())

        added = sorted(ids_b - ids_a)
        removed = sorted(ids_a - ids_b)
        modified = sorted(
            eid for eid in ids_a & ids_b if entries_a[eid] != entries_b[eid]
        )
        return {"added": added, "removed": removed, "modified": modified}

    async def snapshot_restore(self, snapshot_id: str) -> int:
        conn = await self._get_conn()
        # Verificar existencia
        cur = await conn.execute(
            "SELECT id FROM snapshots WHERE id = %s", (snapshot_id,)
        )
        if await cur.fetchone() is None:
            raise ValueError(f"Snapshot '{snapshot_id}' nao encontrado")

        # Auto-backup antes de restaurar
        await self.snapshot_commit(label="auto-backup-pre-restore")

        # Deletar todas as entries atuais
        await conn.execute("DELETE FROM memories")

        # Restaurar entries do snapshot
        cur = await conn.execute(
            "SELECT entry_data FROM snapshot_entries WHERE snapshot_id = %s",
            (snapshot_id,),
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            data = row["entry_data"] if isinstance(row["entry_data"], dict) else json.loads(row["entry_data"])
            entry = MemoryEntry(**data)
            await self.store(entry)
            count += 1
        return count

    # --- Maintenance ---

    async def garbage_collect(self) -> int:
        conn = await self._get_conn()
        cur = await conn.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= now()"
        )
        return cur.rowcount
