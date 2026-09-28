"""Popula o banco descartavel do benchmark.

REGRA DE OURO: este script NUNCA pode rodar contra a base de producao.

Em 29/08/2026 uma execucao de `pytest tests/` com `ORKMIND_DATABASE_URL`
apontando para a base de producao destruiu 18 entries reais. A guarda que
nasceu daquele incidente vive em `tests/conftest.py` e e REUSADA aqui por
import direto, nao reimplementada: duas copias da mesma guarda divergem, e a
copia que diverge e a que apaga producao.

Uso:
    python bench/harness/seed.py                 usa ORKMIND_BENCH_DATABASE_URL
    python bench/harness/seed.py --drop          descarta o banco ao final
    python bench/harness/seed.py --url <dsn>     DSN explicito

Exit codes:
    0  banco semeado
    2  URL recusada pela guarda (nome sem marcador de teste)
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

BENCH_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BENCH_DIR.parent
DATASETS = BENCH_DIR / "datasets"

sys.path.insert(0, str(BENCH_DIR / "harness"))

# Banco padrao do benchmark. O nome contem "test", exigencia da guarda.
DEFAULT_BENCH_URL = "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind_bench_test"


def _carregar_guarda():
    """Importa a guarda de banco de testes de `tests/conftest.py`.

    O diretorio `tests/` nao e pacote instalavel, entao o import e por
    caminho de arquivo. Deliberadamente sem fallback: se a guarda nao puder
    ser carregada, o benchmark NAO roda. Melhor falhar do que rodar sem rede
    de protecao.
    """
    conftest = REPO_DIR / "tests" / "conftest.py"
    spec = importlib.util.spec_from_file_location("orkmind_conftest_guarda", conftest)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Nao foi possivel carregar a guarda de {conftest}")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


GUARDA = _carregar_guarda()


def resolver_url(explicita: str | None = None) -> str:
    """URL do banco do bench, validada pela guarda do conftest."""
    url = (
        explicita
        or os.environ.get("ORKMIND_BENCH_DATABASE_URL", "")
        or DEFAULT_BENCH_URL
    )
    if not GUARDA.is_test_database(url):
        nome = urlparse(url).path.lstrip("/")
        print(
            f"RECUSADO: o banco '{nome}' nao tem marcador de teste no nome "
            f"({', '.join(GUARDA.TEST_DB_MARKERS)}).\n"
            f"O seed do benchmark APAGA todas as linhas do banco apontado.\n"
            f"Use um banco descartavel, por exemplo '{DEFAULT_BENCH_URL}'.",
            file=sys.stderr,
        )
        # Exit 2 e distinto de 1 de proposito: o orquestrador precisa
        # diferenciar "guarda recusou o banco" de qualquer outra falha.
        raise SystemExit(2)
    return url


def _nome_do_banco(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def _url_administrativa(url: str) -> str:
    """Mesma URL apontando para o banco `postgres`, para CREATE/DROP."""
    return url.rsplit("/", 1)[0] + "/postgres"


def carregar_datasets() -> dict:
    return {
        nome: json.loads((DATASETS / f"{nome}.json").read_text(encoding="utf-8"))
        for nome in ("constitutional", "thematic", "distractors")
    }


async def _garantir_banco(url: str) -> None:
    import psycopg

    nome = _nome_do_banco(url)
    async with await psycopg.AsyncConnection.connect(
        _url_administrativa(url), autocommit=True
    ) as conn:
        existe = await (
            await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (nome,))
        ).fetchone()
        if not existe:
            await conn.execute(f'CREATE DATABASE "{nome}"')
            print(f"[seed] banco '{nome}' criado")
        else:
            print(f"[seed] banco '{nome}' ja existe")

    # A extensao pgvector precisa existir ANTES do adapter registrar o tipo
    # `vector` na conexao, senao o initialize falha.
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    print("[seed] extensao pgvector garantida")


async def _aplicar_migrations(url: str) -> None:
    import psycopg

    diretorio = REPO_DIR / "migrations"
    if not diretorio.is_dir():
        return
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        for arquivo in sorted(diretorio.glob("*.sql")):
            try:
                await conn.execute(arquivo.read_text(encoding="utf-8"))
                print(f"[seed] migration aplicada: {arquivo.name}")
            except Exception as e:  # noqa: BLE001
                print(f"[seed] migration {arquivo.name} ignorada: {e}")


async def _limpar(url: str) -> None:
    """Apaga TODAS as linhas. So chega aqui apos a guarda aprovar a URL."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute("DELETE FROM memories")
    print("[seed] tabela memories limpa (re-seed idempotente)")


async def semear(url: str) -> dict[str, int]:
    """Cria o schema e insere regras, memorias tematicas e ruido."""
    from fake_embedder import fake_embedding

    from orkmind.core.config import OrkMindConfig
    from orkmind.core.models import MemoryEntry
    from orkmind.store.factory import create_store

    await _garantir_banco(url)

    cfg = OrkMindConfig(database_url=url, embedding_dim=1024)
    store = create_store(cfg)
    await store.initialize()  # cria o schema completo do OrkMind
    await store.close()

    await _aplicar_migrations(url)
    await _limpar(url)

    dados = carregar_datasets()
    contagens = {"regras": 0, "tematicas": 0, "ruido": 0}

    store = create_store(cfg)
    await store.initialize()
    try:
        for regra in dados["constitutional"]["regras"]:
            await store.store(
                MemoryEntry(
                    id=regra["id"],
                    content=regra["content"],
                    collection=regra["collection"],
                    tags=regra.get("tags", {}),
                    priority=regra["priority"],
                    mandatory=regra["mandatory"],
                    protected=regra.get("protected", False),
                    source="human",
                    scope="global",
                    embedding=fake_embedding(regra["content"]),
                )
            )
            contagens["regras"] += 1

        for mem in dados["thematic"]["memorias"]:
            await store.store(
                MemoryEntry(
                    id=mem["id"],
                    content=mem["content"],
                    collection=mem["collection"],
                    tags=mem.get("tags", {}),
                    priority="medium",
                    mandatory=False,
                    source="human",
                    embedding=fake_embedding(mem["content"]),
                )
            )
            contagens["tematicas"] += 1

        for mem in dados["distractors"]["memorias"]:
            await store.store(
                MemoryEntry(
                    id=mem["id"],
                    content=mem["content"],
                    collection=mem["collection"],
                    tags=mem.get("tags", {}),
                    priority=mem.get("priority", "low"),
                    mandatory=False,
                    source="human",
                    embedding=fake_embedding(mem["content"]),
                )
            )
            contagens["ruido"] += 1
    finally:
        await store.close()

    return contagens


async def descartar(url: str) -> None:
    import psycopg

    nome = _nome_do_banco(url)
    async with await psycopg.AsyncConnection.connect(
        _url_administrativa(url), autocommit=True
    ) as conn:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (nome,),
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{nome}"')
    print(f"[seed] banco '{nome}' descartado")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="DSN do banco descartavel do benchmark")
    parser.add_argument(
        "--drop", action="store_true", help="descartar o banco em vez de semear"
    )
    args = parser.parse_args()

    url = resolver_url(args.url)
    print(f"[seed] banco do benchmark: {_nome_do_banco(url)}")

    if args.drop:
        asyncio.run(descartar(url))
        return 0

    contagens = asyncio.run(semear(url))
    total = sum(contagens.values())
    print(
        f"[seed] concluido: {contagens['regras']} regras, "
        f"{contagens['tematicas']} memorias tematicas, "
        f"{contagens['ruido']} distratores ({total} entries)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
