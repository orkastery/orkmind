"""Verificacao de ESCALA do Memory Provider nativo (orkmind.memory_provider).

Os testes de integracao rodam com uma duzia de chunks, e com uma duzia de
chunks o planner do Postgres faz Seq Scan em tudo e qualquer consulta e
rapida. Este script existe porque tres defeitos reais so apareceram com
dezenas de milhares de linhas:

  1. a perna lexica em OR ranqueava o corpus inteiro (~600 ms em 30 mil chunks);
  2. a partir da 6a execucao o Postgres trocava para o plano generico do
     prepared statement: ~650 ms -> ~6,2 s na primeira versao da consulta, e
     ainda ~11 ms -> ~47 ms na versao atual se `force_custom_plan` for removido;
  3. no empate de RRF a perna densa vencia a evidencia lexica.

Ele monta um corpus sintetico, busca pelo caminho REAL do provider e sai com
codigo != 0 se alguma garantia quebrar.

O QUE ISTO NAO PROVA: qualidade de recuperacao. Os vetores sao ruido
aleatorio e o texto sai de um vocabulario de 16 palavras - o pior caso para
a perna lexica, de proposito. O script prova forma do plano, teto de
latencia, precisao de identificador exato e isolamento de escopo. Relevancia
semantica so se mede com embeddings reais e perguntas reais.

Uso:
    export ORKMIND_PROVIDER_TEST_DATABASE_URL=postgresql://...:5433/memory_provider_test
    python bench/memory_provider_scale.py [--chunks 30000] [--dim 256] [--json saida.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from typing import Any
from urllib.parse import urlparse

import asyncpg
import numpy as np

from orkmind.memory_provider import (
    AccessLevel,
    HashingEmbeddingProvider,
    MemoryProviderSettings,
    MemoryScopeFilter,
    NativeMemoryProvider,
    UserContext,
)
from orkmind.memory_provider.services.retrieval import HYBRID_SEARCH_SQL
from orkmind.memory_provider.services.scope import document_scope_args

TEST_DB_ENV = "ORKMIND_PROVIDER_TEST_DATABASE_URL"
SCHEMA = "mp_test_scale_bench"
CHUNKS_PER_DOC = 15
WORDS = (
    "latencia backbone optico contrato reajuste fila chamado severidade "
    "energia licenca fibra roteador auditoria prazo risco custo"
).split()
DEPARTMENTS: list[list[str]] = [["NOC"], ["FINANCEIRO"], ["ENGENHARIA"], []]

# Da 6a execucao em diante o Postgres pode adotar o plano generico.
RUNS = 12
GENERIC_PLAN_FROM = 6
# Mediana das execucoes tardias contra a das iniciais. A folga aditiva e de
# poucos ms de proposito: um piso absoluto de 50 ms ja escondeu uma regressao
# real de 5x (9,8 ms -> 49,9 ms) num teste de mutacao deste proprio script.
MAX_REGIME_RATIO = 2.5
REGIME_SLACK_MS = 3.0
HNSW_DDL = (
    "CREATE INDEX wiki_chunks_embedding_hnsw ON wiki_chunks "
    "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
)


def _require_test_database() -> str:
    dsn = os.environ.get(TEST_DB_ENV, "")
    name = urlparse(dsn).path.lstrip("/").lower()
    if not dsn or not any(marker in name for marker in ("test", "_ci", "sandbox")):
        sys.exit(f"Defina {TEST_DB_ENV} apontando para um banco com 'test' no nome.")
    return dsn


async def _drop_schema(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    finally:
        await conn.close()


async def _load(memory: NativeMemoryProvider, chunks: int, dim: int) -> float:
    started = time.perf_counter()
    rng = random.Random(7)
    documents = chunks // CHUNKS_PER_DOC
    async with memory._pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO wiki_documents
                (slug, title, doc_type, raw_content, content_hash, access_level, department_scope)
            VALUES ($1, $2, 'opex_analysis', 'x', $3, $4, $5)
            """,
            [
                (
                    f"doc-{g:06d}",
                    f"Relatorio {g}",
                    f"{g:064x}",
                    "EXECUTIVE" if g % 10 == 0 else "OPERATIONAL",
                    DEPARTMENTS[g % 4],
                )
                for g in range(1, documents + 1)
            ],
        )
        ids = [r["id"] for r in await conn.fetch("SELECT id FROM wiki_documents ORDER BY slug")]
        vectors = np.random.default_rng(7).standard_normal((chunks, dim)).astype(np.float32)
        # Carga em massa sem o HNSW: construir o indice no final e muito mais rapido.
        await conn.execute("DROP INDEX wiki_chunks_embedding_hnsw")
        await conn.copy_records_to_table(
            "wiki_chunks",
            columns=[
                "document_id",
                "chunk_index",
                "heading_path",
                "content",
                "token_count",
                "embedding",
            ],
            records=[
                (
                    ids[n // CHUNKS_PER_DOC],
                    n % CHUNKS_PER_DOC,
                    "Relatorio",
                    " ".join(rng.choice(WORDS) for _ in range(60)) + f" CT-2025/{n:06d}",
                    80,
                    vectors[n],
                )
                for n in range(len(ids) * CHUNKS_PER_DOC)
            ],
        )
        await conn.execute(HNSW_DDL, timeout=1800)
        await conn.execute("ANALYZE wiki_chunks")
        await conn.execute("ANALYZE wiki_documents")
    return time.perf_counter() - started


async def _contract_of(memory: NativeMemoryProvider, where: str) -> str:
    value = await memory._pool.fetchval(
        f"""
        SELECT substring(c.content FROM 'CT-2025/[0-9]+')
        FROM wiki_chunks c JOIN wiki_documents d ON d.id = c.document_id
        WHERE {where} ORDER BY c.id LIMIT 1
        """
    )
    return str(value)


async def run(chunks: int, dim: int) -> dict[str, Any]:
    dsn = _require_test_database()
    await _drop_schema(dsn)
    settings = MemoryProviderSettings(
        database_url=dsn,
        db_schema=SCHEMA,
        embedding_dim=dim,
        command_timeout_s=1800,
        # Uma conexao so: e nela que o plano generico apareceria.
        pool_min_size=1,
        pool_max_size=1,
    )
    report: dict[str, Any] = {"chunks": chunks, "dim": dim, "checks": {}}
    checks: dict[str, bool] = report["checks"]
    try:
        async with await NativeMemoryProvider.connect(
            settings, embedder=HashingEmbeddingProvider(dim)
        ) as memory:
            report["load_s"] = round(await _load(memory, chunks, dim), 1)
            scope = MemoryScopeFilter.for_user(UserContext(user_id="ana", departments=["noc"]))

            visible = await _contract_of(
                memory, "d.access_level = 'OPERATIONAL' AND d.department_scope = '{NOC}'"
            )
            hidden = await _contract_of(memory, "d.access_level = 'EXECUTIVE'")
            foreign = await _contract_of(
                memory, "d.access_level = 'OPERATIONAL' AND d.department_scope = '{FINANCEIRO}'"
            )
            query = f"reajuste do contrato {visible} latencia"

            latencies: list[float] = []
            for _ in range(RUNS):
                response = await memory.search_wiki_detailed(query, scope, limit=5)
                latencies.append(round(response.elapsed_ms, 1))
            early = statistics.median(latencies[1 : GENERIC_PLAN_FROM - 1])
            late = statistics.median(latencies[GENERIC_PLAN_FROM - 1 :])
            report["latency_ms"] = latencies
            report["latency_regime"] = {"early": early, "late": late}
            checks["sem_regressao_de_plano_generico"] = (
                late <= early * MAX_REGIME_RATIO + REGIME_SLACK_MS
            )

            top = response.results[0]
            report["top1"] = {"dense_rank": top.dense_rank, "sparse_rank": top.sparse_rank}
            checks["identificador_exato_e_top1"] = visible in top.content

            for label, target in (("executivo", hidden), ("outro_departamento", foreign)):
                leaked = await memory.search_wiki(f"contrato {target}", scope, limit=50)
                checks[f"escopo_nao_vaza_{label}"] = not any(target in r.content for r in leaked)
            everything = await memory.search_wiki("contrato latencia", scope, limit=50)
            checks["escopo_so_operacional_do_noc"] = all(
                r.access_level is AccessLevel.OPERATIONAL
                and (not r.department_scope or "NOC" in r.department_scope)
                for r in everything
            )

            common = await memory.search_wiki_detailed("contrato latencia reajuste", scope)
            report["ubiquitous_words_ms"] = round(common.elapsed_ms, 1)
            checks["palavra_onipresente_tem_teto"] = common.elapsed_ms < 150

            levels, cross, departments = document_scope_args(scope)
            embedding = await HashingEmbeddingProvider(dim).embed(query)
            async with memory._pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT set_config('hnsw.ef_search', '50', true)")
                await conn.execute(
                    "SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"
                )
                tsq_and, tsq_candidates, lexemes = await memory.retrieval._lexical_plan(conn, query)
                plan = await conn.fetch(
                    "EXPLAIN (ANALYZE, COSTS OFF) " + HYBRID_SEARCH_SQL,
                    embedding,
                    tsq_and,
                    tsq_candidates,
                    lexemes,
                    levels,
                    cross,
                    departments,
                    None,
                    None,
                    None,
                    None,
                    50,
                    60,
                    5,
                    None,
                    settings.sparse_candidate_cap,
                )
            text = "\n".join(row[0] for row in plan)
            report["candidate_tsquery"] = tsq_candidates
            checks["plano_usa_hnsw"] = "wiki_chunks_embedding_hnsw" in text
            checks["plano_usa_gin"] = "wiki_chunks_tsv_gin" in text
    finally:
        await _drop_schema(dsn)
    report["ok"] = all(checks.values())
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--chunks", type=int, default=30_000)
    parser.add_argument("--dim", type=int, default=256)
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args()

    report = asyncio.run(run(args.chunks, args.dim))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
