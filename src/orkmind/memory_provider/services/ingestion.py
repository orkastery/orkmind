"""Pipeline de ingestao da Wiki Memory.

Fluxo de um documento:

1. SHA-256 do conteudo normalizado. Hash ja presente e sem `force_update`
   aborta ANTES de fatiar ou chamar a API de embedding: duplicata nao custa
   nada e nao suja o espaco vetorial.
2. Chunking (em thread, para nao segurar o event loop) e embeddings em lote,
   FORA de transacao: nenhuma conexao fica presa esperando HTTP.
3. Uma transacao curta grava tudo ou nada: documento (nova revisao), troca
   dos chunks em lote e apontadores do meta-indice. Advisory locks por slug e
   por hash serializam ingestoes concorrentes do mesmo documento, e a decisao
   do passo 1 e REFEITA dentro do lock.

`submit` enfileira e devolve na hora: o canal de mensageria responde ao
usuario enquanto o worker processa. A fila vive em memoria; se o processo
cair, o chamador reenvia e a idempotencia por hash garante que reenviar e
seguro.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from orkmind.embeddings.provider import EmbeddingProvider
from orkmind.memory_provider.config import MemoryProviderSettings
from orkmind.memory_provider.embeddings import embed_texts
from orkmind.memory_provider.errors import (
    ClassificationOutOfReachError,
    IngestionQueueFullError,
    SlugUnavailableError,
)
from orkmind.memory_provider.models.schemas import (
    ChunkMetadata,
    DocumentIngestRequest,
    IngestJob,
    IngestJobState,
    IngestResult,
    IngestStatus,
    MemoryScopeFilter,
)
from orkmind.memory_provider.services.chunking import Chunk, chunk_document
from orkmind.memory_provider.services.links import replace_links, replace_tags, resolve_pending
from orkmind.memory_provider.services.meta_index import MetaIndexService
from orkmind.memory_provider.services.notes import ParsedNote, parse_note
from orkmind.memory_provider.services.scope import (
    classification_reach_sql,
    document_scope_args,
    document_scope_clause,
)

if TYPE_CHECKING:  # pragma: no cover - so para anotacao
    import asyncpg

logger = logging.getLogger(__name__)

# Jobs finalizados mantidos para consulta de status.
_FINISHED_JOBS_KEPT = 1024

_SELECT_DOC = """
SELECT id, slug, title, doc_type, source_url_or_path, content_format, content_hash,
       version, access_level, department_scope, status, metadata, chunk_count
FROM wiki_documents
"""

# Duplicata por conteudo, so entre o que quem escreve alcanca. Fora do alcance
# ela nao existe para ele: apontar para la entregaria o slug de um documento
# sigiloso, e recusar a gravacao deixaria o escritor sem o proprio documento.
_SELECT_DUP_IN_REACH = f"""
SELECT d.slug
FROM wiki_documents d
WHERE d.content_hash = $4 AND {document_scope_clause("d", 1)}
ORDER BY d.created_at
LIMIT 1
"""

_REACH_SQL = classification_reach_sql(1)

_INSERT_CHUNK = """
INSERT INTO wiki_chunks
    (document_id, chunk_index, heading_path, content, token_count, embedding, metadata_tags)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""


class _Action(Enum):
    CREATE = "create"
    UPDATE = "update"
    METADATA = "metadata"
    DUPLICATE = "duplicate"


@dataclass
class _Decision:
    action: _Action
    existing: asyncpg.Record | None = None
    duplicate_of: str | None = None


@dataclass
class _Prepared:
    chunks: list[Chunk]
    vectors: list[list[float]]
    note: ParsedNote | None = None


def _document_metadata(request: DocumentIngestRequest) -> dict[str, Any]:
    return {
        "tags": list(request.tags),
        "chunk_tags": dict(request.metadata_tags),
        "extra": dict(request.metadata),
    }


def _chunk_tags(request: DocumentIngestRequest, chunk: Chunk, total: int) -> dict[str, Any]:
    return ChunkMetadata(
        slug=request.slug,
        doc_type=request.doc_type,
        heading_path=list(chunk.heading_path),
        section_level=chunk.section_level,
        chunk_index=chunk.index,
        total_chunks=total,
        has_code=chunk.has_code,
        has_table=chunk.has_table,
        has_list=chunk.has_list,
        tags=list(request.tags),
        **request.metadata_tags,
    ).model_dump(mode="json")


class IngestionService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        embedder: EmbeddingProvider,
        settings: MemoryProviderSettings,
        meta_index: MetaIndexService,
        *,
        embedding_label: str,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        self._settings = settings
        self._meta_index = meta_index
        self._embedding_label = embedding_label
        # Avisado DEPOIS do commit de qualquer escrita (ex.: invalidar caches de busca).
        self._on_change = on_change or (lambda: None)
        self._queue: asyncio.Queue[tuple[IngestJob, DocumentIngestRequest]] = asyncio.Queue(
            maxsize=settings.ingest_queue_size
        )
        self._jobs: OrderedDict[UUID, IngestJob] = OrderedDict()
        self._workers: list[asyncio.Task[None]] = []

    # -- ingestao direta ------------------------------------------------------

    async def ingest(
        self, request: DocumentIngestRequest, *, writer: MemoryScopeFilter | None = None
    ) -> IngestResult:
        """Grava o documento e devolve o que aconteceu.

        `writer` e o escopo de quem escreve. Com ele, a escrita segue a regra da
        leitura: a classificacao pedida tem de caber no alcance do escritor, o
        slug nao pode pertencer a documento que ele nao le e a deduplicacao por
        conteudo so olha o que ele alcanca. Sem `writer` (ingestao de sistema:
        vault, fila, CLI) nada muda.
        """
        started = perf_counter()
        content_hash = request.content_hash
        prepared: _Prepared | None = None

        while True:
            async with self._pool.acquire() as conn:
                decision = await self._decide(conn, request, content_hash, writer)
            if decision.action is _Action.DUPLICATE:
                return self._duplicate(decision, content_hash, started)
            if decision.action in (_Action.CREATE, _Action.UPDATE) and prepared is None:
                prepared = await self._prepare(request)

            async with self._pool.acquire() as conn, conn.transaction():
                # Sempre slug antes de hash: duas classes de lock tomadas na
                # mesma ordem nao fecham ciclo.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"wiki:slug:{request.slug}",
                )
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"wiki:hash:{content_hash}",
                )
                decision = await self._decide(conn, request, content_hash, writer)
                if decision.action is _Action.DUPLICATE:
                    return self._duplicate(decision, content_hash, started)
                if decision.action is not _Action.METADATA and prepared is None:
                    # Alguem mudou o documento entre a leitura e o lock, e
                    # agora precisamos de embeddings que nao calculamos.
                    continue
                result = await self._apply(conn, request, content_hash, decision, prepared, started)
            self._on_change()
            return result

    async def retire(self, slug: str, *, actor: str) -> bool:
        """Aposenta o documento: sai da busca, o conteudo integro permanece."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"wiki:slug:{slug}"
            )
            document_id = await conn.fetchval(
                """
                UPDATE wiki_documents
                SET status = 'retired', version = version + 1, chunk_count = 0, updated_by = $2
                WHERE slug = $1 AND status = 'active'
                RETURNING id
                """,
                slug,
                actor,
            )
            if document_id is None:
                return False
            await conn.execute("DELETE FROM wiki_chunks WHERE document_id = $1", document_id)
        self._on_change()
        return True

    # -- fila assincrona ------------------------------------------------------

    def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(), name=f"orkmind-ingest-{n}")
            for n in range(self._settings.ingest_workers)
        ]

    def submit(self, request: DocumentIngestRequest) -> IngestJob:
        """Enfileira e devolve imediatamente. Nao espera I/O nenhum."""
        self.start()
        job = IngestJob(job_id=uuid4(), slug=request.slug, submitted_at=_now())
        try:
            self._queue.put_nowait((job, request))
        except asyncio.QueueFull as exc:
            raise IngestionQueueFullError(
                f"fila de ingestao cheia ({self._settings.ingest_queue_size}); reenvie depois"
            ) from exc
        self._jobs[job.job_id] = job
        self._evict_finished()
        return job

    def get_job(self, job_id: UUID) -> IngestJob | None:
        return self._jobs.get(job_id)

    async def join(self) -> None:
        """Espera a fila esvaziar (testes, shutdown ordenado)."""
        await self._queue.join()

    async def close(self, *, drain: bool = True) -> None:
        if drain and self._workers:
            await self._queue.join()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    async def _worker(self) -> None:
        while True:
            job, request = await self._queue.get()
            job.state = IngestJobState.RUNNING
            try:
                job.result = await self.ingest(request)
                job.state = IngestJobState.DONE
            except asyncio.CancelledError:
                job.state, job.error = IngestJobState.FAILED, "cancelado no shutdown"
                raise
            except Exception as exc:  # noqa: BLE001 - o worker nao pode morrer
                logger.exception("ingestao de %r falhou", request.slug)
                job.state, job.error = IngestJobState.FAILED, f"{type(exc).__name__}: {exc}"
            finally:
                job.finished_at = _now()
                self._queue.task_done()

    def _evict_finished(self) -> None:
        finished = [
            job_id
            for job_id, job in self._jobs.items()
            if job.state in (IngestJobState.DONE, IngestJobState.FAILED)
        ]
        for job_id in finished[: max(0, len(finished) - _FINISHED_JOBS_KEPT)]:
            del self._jobs[job_id]

    # -- decisao --------------------------------------------------------------

    async def _decide(
        self,
        conn: asyncpg.Connection,
        request: DocumentIngestRequest,
        content_hash: str,
        writer: MemoryScopeFilter | None = None,
    ) -> _Decision:
        if writer is not None and not await _reaches(
            conn, writer, request.access_level.value, request.department_scope
        ):
            raise ClassificationOutOfReachError(
                f"classificacao {request.access_level.value} "
                f"{request.department_scope or ['empresa toda']} fora do alcance de quem escreve"
            )
        existing = await conn.fetchrow(_SELECT_DOC + "WHERE slug = $1", request.slug)
        if existing is not None and writer is not None:
            # Julgado pela classificacao, nao pelo status: um documento
            # aposentado de outro nivel continua sendo de outro nivel.
            if not await _reaches(
                conn, writer, existing["access_level"], list(existing["department_scope"])
            ):
                raise SlugUnavailableError(f"slug {request.slug!r} ja esta em uso no acervo")
        if existing is not None:
            same_content = (
                existing["status"] == "active"
                and existing["content_hash"] == content_hash
                # Titulo e formato entram no texto dos chunks; mudou, refaz.
                and existing["title"] == request.title
                and existing["content_format"] == request.content_format
            )
            if not same_content or request.force_update:
                return _Decision(_Action.UPDATE, existing)
            if self._same_metadata(existing, request):
                return _Decision(_Action.DUPLICATE, existing)
            return _Decision(_Action.METADATA, existing)

        if not request.force_update:
            if writer is None:
                other = await conn.fetchrow(
                    _SELECT_DOC
                    + "WHERE content_hash = $1 AND status = 'active' ORDER BY created_at LIMIT 1",
                    content_hash,
                )
            else:
                slug = await conn.fetchval(
                    _SELECT_DUP_IN_REACH, *document_scope_args(writer), content_hash
                )
                other = (
                    None
                    if slug is None
                    else await conn.fetchrow(_SELECT_DOC + "WHERE slug = $1", slug)
                )
            if other is not None:
                return _Decision(_Action.DUPLICATE, other, duplicate_of=other["slug"])
        return _Decision(_Action.CREATE)

    @staticmethod
    def _same_metadata(existing: asyncpg.Record, request: DocumentIngestRequest) -> bool:
        return (
            existing["doc_type"] == request.doc_type
            and existing["source_url_or_path"] == request.source_url_or_path
            and existing["access_level"] == request.access_level.value
            and sorted(existing["department_scope"]) == sorted(request.department_scope)
            and existing["metadata"] == _document_metadata(request)
        )

    @staticmethod
    def _duplicate(decision: _Decision, content_hash: str, started: float) -> IngestResult:
        existing = decision.existing
        assert existing is not None
        return IngestResult(
            status=IngestStatus.DUPLICATE,
            document_id=existing["id"],
            slug=existing["slug"],
            version=existing["version"],
            content_hash=content_hash,
            chunk_count=existing["chunk_count"],
            duplicate_of=decision.duplicate_of,
            elapsed_ms=(perf_counter() - started) * 1000,
        )

    # -- preparo e gravacao ---------------------------------------------------

    async def _prepare(self, request: DocumentIngestRequest) -> _Prepared:
        # Markdown vira nota: frontmatter, teia de links e tags saem daqui. O
        # que se fatia e o CORPO - frontmatter e metadado, nao prosa, e
        # embedar `tags: [x, y]` so suja o espaco vetorial. `raw_content`
        # continua intacto no banco, frontmatter incluso.
        note = (
            await asyncio.to_thread(parse_note, request.raw_content)
            if request.content_format == "markdown"
            else None
        )
        chunks = await asyncio.to_thread(
            chunk_document,
            note.body if note else request.raw_content,
            title=request.title,
            content_format=request.content_format,
            options=self._settings.chunking,
        )
        if not chunks:
            raise ValueError(f"documento {request.slug!r} nao gerou nenhum chunk")
        # O embedding ve o caminho de titulos: "Prazo" sozinho nao diz de que
        # contrato e; "Contrato X > Vigencia > Prazo" diz.
        inputs = [f"{' > '.join(chunk.heading_path)}\n\n{chunk.content}" for chunk in chunks]
        vectors = await embed_texts(self._embedder, inputs, self._settings)
        return _Prepared(chunks, vectors, note)

    async def _apply(
        self,
        conn: asyncpg.Connection,
        request: DocumentIngestRequest,
        content_hash: str,
        decision: _Decision,
        prepared: _Prepared | None,
        started: float,
    ) -> IngestResult:
        metadata = _document_metadata(request)
        existing = decision.existing

        if decision.action is _Action.METADATA:
            assert existing is not None
            version = await conn.fetchval(
                """
                UPDATE wiki_documents
                SET doc_type = $2, source_url_or_path = $3, access_level = $4,
                    department_scope = $5, metadata = $6, version = version + 1, updated_by = $7
                WHERE id = $1
                RETURNING version
                """,
                existing["id"],
                request.doc_type,
                request.source_url_or_path,
                request.access_level.value,
                request.department_scope,
                metadata,
                request.ingested_by,
            )
            # Tags de documento vivem copiadas nos chunks (filtro GIN); troca
            # as antigas pelas novas sem reembedar nada.
            stale_keys = list((existing["metadata"] or {}).get("chunk_tags", {}).keys())
            await conn.execute(
                """
                UPDATE wiki_chunks
                SET metadata_tags = (metadata_tags - $2::text[]) || $3::jsonb
                WHERE document_id = $1
                """,
                existing["id"],
                stale_keys,
                {"doc_type": request.doc_type, "tags": request.tags, **request.metadata_tags},
            )
            document_id, chunk_count = existing["id"], existing["chunk_count"]
            status = IngestStatus.METADATA_UPDATED
        else:
            assert prepared is not None
            chunk_count = len(prepared.chunks)
            if decision.action is _Action.CREATE:
                row = await conn.fetchrow(
                    """
                    INSERT INTO wiki_documents
                        (slug, title, doc_type, source_url_or_path, content_format, raw_content,
                         content_hash, access_level, department_scope, metadata,
                         embedding_model, chunk_count, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    RETURNING id, version
                    """,
                    request.slug,
                    request.title,
                    request.doc_type,
                    request.source_url_or_path,
                    request.content_format,
                    request.raw_content,
                    content_hash,
                    request.access_level.value,
                    request.department_scope,
                    metadata,
                    self._embedding_label,
                    chunk_count,
                    request.ingested_by,
                )
                status = IngestStatus.CREATED
            else:
                assert existing is not None
                row = await conn.fetchrow(
                    """
                    UPDATE wiki_documents
                    SET title = $2, doc_type = $3, source_url_or_path = $4, content_format = $5,
                        raw_content = $6, content_hash = $7, access_level = $8,
                        department_scope = $9, metadata = $10, embedding_model = $11,
                        chunk_count = $12, updated_by = $13, status = 'active',
                        version = version + 1
                    WHERE id = $1
                    RETURNING id, version
                    """,
                    existing["id"],
                    request.title,
                    request.doc_type,
                    request.source_url_or_path,
                    request.content_format,
                    request.raw_content,
                    content_hash,
                    request.access_level.value,
                    request.department_scope,
                    metadata,
                    self._embedding_label,
                    chunk_count,
                    request.ingested_by,
                )
                await conn.execute("DELETE FROM wiki_chunks WHERE document_id = $1", row["id"])
                status = IngestStatus.UPDATED
            document_id, version = row["id"], row["version"]
            await conn.executemany(
                _INSERT_CHUNK,
                [
                    (
                        document_id,
                        chunk.index,
                        " > ".join(chunk.heading_path),
                        chunk.content,
                        chunk.token_count,
                        vector,
                        _chunk_tags(request, chunk, chunk_count),
                    )
                    for chunk, vector in zip(prepared.chunks, prepared.vectors)
                ],
            )

        # A teia. Links e tags sao DERIVADOS do conteudo: refazer e sempre
        # seguro, e por isso trocam por inteiro em vez de tentar casar delta.
        note = prepared.note if prepared else None
        tags: dict[str, str] = dict.fromkeys(request.tags, "ingest")
        if note is not None:
            tags.update(note.tag_sources)
        await replace_tags(conn, document_id, tags)
        if note is not None and decision.action is not _Action.METADATA:
            await replace_links(conn, document_id, note.links)
        if decision.action is _Action.CREATE:
            # Momento em que a nota deixa de ser fantasma: quem ja apontava
            # para este slug passa a apontar para um documento de verdade.
            resolvidos = await resolve_pending(conn, request.slug, document_id)
            if resolvidos:
                logger.info("%s resolveu %d link(s) pendente(s)", request.slug, resolvidos)

        for path in request.index_paths:
            await self._meta_index.link_document(
                conn, path, document_id, request.slug, request.access_level
            )

        return IngestResult(
            status=status,
            document_id=document_id,
            slug=request.slug,
            version=version,
            content_hash=content_hash,
            chunk_count=chunk_count,
            elapsed_ms=(perf_counter() - started) * 1000,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _reaches(
    conn: asyncpg.Connection,
    scope: MemoryScopeFilter,
    access_level: str,
    department_scope: list[str],
) -> bool:
    """O escopo alcancaria esta classificacao? Mesma regra da leitura."""
    return bool(
        await conn.fetchval(
            _REACH_SQL, *document_scope_args(scope), access_level, list(department_scope)
        )
    )
