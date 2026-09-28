"""Backfill de embeddings para entries existentes sem vetor.

Uso:
    python -m orkmind.embeddings.backfill [--collection COLL] [--limit N] [--batch-size B]

Percorre entries sem embedding, gera o vetor via provider configurado e
grava pelo contrato do store. Funciona em qualquer backend que declare
`capabilities.backfill=True`.

F3.6: este modulo nao fala mais SQL nem pesca a conexao interna do
adapter. Ele usa `list_entries_without_embedding` e `set_embedding`, que
sao parte do contrato `MemoryStore`. Era o ultimo vazamento de backend
no codigo Python.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import click

from orkmind.core.config import load_config
from orkmind.store.factory import create_embedder, create_store

logger = logging.getLogger(__name__)


async def backfill(
    collection: str | None = None,
    limit: int = 1000,
    batch_size: int = 16,
) -> int:
    """Gera embeddings para entries sem vetor.

    Retorna o numero de entries atualizadas.
    """
    config = load_config()
    store = create_store(config)
    await store.initialize()

    capabilities = store.capabilities
    if not capabilities.backfill:
        # R0.3: capacidade ausente e alta e nomeada, nunca um zero mudo.
        logger.error(
            "Backend '%s' declara backfill=False: nao implementa a listagem "
            "de pendentes nem a gravacao de vetor. Nada a fazer.",
            capabilities.backend,
        )
        await store.close()
        return 0
    if not capabilities.stores_embedding:
        logger.error(
            "Backend '%s' declara stores_embedding=False: gerar vetores nao "
            "traria ganho nenhum, porque eles nao seriam guardados.",
            capabilities.backend,
        )
        await store.close()
        return 0

    # Timeout longo de proposito: aqui o embedding roda em lote, fora do
    # turno do agente, e cortar em 8s so faria o backfill falhar a toa.
    embedder = create_embedder(config, timeout_s=60.0)
    if embedder is None:
        logger.error(
            "Embedder nao disponivel. Configure [embedding] no config.toml "
            "e defina a variavel de ambiente com a API key."
        )
        await store.close()
        return 0

    pendentes = await store.list_entries_without_embedding(
        collection=collection, limit=limit
    )

    if not pendentes:
        logger.info("Nenhuma entry sem embedding encontrada.")
        await store.close()
        return 0

    logger.info(
        "Encontradas %d entries sem embedding no backend '%s'. Gerando vetores...",
        len(pendentes),
        capabilities.backend,
    )

    updated = 0
    for i in range(0, len(pendentes), batch_size):
        batch = pendentes[i : i + batch_size]
        texts = [e.content for e in batch]
        ids = [e.id for e in batch]

        # Retry simples com backoff para rate limit
        embeddings = None
        for attempt in range(3):
            try:
                embeddings = await embedder.embed_batch(texts)
                break
            except Exception:
                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Tentativa %d falhou, aguardando %ds...",
                        attempt + 1, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Erro ao gerar batch de embeddings (entries %d-%d) apos 3 tentativas",
                        i, i + len(batch),
                        exc_info=True,
                    )
        if embeddings is None:
            continue

        for entry_id, emb in zip(ids, embeddings):
            try:
                if await store.set_embedding(entry_id, emb):
                    updated += 1
                else:
                    logger.warning(
                        "Entry %s nao encontrada ao gravar o embedding.", entry_id
                    )
            except Exception:
                logger.error(
                    "Erro ao salvar embedding da entry %s", entry_id,
                    exc_info=True,
                )

        logger.info("Batch %d-%d concluido (%d atualizadas ate agora)",
                     i, i + len(batch), updated)

    await store.close()
    return updated


@click.command()
@click.option("--collection", "-c", default=None, help="Filtrar por colecao")
@click.option("--limit", "-n", default=1000, help="Maximo de entries a processar")
@click.option("--batch-size", "-b", default=16, help="Tamanho do batch para a API")
def main(collection: str | None, limit: int, batch_size: int) -> None:
    """Backfill de embeddings para entries existentes."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    count = asyncio.run(backfill(collection=collection, limit=limit, batch_size=batch_size))
    click.echo(f"\nBackfill concluido: {count} entries atualizadas com embedding.")
    if count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
