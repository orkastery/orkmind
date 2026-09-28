"""A teia: links entre documentos, backlinks e tags semanticas.

Um vault vale pelas arestas, nao so pelos textos. Este modulo mantem as duas
tabelas derivadas (`wiki_links`, `wiki_document_tags`) e as consultas que o
agente usa para navegar.

Backlink e superficie de vazamento
----------------------------------
A consulta de backlinks filtra o documento de ORIGEM pelo escopo de quem
pergunta, nao o de destino. Sem isso, um documento publico entregaria a
existencia de um confidencial que aponta para ele - o conteudo ficaria
protegido e a relacao, nao. O mesmo pre-filtro de `services/scope.py` vale
aqui, aplicado do outro lado da aresta.

Resolucao tardia
----------------
Link para nota que ainda nao existe e uso legitimo. Ele nasce com
`target_document_id` nulo e se resolve sozinho quando a nota aparece
(`resolve_pending`). O inverso tambem importa: a lista de alvos nao resolvidos
e um mapa do que a organizacao ja citou mas nunca escreveu.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from uuid import UUID

from orkmind.memory_provider.models.schemas import MemoryScopeFilter
from orkmind.memory_provider.services.notes import NoteLink
from orkmind.memory_provider.services.scope import document_scope_args, document_scope_clause

# Mesmo formato do CHECK da tabela: tag invalida e descartada, nao derruba a
# ingestao inteira de um documento por causa de um `#` solto.
_TAG_VALIDA = re.compile(r"[a-z0-9][a-z0-9_/-]*")

if TYPE_CHECKING:  # pragma: no cover - so para anotacao
    import asyncpg

_INSERT_LINK = """
INSERT INTO wiki_links
    (source_document_id, target_slug, target_document_id, kind, alias, anchor, context, position)
VALUES ($1, $2, (SELECT id FROM wiki_documents WHERE slug = $2), $3, $4, $5, $6, $7)
ON CONFLICT ON CONSTRAINT wiki_links_uq DO UPDATE
SET target_document_id = EXCLUDED.target_document_id,
    alias = EXCLUDED.alias, context = EXCLUDED.context, position = EXCLUDED.position
"""

# Backlinks: o WHERE recai sobre `origem`, o documento que APONTA.
_BACKLINKS_SQL = f"""
SELECT origem.slug, origem.title, origem.doc_type, l.kind, l.alias, l.anchor, l.context
FROM wiki_links l
JOIN wiki_documents alvo ON alvo.id = l.target_document_id
JOIN wiki_documents origem ON origem.id = l.source_document_id
WHERE (alvo.slug = $1::text OR alvo.id::text = $1::text)
  AND {document_scope_clause("origem", 2)}
ORDER BY origem.slug, l.position
LIMIT $5
"""

_OUTGOING_SQL = f"""
SELECT l.target_slug, l.kind, l.alias, l.anchor, l.context,
       alvo.title AS target_title,
       (l.target_document_id IS NOT NULL AND alvo.id IS NOT NULL) AS resolvido
FROM wiki_links l
JOIN wiki_documents origem ON origem.id = l.source_document_id
LEFT JOIN wiki_documents alvo
       ON alvo.id = l.target_document_id AND {document_scope_clause("alvo", 2)}
WHERE (origem.slug = $1::text OR origem.id::text = $1::text)
  AND {document_scope_clause("origem", 2)}
ORDER BY l.position
LIMIT $5
"""

# Alvos citados que nunca viraram documento: o mapa do que falta escrever.
_UNRESOLVED_SQL = f"""
SELECT l.target_slug, count(*) AS citacoes,
       min(origem.slug) AS primeiro_citador
FROM wiki_links l
JOIN wiki_documents origem ON origem.id = l.source_document_id
WHERE l.target_document_id IS NULL
  AND {document_scope_clause("origem", 1)}
GROUP BY l.target_slug
ORDER BY citacoes DESC, l.target_slug
LIMIT $4
"""

_LIST_TAGS_SQL = f"""
SELECT t.tag, count(*) AS documentos
FROM wiki_document_tags t
JOIN wiki_documents d ON d.id = t.document_id
WHERE {document_scope_clause("d", 1)}
  AND ($4::text IS NULL OR t.tag = $4::text OR t.tag LIKE $4::text || '/%')
GROUP BY t.tag
ORDER BY documentos DESC, t.tag
LIMIT $5
"""

_BY_TAG_SQL = f"""
SELECT DISTINCT d.slug, d.title, d.doc_type, d.updated_at
FROM wiki_document_tags t
JOIN wiki_documents d ON d.id = t.document_id
WHERE (t.tag = $4::text OR t.tag LIKE $4::text || '/%')
  AND {document_scope_clause("d", 1)}
ORDER BY d.updated_at DESC
LIMIT $5
"""


async def replace_links(conn: asyncpg.Connection, document_id: UUID, links: list[NoteLink]) -> int:
    """Troca as arestas que saem do documento. Derivadas: refazer e seguro."""
    await conn.execute("DELETE FROM wiki_links WHERE source_document_id = $1", document_id)
    if not links:
        return 0
    await conn.executemany(
        _INSERT_LINK,
        [(document_id, x.target, x.kind, x.alias, x.anchor, x.context, x.position) for x in links],
    )
    return len(links)


async def replace_tags(conn: asyncpg.Connection, document_id: UUID, tags: dict[str, str]) -> int:
    """Troca as tags do documento. `tags` mapeia tag -> origem (frontmatter/inline/ingest)."""
    await conn.execute("DELETE FROM wiki_document_tags WHERE document_id = $1", document_id)
    validas = {t: o for t, o in tags.items() if _TAG_VALIDA.fullmatch(t)}
    if validas:
        await conn.executemany(
            "INSERT INTO wiki_document_tags (document_id, tag, source) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING",
            [(document_id, t, o) for t, o in validas.items()],
        )
    return len(validas)


async def resolve_pending(conn: asyncpg.Connection, slug: str, document_id: UUID) -> int:
    """Liga as arestas que ja apontavam para este slug antes de ele existir."""
    status = await conn.execute(
        "UPDATE wiki_links SET target_document_id = $2 "
        "WHERE target_slug = $1 AND target_document_id IS NULL",
        slug,
        document_id,
    )
    return int(status.rsplit(" ", 1)[-1]) if status.startswith("UPDATE") else 0


class LinkService:
    """Consultas de navegacao da teia, sempre sob o escopo de quem pergunta."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def backlinks(
        self, slug_or_id: str | UUID, scope: MemoryScopeFilter, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Quem aponta para este documento - so a partir do que o escopo ve."""
        rows = await self._pool.fetch(
            _BACKLINKS_SQL, str(slug_or_id), *document_scope_args(scope), limit
        )
        return [dict(r) for r in rows]

    async def outgoing(
        self, slug_or_id: str | UUID, scope: MemoryScopeFilter, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Para onde este documento aponta, com marca de resolvido ou pendente."""
        rows = await self._pool.fetch(
            _OUTGOING_SQL, str(slug_or_id), *document_scope_args(scope), limit
        )
        return [dict(r) for r in rows]

    async def unresolved(self, scope: MemoryScopeFilter, limit: int = 50) -> list[dict[str, Any]]:
        """Alvos citados que nunca viraram documento: o que falta escrever."""
        rows = await self._pool.fetch(_UNRESOLVED_SQL, *document_scope_args(scope), limit)
        return [dict(r) for r in rows]

    async def list_tags(
        self, scope: MemoryScopeFilter, prefix: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Todas as tags visiveis com contagem; `prefix` desce na hierarquia."""
        rows = await self._pool.fetch(_LIST_TAGS_SQL, *document_scope_args(scope), prefix, limit)
        return [dict(r) for r in rows]

    async def by_tag(
        self, tag: str, scope: MemoryScopeFilter, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Documentos com a tag, incluindo as filhas (`rede` traz `rede/backbone`)."""
        rows = await self._pool.fetch(_BY_TAG_SQL, *document_scope_args(scope), tag, limit)
        return [dict(r) for r in rows]
