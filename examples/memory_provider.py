"""Memory Provider nativo de ponta a ponta, sem rede.

Usa o `HashingEmbeddingProvider`, que NAO e semantico: serve para ver o fluxo
inteiro rodar sem chave de API. Em producao, omita `embedder=` e configure
`ORKMIND_PROVIDER_EMBEDDING_MODEL` / `OPENROUTER_API_KEY`.

    docker run --rm -d --name orkmind-mp-pg -e POSTGRES_PASSWORD=senha-de-teste \
        -e POSTGRES_DB=memory_provider -p 127.0.0.1:5433:5432 pgvector/pgvector:pg17
    export ORKMIND_PROVIDER_DATABASE_URL=postgresql://postgres:senha-de-teste@127.0.0.1:5433/memory_provider
    pip install 'orkmind[memory-provider]'
    python examples/memory_provider.py
"""

from __future__ import annotations

import asyncio

from pydantic import ValidationError

from orkmind.memory_provider import (
    AccessLevel,
    DocumentIngestRequest,
    HashingEmbeddingProvider,
    MemoryProviderSettings,
    MemoryScopeFilter,
    NativeMemoryProvider,
    UserContext,
)

DIM = 256

CONTRATO = """# Contrato de Transito IP

## Valores

O valor mensal do contrato CT-2024/0187 e de R$ 184.500,00, reajustado pelo IPCA.

## SLA

Disponibilidade minima de 99,95% medida mensalmente.
"""

ESTUDO = """# Estudo de Aquisicao

## Tese

Avaliacao sigilosa da aquisicao de um provedor regional. Nao divulgar.
"""


async def main() -> None:
    settings = MemoryProviderSettings.from_env(
        db_schema="memory_provider_example", embedding_dim=DIM
    )
    embedder = HashingEmbeddingProvider(DIM)

    async with await NativeMemoryProvider.connect(settings, embedder=embedder) as memory:
        # Tier 1 - Core Memory (API de operador; o agente nao altera isto).
        await memory.set_core_block(
            "assistant", "persona", "Voce e o assistente corporativo da Acme.", created_by="ops"
        )
        await memory.set_core_block(
            "assistant", "system_invariants", "Nunca exponha dado acima do papel.", created_by="ops"
        )

        # Tier 3 - ingestao idempotente, com classificacao obrigatoria.
        for request in (
            DocumentIngestRequest(
                slug="contrato-transito-ip",
                title="Contrato de Transito IP",
                doc_type="opex_analysis",
                raw_content=CONTRATO,
                access_level=AccessLevel.OPERATIONAL,
                department_scope=["financeiro"],
                index_paths=["FINANCAS_OPEX/CONTRATOS_TI"],
            ),
            DocumentIngestRequest(
                slug="estudo-aquisicao",
                title="Estudo de Aquisicao",
                doc_type="deep_research",
                raw_content=ESTUDO,
                access_level=AccessLevel.EXECUTIVE,
                index_paths=["PESQUISA/AQUISICAO"],
            ),
        ):
            result = await memory.ingest_document(request)
            print(f"ingestao {result.slug}: {result.status.value} v{result.version}")

        # Identidade vem do canal autenticado (Teams, Slack, sua API), nunca do agente.
        caio = UserContext(user_id="caio", role=AccessLevel.OPERATIONAL, departments=["financeiro"])
        dora = UserContext(user_id="dora", role=AccessLevel.EXECUTIVE, departments=["diretoria"])

        # Meta-indice: navegar antes de buscar.
        for user in (caio, dora):
            tree = await memory.browse_index(MemoryScopeFilter.for_user(user), depth=2)
            print(f"indice visto por {user.user_id}: {[node.path for node in tree]}")

        # Busca hibrida (densa + lexica, RRF) sob pre-filtro de escopo.
        hits = await memory.search_wiki(
            "qual o valor do contrato CT-2024/0187?", MemoryScopeFilter.for_user(caio)
        )
        top = hits[0]
        print(f"top-1: {top.heading_path} (dense={top.dense_rank}, sparse={top.sparse_rank})")

        sigiloso = await memory.search_wiki(
            "aquisicao de provedor regional", MemoryScopeFilter.for_user(caio)
        )
        print(f"operacional ve o estudo? {any(h.slug == 'estudo-aquisicao' for h in sigiloso)}")

        try:
            forjado = MemoryScopeFilter(user_role=AccessLevel.EXECUTIVE)
            await memory.search_wiki("aquisicao", forjado, user_context=caio)
        except ValidationError as exc:
            print(f"escalada barrada pelo guard: {exc.errors()[0]['msg']}")

        # Tier 2 - historico append-only; o orquestrador so ve a janela.
        session = "demo:caio"
        for n in range(1, 9):
            await memory.append_history(session, "user", f"pergunta {n}", user_id="caio")
            await memory.append_history(session, "assistant", f"resposta {n}")

        context = await memory.assemble_turn_context(
            "assistant", session, caio, "reajuste do contrato CT-2024/0187"
        )
        turns = sorted({m.turn_index for m in context.history})
        total = len(await memory.get_full_history(session))
        print(f"janela: turnos {turns} | no banco: {total} mensagens")
        print(f"tokens do turno: {context.tokens} = {context.token_total}")


if __name__ == "__main__":
    asyncio.run(main())
