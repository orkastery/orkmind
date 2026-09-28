"""`NativeMemoryProvider`: fachada assincrona das tres camadas de memoria.

    Tier 1  Core Memory    get_system_context   blocos pinados, sempre presentes
    Tier 2  Recall Memory  append_history       historico append-only + janela curta
    Tier 3  Wiki Memory    search_wiki          documentos -> chunks, busca hibrida RRF

mais o meta-indice (`browse_index`) e a montagem JIT do turno
(`assemble_turn_context`).

O provider roda direto sobre PostgreSQL + pgvector, sem runtime externo de
memoria. E ele nao compacta nada: o contexto de um turno e limitado por
CONSTRUCAO (core fixo + N turnos + poucos chunks), entao nao existe o momento
em que a memoria "enche" e precisa ser resumida.

Uso:

    async with await NativeMemoryProvider.connect(settings) as memory:
        system = await memory.get_system_context("assistant", user)
        hits = await memory.search_wiki("SLA da rede neutra", scope)
        await memory.append_history(session_id, "user", texto)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any
from uuid import UUID

from orkmind.embeddings.provider import EmbeddingProvider
from orkmind.memory_provider.config import MemoryProviderSettings
from orkmind.memory_provider.db.migrations import apply_schema, create_pool, pgvector_version
from orkmind.memory_provider.embeddings import build_default_embedder
from orkmind.memory_provider.errors import EmbeddingDimensionError
from orkmind.memory_provider.models.schemas import (
    AccessLevel,
    ChatRole,
    CoreBlockLabel,
    CoreMemoryBlock,
    DocumentIngestRequest,
    DocumentView,
    ExecutiveScopeGuard,
    HistoryMessage,
    HistoryPage,
    IngestJob,
    IngestResult,
    IngestStatus,
    MemoryScopeFilter,
    MetaIndexNode,
    SearchResponse,
    SearchResult,
    SystemContext,
    TurnContext,
    UserContext,
)
from orkmind.memory_provider.services.core_memory import CoreMemoryService
from orkmind.memory_provider.services.extraction import SUPPORTED_SUFFIXES, extract_file
from orkmind.memory_provider.services.graph import GraphService
from orkmind.memory_provider.services.ingestion import IngestionService
from orkmind.memory_provider.services.links import LinkService
from orkmind.memory_provider.services.meta_index import MetaIndexService
from orkmind.memory_provider.services.notes import parse_note, slugify
from orkmind.memory_provider.services.recall import RecallService
from orkmind.memory_provider.services.retrieval import RetrievalService
from orkmind.memory_provider.tokens import estimate_tokens

if TYPE_CHECKING:  # pragma: no cover - so para anotacao
    import asyncpg

logger = logging.getLogger(__name__)


class NativeMemoryProvider:
    """Memory Provider nativo: Core, Recall e Wiki sobre PostgreSQL + pgvector."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        embedder: EmbeddingProvider,
        settings: MemoryProviderSettings,
        *,
        vector_version: tuple[int, ...] = (0,),
        embedding_label: str | None = None,
    ) -> None:
        if embedder.dim != settings.embedding_dim:
            raise EmbeddingDimensionError(
                f"embedder gera {embedder.dim} dimensoes, settings.embedding_dim e "
                f"{settings.embedding_dim}"
            )
        self._pool = pool
        self._settings = settings
        self.core = CoreMemoryService(pool)
        self.recall = RecallService(pool, settings)
        self.meta_index = MetaIndexService(pool)
        self.links = LinkService(pool)
        self.graph = GraphService(pool)
        self.retrieval = RetrievalService(pool, embedder, settings, pgvector_version=vector_version)
        self.ingestion = IngestionService(
            pool,
            embedder,
            settings,
            self.meta_index,
            embedding_label=embedding_label or type(embedder).__name__,
            on_change=self.retrieval.invalidate_lexeme_cache,
        )

    @classmethod
    async def connect(
        cls,
        settings: MemoryProviderSettings | None = None,
        *,
        embedder: EmbeddingProvider | None = None,
    ) -> NativeMemoryProvider:
        """Migra (se `auto_migrate`), abre o pool e devolve o provider pronto."""
        settings = settings or MemoryProviderSettings.from_env()
        vector_schema = await apply_schema(settings) if settings.auto_migrate else None
        pool = await create_pool(settings, vector_schema=vector_schema)
        try:
            label = None if embedder else settings.embedding_model
            return cls(
                pool,
                embedder or build_default_embedder(settings),
                settings,
                vector_version=await pgvector_version(pool),
                embedding_label=label,
            )
        except BaseException:
            await pool.close()
            raise

    async def close(self) -> None:
        await self.ingestion.close()
        await self._pool.close()

    async def __aenter__(self) -> NativeMemoryProvider:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def settings(self) -> MemoryProviderSettings:
        return self._settings

    # -- Tier 1: Core Memory --------------------------------------------------

    async def get_system_context(
        self, agent_id: str, user_context: UserContext, *, strict: bool = True
    ) -> SystemContext:
        """Blocos pinados do agente + identidade do canal, prontos para o prompt."""
        return await self.core.get_system_context(agent_id, user_context, strict=strict)

    async def set_core_block(
        self,
        agent_id: str,
        label: CoreBlockLabel | str,
        content: str,
        *,
        created_by: str,
        scope_key: str = "",
    ) -> CoreMemoryBlock:
        """API de OPERADOR. Nao exponha como ferramenta do agente."""
        return await self.core.set_block(
            agent_id, CoreBlockLabel(label), content, created_by=created_by, scope_key=scope_key
        )

    # -- Tier 2: Recall Memory ------------------------------------------------

    async def append_history(
        self,
        session_id: str,
        role: ChatRole | str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        *,
        agent_id: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> HistoryMessage:
        return await self.recall.append(
            session_id,
            ChatRole(role),
            content,
            tool_calls,
            agent_id=agent_id or self._settings.default_agent_id,
            user_id=user_id,
            metadata=metadata,
        )

    async def get_recent_history(
        self, session_id: str, turns: int | None = None
    ) -> list[HistoryMessage]:
        return await self.recall.window(session_id, turns)

    async def read_history_message(
        self, message_id: UUID, *, offset: int = 0, max_tokens: int | None = None
    ) -> HistoryPage:
        return await self.recall.read_message(message_id, offset=offset, max_tokens=max_tokens)

    async def search_history(
        self,
        query: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 5,
    ) -> list[HistoryMessage]:
        return await self.recall.search(query, session_id=session_id, user_id=user_id, limit=limit)

    async def get_full_history(
        self, session_id: str, *, after_seq: int = 0, limit: int = 200
    ) -> list[HistoryMessage]:
        return await self.recall.full_history(session_id, after_seq=after_seq, limit=limit)

    # -- Tier 3: Wiki Memory --------------------------------------------------

    async def search_wiki(
        self,
        query: str,
        scope_filter: MemoryScopeFilter,
        limit: int = 5,
        *,
        user_context: UserContext | None = None,
        max_tokens: int | None = None,
    ) -> list[SearchResult]:
        response = await self.search_wiki_detailed(
            query, scope_filter, limit, user_context=user_context, max_tokens=max_tokens
        )
        return response.results

    async def search_wiki_detailed(
        self,
        query: str,
        scope_filter: MemoryScopeFilter,
        limit: int = 5,
        *,
        user_context: UserContext | None = None,
        max_tokens: int | None = None,
    ) -> SearchResponse:
        """Como `search_wiki`, mais total de tokens e o sinal `degraded`."""
        scope = self._guarded(scope_filter, user_context)
        return await self.retrieval.search(query, scope, limit, max_tokens=max_tokens)

    async def get_document(
        self,
        slug_or_id: str | UUID,
        scope_filter: MemoryScopeFilter,
        *,
        user_context: UserContext | None = None,
    ) -> DocumentView:
        scope = self._guarded(scope_filter, user_context)
        return await self.retrieval.get_document(slug_or_id, scope)

    async def ingest_document(self, request: DocumentIngestRequest) -> IngestResult:
        """Ingestao direta: espera terminar e devolve o resultado."""
        return await self.ingestion.ingest(request)

    def submit_document(self, request: DocumentIngestRequest) -> IngestJob:
        """Ingestao em segundo plano: devolve na hora, sem travar o canal."""
        return self.ingestion.submit(request)

    def get_ingest_job(self, job_id: UUID) -> IngestJob | None:
        return self.ingestion.get_job(job_id)

    async def retire_document(self, slug: str, *, actor: str) -> bool:
        return await self.ingestion.retire(slug, actor=actor)

    async def ingest_file(
        self,
        path: str | Path,
        *,
        slug: str | None = None,
        doc_type: str = "note",
        access_level: AccessLevel = AccessLevel.OPERATIONAL,
        department_scope: list[str] | None = None,
        index_paths: list[str] | None = None,
        ingested_by: str = "system",
        force_update: bool = False,
    ) -> IngestResult:
        """Ingere um arquivo do disco: PDF, DOCX, CSV, TXT, Markdown ou HTML.

        O texto extraido vira o documento integro. Titulo e tags saem do
        proprio arquivo quando ele traz (frontmatter, H1, `#tag`).
        """
        caminho = Path(path)
        extraido = await asyncio.to_thread(extract_file, caminho)
        note = parse_note(extraido.text) if extraido.content_format == "markdown" else None
        titulo = (note.title if note else None) or caminho.stem
        return await self.ingest_document(
            DocumentIngestRequest(
                slug=slug or slugify(caminho.stem),
                title=titulo,
                doc_type=doc_type,
                raw_content=extraido.text,
                content_format=extraido.content_format,  # type: ignore[arg-type]
                source_url_or_path=str(caminho.resolve()),
                access_level=access_level,
                department_scope=department_scope or [],
                metadata=extraido.metadata,
                index_paths=index_paths or [],
                ingested_by=ingested_by,
                force_update=force_update,
            )
        )

    async def ingest_vault(
        self,
        root: str | Path,
        *,
        access_level: AccessLevel = AccessLevel.OPERATIONAL,
        department_scope: list[str] | None = None,
        doc_type: str = "note",
        ingested_by: str = "vault",
        force_update: bool = False,
    ) -> dict[str, Any]:
        """Ingere um vault/pasta inteira, recursivamente.

        Os links entre as notas se resolvem sozinhos conforme os alvos vao
        nascendo: quem foi ingerido antes do seu alvo fica pendente e e
        religado quando o alvo aparece. Por isso nao ha ordem obrigatoria.

        Arquivo que falha NAO aborta o vault - ele entra em `falhas`, porque
        um PDF digitalizado no meio de mil notas nao pode impedir as outras.
        """
        raiz = Path(root)
        if not raiz.is_dir():
            raise NotADirectoryError(f"nao e um diretorio: {raiz}")
        resumo: dict[str, Any] = {"ingeridos": 0, "duplicados": 0, "falhas": [], "ignorados": 0}
        for arquivo in sorted(raiz.rglob("*")):
            if not arquivo.is_file() or any(p.startswith(".") for p in arquivo.parts):
                continue  # `.obsidian/`, `.git/` e afins ficam de fora
            if arquivo.suffix.lower() not in SUPPORTED_SUFFIXES:
                resumo["ignorados"] += 1
                continue
            try:
                resultado = await self.ingest_file(
                    arquivo,
                    doc_type=doc_type,
                    access_level=access_level,
                    department_scope=department_scope,
                    ingested_by=ingested_by,
                    force_update=force_update,
                )
            except Exception as e:  # noqa: BLE001 - um arquivo ruim nao derruba o vault
                logger.warning("falha ao ingerir %s: %s", arquivo, e)
                resumo["falhas"].append(
                    {"arquivo": str(arquivo), "erro": f"{type(e).__name__}: {e}"}
                )
                continue
            chave = "duplicados" if resultado.status is IngestStatus.DUPLICATE else "ingeridos"
            resumo[chave] += 1
        return resumo

    # -- A teia: links e tags --------------------------------------------------

    async def backlinks(
        self,
        slug_or_id: str | UUID,
        scope_filter: MemoryScopeFilter,
        *,
        user_context: UserContext | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Quem aponta para este documento, entre o que o escopo deixa ver."""
        return await self.links.backlinks(
            slug_or_id, self._guarded(scope_filter, user_context), limit
        )

    async def outgoing_links(
        self,
        slug_or_id: str | UUID,
        scope_filter: MemoryScopeFilter,
        *,
        user_context: UserContext | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self.links.outgoing(
            slug_or_id, self._guarded(scope_filter, user_context), limit
        )

    async def unresolved_links(
        self,
        scope_filter: MemoryScopeFilter,
        *,
        user_context: UserContext | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """O que ja foi citado mas nunca escrito: a pauta do Company Brain."""
        return await self.links.unresolved(self._guarded(scope_filter, user_context), limit)

    async def neighborhood(
        self,
        slug: str,
        scope_filter: MemoryScopeFilter,
        *,
        user_context: UserContext | None = None,
        depth: int = 1,
        limit: int = 120,
        include_ghosts: bool = True,
    ) -> dict[str, Any]:
        """O grafo local: `slug` e `depth` saltos de arestas autoradas.

        O escopo vale nos dois lados de cada aresta, entao um documento fora do
        alcance nem serve de ponte. Documento inexistente e documento invisivel
        devolvem a mesma resposta vazia.
        """
        return await self.graph.neighborhood(
            slug,
            self._guarded(scope_filter, user_context),
            depth=depth,
            limit=limit,
            include_ghosts=include_ghosts,
        )

    async def list_tags(
        self,
        scope_filter: MemoryScopeFilter,
        prefix: str | None = None,
        *,
        user_context: UserContext | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return await self.links.list_tags(self._guarded(scope_filter, user_context), prefix, limit)

    async def documents_by_tag(
        self,
        tag: str,
        scope_filter: MemoryScopeFilter,
        *,
        user_context: UserContext | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Documentos com a tag e com as filhas dela (`rede` traz `rede/backbone`)."""
        return await self.links.by_tag(tag, self._guarded(scope_filter, user_context), limit)

    # -- Meta-indice ----------------------------------------------------------

    async def browse_index(
        self,
        scope_filter: MemoryScopeFilter,
        path: str | None = None,
        depth: int = 1,
        *,
        user_context: UserContext | None = None,
    ) -> list[MetaIndexNode]:
        scope = self._guarded(scope_filter, user_context)
        return await self.meta_index.browse(scope, path, depth)

    async def upsert_index_node(
        self,
        path: str,
        *,
        title: str = "",
        description: str = "",
        access_level: AccessLevel = AccessLevel.OPERATIONAL,
        position: int = 0,
    ) -> UUID:
        return await self.meta_index.upsert_node(
            path,
            title=title,
            description=description,
            access_level=access_level,
            position=position,
        )

    async def link_document(self, path: str, slug: str) -> None:
        await self.meta_index.link_document_by_slug(path, slug)

    # -- Montagem JIT ---------------------------------------------------------

    async def assemble_turn_context(
        self,
        agent_id: str,
        session_id: str,
        user_context: UserContext,
        query: str | None = None,
        *,
        scope_filter: MemoryScopeFilter | None = None,
        wiki_limit: int = 5,
        wiki_max_tokens: int | None = None,
        turns: int | None = None,
    ) -> TurnContext:
        """Contexto do turno: core + janela + chunks da wiki (se houver `query`).

        As tres leituras sao independentes e rodam em paralelo.
        """
        scope = self._guarded(
            scope_filter or MemoryScopeFilter.for_user(user_context), user_context
        )

        async def no_wiki() -> None:
            return None

        system, history, wiki = await asyncio.gather(
            self.core.get_system_context(agent_id, user_context),
            self.recall.window(session_id, turns),
            self.retrieval.search(query, scope, wiki_limit, max_tokens=wiki_max_tokens)
            if query
            else no_wiki(),
        )
        return TurnContext(
            system=system,
            history=history,
            wiki=wiki,
            tokens={
                "core": system.token_estimate,
                "recall": sum(
                    estimate_tokens(m.content) if m.truncated else m.token_estimate for m in history
                ),
                "wiki": wiki.token_total if wiki else 0,
            },
        )

    @staticmethod
    def _guarded(scope: MemoryScopeFilter, user: UserContext | None) -> MemoryScopeFilter:
        """Com identidade do canal em maos, o filtro passa pelo guard."""
        if user is None:
            return scope
        return ExecutiveScopeGuard(user=user, scope=scope).scope
