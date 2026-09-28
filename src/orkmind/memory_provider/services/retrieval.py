"""Recuperacao hibrida (densa + lexica) com Reciprocal Rank Fusion em SQL.

Uma unica consulta funde tudo, sobre o mesmo snapshot:

- `dense`: vizinhos por distancia de cosseno (`<=>`) no indice HNSW;
- `sparse`: full-text em portugues no indice GIN. E esta perna que acerta
  jargao, sigla e numero de contrato, onde embedding costuma errar;
- `fused`: RRF(d) = soma de 1 / (k + rank_i(d)) sobre as listas em que o
  chunk apareceu.

As duas pernas recebem o MESMO pre-filtro de escopo (`services/scope.py`),
aplicado antes do ranking: chunk fora do papel ou do departamento de quem
pergunta nao e candidato, entao nao ha o que vazar no pos-processamento.

Perna lexica
------------
`plainto_tsquery` liga os termos com AND, e uma pergunta em linguagem natural
quase nunca tem TODAS as palavras num chunk so. No modo padrao (`any`) os
lexemas sao ligados com OR para dar recall, e a ordenacao devolve a precisao:
primeiro quem casa a consulta inteira, depois quem cobre mais lexemas
distintos, depois `ts_rank_cd`. "valor do contrato CT-2024/0187" vira
{valor, contrat, ct, -2024, /0187}: o chunk com o numero exato cobre mais
lexemas que qualquer chunk generico sobre contratos. `lexical_mode="all"`
restaura o AND estrito.

OR ingenuo nao escala: "contrato" e "rede" casam com metade do corpus, e o
Postgres precisa ranquear CADA linha casada (medido: ~600 ms em 30 mil
chunks, crescendo linearmente). Dois mecanismos resolvem:

1. Os CANDIDATOS saem so dos lexemas raros. Mede-se a frequencia de cada
   lexema com teto (`sparse_candidate_cap`, em cache) e entram os mais raros
   ate a soma bater o teto. O ranking continua usando todos os lexemas. E o
   IDF fazendo o trabalho que o `ts_rank` nao faz: quem discrimina e `/0187`,
   nao `contrato`. Se ate o mais raro passa do teto, a consulta nao tem sinal
   lexico proprio e os candidatos caem para o AND.
2. Um LIMIT rigido (`sparse_candidate_cap`) antes do ranking. Por (1) ele
   quase nunca e atingido; quando e (AND de palavras onipresentes, cache de
   frequencia velho), a perna lexica ranqueia uma AMOSTRA dos casamentos, e
   quem ordena de fato e a perna densa. O custo tem teto; a precisao lexica,
   nesse caso e so nesse, nao.

Desempate
---------
Rank 1 so na densa e rank 1 so na lexica valem o mesmo 1/(k+1). O empate vai
para a lexica: vizinho mais proximo existe SEMPRE, inclusive quando nada no
corpus responde a pergunta; casamento lexico so existe quando ha evidencia.

Plano de execucao
-----------------
A consulta roda com `plan_cache_mode = force_custom_plan`, e todo tsquery
chega como constante. Nao e detalhe: os filtros opcionais (`$n IS NULL OR
...`) so somem no plano customizado, e o Postgres adota o plano generico a
partir da 6a execucao de um prepared statement. Medido em 30 mil chunks
(`bench/memory_provider_scale.py`): ~11 ms no customizado contra ~47 ms no
generico. Numa versao anterior desta consulta, com os tsquery saindo de uma
CTE, a mesma troca levava de ~650 ms para ~6,2 s.
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic, perf_counter
from typing import TYPE_CHECKING
from uuid import UUID

from orkmind.embeddings.provider import EmbeddingError, EmbeddingProvider
from orkmind.memory_provider.config import MemoryProviderSettings
from orkmind.memory_provider.embeddings import embed_query
from orkmind.memory_provider.errors import DocumentNotFoundError, EmbeddingDimensionError
from orkmind.memory_provider.models.schemas import (
    DocumentView,
    MemoryScopeFilter,
    SearchResponse,
    SearchResult,
)
from orkmind.memory_provider.services.scope import document_scope_args, document_scope_clause

if TYPE_CHECKING:  # pragma: no cover - so para anotacao
    import asyncpg

logger = logging.getLogger(__name__)

# Frequencias de lexema ficam em cache por processo: o vocabulario de um
# dominio e pequeno e muda devagar.
_DF_CACHE_TTL_S = 300.0
_DF_CACHE_MAX = 20_000

# Teto do GUC hnsw.ef_search no pgvector.
_EF_SEARCH_MAX = 1000
# A partir desta versao o HNSW continua varrendo ate o filtro encher o LIMIT.
_ITERATIVE_SCAN_MIN_VERSION = (0, 8)

# Lexemas e AND estrito da consulta, ja normalizados pelo dicionario do banco.
_ANALYZE_QUERY_SQL = """
SELECT tsvector_to_array(to_tsvector($1::text::regconfig, $2)) AS lexemes,
       NULLIF(plainto_tsquery($1::text::regconfig, $2)::text, '') AS tsq_and
"""

# Frequencia de cada lexema COM TETO: o LIMIT interrompe a contagem em
# teto + 1, entao palavra onipresente custa o mesmo que palavra rara.
_LEXEME_DF_SQL = """
SELECT entry.lexeme,
       (SELECT count(*) FROM (
            SELECT 1 FROM wiki_chunks c
            WHERE c.tsv @@ entry.tsq::tsquery
            LIMIT $3
        ) capped) AS df
FROM unnest($1::text[], $2::text[]) AS entry(lexeme, tsq)
"""

# $1 embedding (NULL = sem perna densa)
# $2 tsquery AND da consulta (NULL = sem perna lexica)
# $3 tsquery de candidatos (NULL = usar o AND)   $4 lexemas da consulta
# $5-$7 escopo (niveis, cross_department, departamentos)
# $8 doc_types  $9 tags  $10 index_path  $11 document_ids
# $12 candidatos por perna  $13 k do RRF  $14 limit
# $15 distancia maxima  $16 teto de linhas ranqueadas na perna lexica
_CANDIDATE_FILTER = f"""
      {document_scope_clause("d", 5)}
      AND ($8::text[] IS NULL OR d.doc_type = ANY($8::text[]))
      AND ($9::jsonb IS NULL OR c.metadata_tags @> $9::jsonb)
      AND ($10::text IS NULL OR d.id IN (
            SELECT m.document_id FROM meta_index m
            WHERE m.level = 2 AND starts_with(m.path, $10::text || '/')))
      AND ($11::uuid[] IS NULL OR d.id = ANY($11::uuid[]))
"""

HYBRID_SEARCH_SQL = f"""
WITH dense AS (
    SELECT id, dist, ROW_NUMBER() OVER (ORDER BY dist, id) AS rnk
    FROM (
        SELECT c.id, c.embedding <=> $1::vector AS dist
        FROM wiki_chunks c
        JOIN wiki_documents d ON d.id = c.document_id
        WHERE $1::vector IS NOT NULL
          AND ($15::float8 IS NULL OR c.embedding <=> $1::vector <= $15::float8)
          AND {_CANDIDATE_FILTER}
        ORDER BY c.embedding <=> $1::vector
        LIMIT $12
    ) nearest
),
sparse AS (
    SELECT id, lex, ROW_NUMBER() OVER (ORDER BY strict DESC, coverage DESC, lex DESC, id) AS rnk
    FROM (
        SELECT pool.id,
               (pool.tsv @@ $2::text::tsquery) AS strict,
               (SELECT count(*) FROM unnest($4::text[]) AS lexeme
                 WHERE lexeme = ANY(tsvector_to_array(pool.tsv))) AS coverage,
               ts_rank_cd(pool.tsv, replace($2::text, ' & ', ' | ')::tsquery, 1) AS lex
        FROM (
            SELECT c.id, c.tsv
            FROM wiki_chunks c
            JOIN wiki_documents d ON d.id = c.document_id
            WHERE $2::text IS NOT NULL
              AND c.tsv @@ COALESCE($3::text::tsquery, $2::text::tsquery)
              AND {_CANDIDATE_FILTER}
            LIMIT $16
        ) pool
        ORDER BY strict DESC, coverage DESC, lex DESC, pool.id
        LIMIT $12
    ) matched
),
fused AS (
    SELECT id,
           SUM(1.0 / ($13::int + rnk)) AS rrf_score,
           MAX(rnk) FILTER (WHERE leg = 'dense') AS dense_rank,
           MAX(rnk) FILTER (WHERE leg = 'sparse') AS sparse_rank,
           MAX(dist) AS dist,
           MAX(lex) AS lex
    FROM (
        SELECT id, rnk, 'dense' AS leg, dist, NULL::real AS lex FROM dense
        UNION ALL
        SELECT id, rnk, 'sparse' AS leg, NULL::float8 AS dist, lex FROM sparse
    ) legs
    GROUP BY id
)
SELECT c.id AS chunk_id, c.document_id, d.slug, d.title, d.doc_type, d.access_level,
       d.department_scope, d.version AS document_version, d.source_url_or_path,
       c.chunk_index, c.heading_path, c.content, c.token_count, c.metadata_tags,
       f.rrf_score::float8 AS rrf_score, f.dense_rank, f.sparse_rank,
       f.dist AS cosine_distance, f.lex::float8 AS lexical_score
FROM fused f
JOIN wiki_chunks c ON c.id = f.id
JOIN wiki_documents d ON d.id = c.document_id
ORDER BY f.rrf_score DESC, f.sparse_rank NULLS LAST, f.dense_rank NULLS LAST, c.id
LIMIT $14
"""

_DOCUMENT_SQL = f"""
SELECT d.id, d.slug, d.title, d.doc_type, d.source_url_or_path, d.content_format,
       d.raw_content, d.content_hash, d.version, d.access_level, d.department_scope,
       d.status, d.metadata, d.chunk_count, d.created_at, d.updated_at
FROM wiki_documents d
WHERE (d.slug = $1::text OR d.id::text = $1::text)
  AND {document_scope_clause("d", 2)}
"""


def quote_lexeme(lexeme: str) -> str:
    """Lexema JA normalizado na sintaxe textual de tsquery (aspas e barra escapadas)."""
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def select_candidate_lexemes(frequencies: dict[str, int], cap: int) -> list[str]:
    """Lexemas que geram candidatos: os mais raros, ate a soma bater `cap`.

    Lexema com frequencia 0 entra sempre e de graca: o cache pode estar
    velho, e descarta-lo esconderia um documento recem-ingerido. Lista vazia
    = nem o mais raro cabe no teto (a consulta so tem palavra onipresente).
    """
    selected: list[str] = []
    spent = 0
    for lexeme, df in sorted(frequencies.items(), key=lambda item: (item[1], item[0])):
        if spent + df > cap:
            break
        selected.append(lexeme)
        spent += df
    return selected


class RetrievalService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        embedder: EmbeddingProvider,
        settings: MemoryProviderSettings,
        *,
        pgvector_version: tuple[int, ...] = (0,),
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        self._settings = settings
        self._iterative_scan = pgvector_version >= _ITERATIVE_SCAN_MIN_VERSION
        self._df_cache: dict[str, tuple[int, float]] = {}

    async def search(
        self,
        query: str,
        scope: MemoryScopeFilter,
        limit: int = 5,
        *,
        max_tokens: int | None = None,
    ) -> SearchResponse:
        started = perf_counter()
        query = query.strip()
        if not query or limit < 1:
            return SearchResponse(query=query, results=[], token_total=0)

        embedding, degraded = await self._query_embedding(query)
        settings = self._settings
        candidates = min(
            _EF_SEARCH_MAX, max(limit * settings.candidate_multiplier, settings.min_candidates)
        )
        levels, cross, departments = document_scope_args(scope)

        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            # Ver "Plano de execucao" no docstring do modulo.
            await conn.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
            # ef_search < candidatos faria o HNSW devolver menos do que o LIMIT.
            await conn.execute("SELECT set_config('hnsw.ef_search', $1, true)", str(candidates))
            if self._iterative_scan:
                # Com pre-filtro seletivo o HNSW puro acha k vizinhos e o
                # filtro descarta quase todos; o scan iterativo segue
                # varrendo ate encher o LIMIT.
                await conn.execute(
                    "SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"
                )
            tsq_and, tsq_candidates, lexemes = await self._lexical_plan(conn, query)
            rows = await conn.fetch(
                HYBRID_SEARCH_SQL,
                embedding,
                tsq_and,
                tsq_candidates,
                lexemes,
                levels,
                cross,
                departments,
                scope.doc_types,
                scope.tags,
                scope.index_path,
                scope.document_ids,
                candidates,
                settings.rrf_k,
                limit,
                settings.dense_max_distance,
                settings.sparse_candidate_cap,
            )

        results: list[SearchResult] = []
        token_total = 0
        for row in rows:
            result = SearchResult(**dict(row))
            # O primeiro sempre entra: orcamento menor que um chunk nao pode
            # significar resposta vazia.
            if max_tokens is not None and results and token_total + result.token_count > max_tokens:
                break
            results.append(result)
            token_total += result.token_count

        return SearchResponse(
            query=query,
            results=results,
            token_total=token_total,
            degraded=degraded,
            elapsed_ms=(perf_counter() - started) * 1000,
        )

    async def get_document(self, slug_or_id: str | UUID, scope: MemoryScopeFilter) -> DocumentView:
        """Documento integro, sob o mesmo escopo da busca."""
        row = await self._pool.fetchrow(_DOCUMENT_SQL, str(slug_or_id), *document_scope_args(scope))
        if row is None:
            raise DocumentNotFoundError(f"documento nao encontrado: {slug_or_id}")
        return DocumentView(**dict(row))

    def invalidate_lexeme_cache(self) -> None:
        """Chamado apos ingestao: frequencia velha so custa desempenho, mas nao precisa durar."""
        self._df_cache.clear()

    async def _lexical_plan(
        self, conn: asyncpg.Connection, query: str
    ) -> tuple[str | None, str | None, list[str]]:
        """(AND da consulta, tsquery de candidatos, lexemas). Tudo None/[] = sem perna lexica."""
        settings = self._settings
        analyzed = await conn.fetchrow(_ANALYZE_QUERY_SQL, settings.fts_config, query)
        lexemes: list[str] = list(analyzed["lexemes"] or [])
        tsq_and: str | None = analyzed["tsq_and"]
        if tsq_and is None or not lexemes:
            return None, None, []
        if settings.lexical_mode == "all":
            return tsq_and, None, lexemes

        frequencies = await self._lexeme_frequencies(conn, lexemes)
        selected = select_candidate_lexemes(frequencies, settings.sparse_candidate_cap)
        if not selected:
            return tsq_and, None, lexemes
        return tsq_and, " | ".join(quote_lexeme(lexeme) for lexeme in selected), lexemes

    async def _lexeme_frequencies(
        self, conn: asyncpg.Connection, lexemes: list[str]
    ) -> dict[str, int]:
        now = monotonic()
        frequencies: dict[str, int] = {}
        missing: list[str] = []
        for lexeme in lexemes:
            cached = self._df_cache.get(lexeme)
            if cached is not None and cached[1] > now:
                frequencies[lexeme] = cached[0]
            else:
                missing.append(lexeme)
        if missing:
            rows = await conn.fetch(
                _LEXEME_DF_SQL,
                missing,
                [quote_lexeme(lexeme) for lexeme in missing],
                self._settings.sparse_candidate_cap + 1,
            )
            if len(self._df_cache) + len(rows) > _DF_CACHE_MAX:
                self._df_cache.clear()
            for row in rows:
                frequencies[row["lexeme"]] = row["df"]
                self._df_cache[row["lexeme"]] = (row["df"], now + _DF_CACHE_TTL_S)
        return frequencies

    async def _query_embedding(self, query: str) -> tuple[list[float] | None, str | None]:
        try:
            return await embed_query(self._embedder, query, self._settings), None
        except EmbeddingDimensionError:
            # Configuracao errada, nao indisponibilidade: tem que estourar.
            raise
        except (EmbeddingError, asyncio.TimeoutError) as exc:
            reason = f"dense_unavailable: {type(exc).__name__}: {exc}".rstrip(": ")
            logger.warning("busca degradada para lexica pura (%s)", reason)
            return None, reason
