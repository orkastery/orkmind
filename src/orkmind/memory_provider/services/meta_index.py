"""Meta-indice: o "indice de indices" que o agente navega antes de buscar.

Anti-esquecimento sem resumo: em vez de trocar conteudo real por uma sintese
vaga, o agente recebe um MAPA barato (dominio -> no tematico -> documentos) e
desce so ate onde precisa. A busca vetorial pesada vem depois, ja restrita a
uma subarvore (`MemoryScopeFilter.index_path`).

A navegacao respeita o mesmo RBAC da busca: no de nivel 0/1 tem classificacao
propria, apontador de nivel 2 herda a do documento, e um no so aparece se
todos os ancestrais aparecem E se houver ao menos um documento visivel na
subarvore dele. A segunda condicao cobre o eixo de departamento, que o no nao
tem: sem ela o NOC leria `RH/DESLIGAMENTOS_Q4` mesmo sem abrir documento algum.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from orkmind.memory_provider.models.schemas import (
    AccessLevel,
    DocumentPointer,
    MemoryScopeFilter,
    MetaIndexNode,
    validate_index_path,
)
from orkmind.memory_provider.services.scope import document_scope_args, document_scope_clause

if TYPE_CHECKING:  # pragma: no cover - so para anotacao
    import asyncpg

_BROWSE_SQL = f"""
WITH RECURSIVE tree AS (
    SELECT n.id, n.parent_id, n.level, n.key, n.path, n.title, n.description,
           n.position, n.document_id, 0 AS rel_depth
    FROM meta_index n
    WHERE (($1::text IS NULL AND n.level = 0) OR n.path = $1)
      AND n.level < 2
      AND n.access_level = ANY($3::text[])
      AND (n.parent_id IS NULL OR EXISTS (
            SELECT 1 FROM meta_index p
            WHERE p.id = n.parent_id AND p.access_level = ANY($3::text[])))
    UNION ALL
    SELECT c.id, c.parent_id, c.level, c.key, c.path, c.title, c.description,
           c.position, c.document_id, t.rel_depth + 1
    FROM meta_index c
    JOIN tree t ON c.parent_id = t.id
    WHERE t.rel_depth < $2
      AND (c.level = 2 OR c.access_level = ANY($3::text[]))
)
SELECT t.id, t.parent_id, t.level, t.key, t.path, t.title, t.description, t.position,
       t.document_id, d.slug, d.title AS doc_title, d.doc_type,
       d.version AS doc_version, d.updated_at AS doc_updated_at
FROM tree t
LEFT JOIN wiki_documents d ON d.id = t.document_id
WHERE t.level < 2 OR ({document_scope_clause("d", 3)})
ORDER BY t.level, t.position, t.key
"""

_COUNTS_SQL = f"""
SELECT theme.id AS theme_id, theme.parent_id AS domain_id, count(*) AS total
FROM meta_index pointer
JOIN meta_index theme ON theme.id = pointer.parent_id
JOIN wiki_documents d ON d.id = pointer.document_id
WHERE pointer.level = 2
  AND theme.access_level = ANY($1::text[])
  AND {document_scope_clause("d", 1)}
GROUP BY theme.id, theme.parent_id
"""

_UPSERT_SQL = """
INSERT INTO meta_index (parent_id, level, key, path, title, description, access_level, position)
VALUES ($1, $2, $3, $3, $4, $5, $6, $7)
ON CONFLICT ON CONSTRAINT meta_index_sibling_uq DO UPDATE
SET title = EXCLUDED.title, description = EXCLUDED.description,
    access_level = EXCLUDED.access_level, position = EXCLUDED.position
RETURNING id
"""

_ENSURE_SQL = """
INSERT INTO meta_index (parent_id, level, key, path, access_level)
VALUES ($1, $2, $3, $3, $4)
ON CONFLICT ON CONSTRAINT meta_index_sibling_uq DO NOTHING
"""


class MetaIndexService:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert_node(
        self,
        path: str,
        *,
        title: str = "",
        description: str = "",
        access_level: AccessLevel = AccessLevel.OPERATIONAL,
        position: int = 0,
    ) -> UUID:
        """Cria ou atualiza um dominio (`A`) ou no tematico (`A/B`)."""
        parts = validate_index_path(path).split("/")
        async with self._pool.acquire() as conn, conn.transaction():
            parent_id: UUID | None = None
            if len(parts) == 2:
                parent_id = await self._ensure_node(conn, None, 0, parts[0], access_level)
            node_id = await conn.fetchval(
                _UPSERT_SQL,
                parent_id,
                len(parts) - 1,
                parts[-1],
                title,
                description,
                access_level.value,
                position,
            )
        return UUID(str(node_id))

    async def link_document(
        self,
        conn: asyncpg.Connection,
        path: str,
        document_id: UUID,
        slug: str,
        access_level: AccessLevel,
    ) -> None:
        """Aponta `path` (DOMINIO/NO_TEMATICO) para o documento. Idempotente.

        No criado aqui nasce com a classificacao do DOCUMENTO: ingerir um
        estudo EXECUTIVE sob um projeto novo nao pode revelar o nome do
        projeto para um operacional. No que ja existe nao e tocado.
        """
        domain_key, theme_key = validate_index_path(path).split("/")
        domain_id = await self._ensure_node(conn, None, 0, domain_key, access_level)
        theme_id = await self._ensure_node(conn, domain_id, 1, theme_key, access_level)
        await conn.execute(
            """
            INSERT INTO meta_index (parent_id, level, key, path, document_id)
            VALUES ($1, 2, $2, $2, $3)
            ON CONFLICT ON CONSTRAINT meta_index_sibling_uq DO NOTHING
            """,
            theme_id,
            slug,
            document_id,
        )

    async def link_document_by_slug(self, path: str, slug: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, access_level FROM wiki_documents WHERE slug = $1", slug
            )
            if row is None:
                raise LookupError(f"documento inexistente: {slug!r}")
            await self.link_document(conn, path, row["id"], slug, AccessLevel(row["access_level"]))

    async def unlink_document(self, path: str, slug: str) -> bool:
        pointer_path = f"{validate_index_path(path)}/{slug}"
        status = await self._pool.execute(
            "DELETE FROM meta_index WHERE level = 2 AND path = $1", pointer_path
        )
        return bool(status.endswith(" 1"))

    async def browse(
        self, scope: MemoryScopeFilter, path: str | None = None, depth: int = 1
    ) -> list[MetaIndexNode]:
        """Arvore visivel para `scope`, a partir de `path` (None = dominios).

        `depth` = quantos niveis descer abaixo do ponto de partida.
        """
        if not 0 <= depth <= 2:
            raise ValueError("depth deve estar entre 0 e 2")
        anchor = None if path is None else validate_index_path(path)
        levels, cross, departments = document_scope_args(scope)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_BROWSE_SQL, anchor, depth, levels, cross, departments)
            counts = await conn.fetch(_COUNTS_SQL, levels, cross, departments)

        by_theme: dict[UUID, int] = {}
        by_domain: dict[UUID, int] = {}
        for count in counts:
            by_theme[count["theme_id"]] = count["total"]
            by_domain[count["domain_id"]] = by_domain.get(count["domain_id"], 0) + count["total"]

        visible_docs = {0: by_domain, 1: by_theme}
        nodes: dict[UUID, MetaIndexNode] = {}
        roots: list[MetaIndexNode] = []
        # ORDER BY level garante que o pai ja existe quando o filho chega.
        for row in rows:
            pointer = None
            if row["level"] == 2:
                document_count = 1
                pointer = DocumentPointer(
                    document_id=row["document_id"],
                    slug=row["slug"],
                    title=row["doc_title"],
                    doc_type=row["doc_type"],
                    version=row["doc_version"],
                    updated_at=row["doc_updated_at"],
                )
            else:
                document_count = visible_docs[row["level"]].get(row["id"], 0)
                if document_count == 0:
                    continue
            node = MetaIndexNode(
                id=row["id"],
                level=row["level"],
                key=row["key"],
                path=row["path"],
                title=row["title"],
                description=row["description"],
                document=pointer,
                document_count=document_count,
            )
            nodes[node.id] = node
            parent = nodes.get(row["parent_id"]) if row["parent_id"] else None
            (parent.children if parent else roots).append(node)
        return roots

    async def _ensure_node(
        self,
        conn: asyncpg.Connection,
        parent_id: UUID | None,
        level: int,
        key: str,
        access_level: AccessLevel,
    ) -> UUID:
        await conn.execute(_ENSURE_SQL, parent_id, level, key, access_level.value)
        node_id = await conn.fetchval(
            "SELECT id FROM meta_index WHERE parent_id IS NOT DISTINCT FROM $1 AND key = $2",
            parent_id,
            key,
        )
        return UUID(str(node_id))
