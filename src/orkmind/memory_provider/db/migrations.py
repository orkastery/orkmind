"""Migracao e pool de conexoes do Memory Provider nativo.

`apply_schema` renderiza `schema.sql` e executa tudo numa transacao, atras de
um advisory lock, entao dois processos subindo juntos nao colidem. O DDL e
idempotente; o que NAO e idempotente - trocar a dimensao do vetor ou o idioma
do full-text - e detectado e recusado com `SchemaMismatchError`.

O driver assincrono e importado SOB DEMANDA (mesmo padrao de
`orkmind.store.factory`): o pacote inteiro continua importavel sem o extra
`orkmind[memory-provider]`, e so conectar de fato exige o asyncpg.
"""

from __future__ import annotations

import json
import logging
from importlib import resources
from typing import TYPE_CHECKING, Any

from orkmind.memory_provider.config import MemoryProviderSettings
from orkmind.memory_provider.errors import MemoryProviderError, SchemaMismatchError

if TYPE_CHECKING:  # pragma: no cover - so para anotacao
    import asyncpg

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2"  # 2: teia de links + tags semanticas

# Chave fixa do advisory lock de migracao ("orkm" em ASCII).
_MIGRATION_LOCK_KEY = 0x6F726B6D


def _require_driver() -> Any:
    """Importa o asyncpg sob demanda, com mensagem util se faltar."""
    try:
        import asyncpg  # import LAZY
    except ImportError as e:
        raise MemoryProviderError(
            "O Memory Provider precisa do asyncpg, que nao esta instalado. "
            'Rode: pip install "orkmind[memory-provider]"'
        ) from e
    return asyncpg


def render_schema(settings: MemoryProviderSettings) -> str:
    """DDL final. Os dois valores substituidos ja foram validados no settings."""
    template = (
        resources.files("orkmind.memory_provider.db").joinpath("schema.sql").read_text("utf-8")
    )
    return template.replace("{{embedding_dim}}", str(settings.embedding_dim)).replace(
        "{{fts_config}}", settings.fts_config
    )


def _server_settings(settings: MemoryProviderSettings, vector_schema: str | None) -> dict[str, str]:
    # O schema da extensao entra no search_path para o tipo `vector` e o
    # operador `<=>` resolverem sem qualificacao.
    path = [settings.db_schema]
    for extra in (vector_schema, "public"):
        if extra and extra not in path:
            path.append(extra)
    return {"search_path": ", ".join(path), "application_name": "orkmind-memory-provider"}


async def _vector_schema(conn: asyncpg.Connection) -> str | None:
    value = await conn.fetchval(
        "SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'vector'"
    )
    return None if value is None else str(value)


async def apply_schema(settings: MemoryProviderSettings) -> str:
    """Aplica o DDL e devolve o schema onde a extensao `vector` vive."""
    driver = _require_driver()
    dsn = settings.require_database_url()
    conn = await driver.connect(dsn, timeout=settings.command_timeout_s)
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _MIGRATION_LOCK_KEY)
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{settings.db_schema}"')
            # Extensao antes do search_path: CREATE EXTENSION instala no
            # primeiro schema do path, e ela deve ficar fora do schema do
            # provider para sobreviver a um DROP SCHEMA de testes.
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            vector_schema = settings.vector_schema or await _vector_schema(conn) or "public"
            search_path = _server_settings(settings, vector_schema)["search_path"]
            await conn.execute("SELECT set_config('search_path', $1, true)", search_path)
            await _check_compatibility(conn, settings)
            await conn.execute(render_schema(settings))
            await conn.executemany(
                """
                INSERT INTO memory_provider_meta (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                WHERE memory_provider_meta.value IS DISTINCT FROM EXCLUDED.value
                """,
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("embedding_dim", str(settings.embedding_dim)),
                    ("fts_config", settings.fts_config),
                ],
            )
        return vector_schema
    finally:
        await conn.close()


async def _check_compatibility(conn: asyncpg.Connection, settings: MemoryProviderSettings) -> None:
    exists = await conn.fetchval("SELECT to_regclass('memory_provider_meta') IS NOT NULL")
    if not exists:
        return
    rows = await conn.fetch("SELECT key, value FROM memory_provider_meta")
    installed = {row["key"]: row["value"] for row in rows}
    expected = {"embedding_dim": str(settings.embedding_dim), "fts_config": settings.fts_config}
    for key, want in expected.items():
        have = installed.get(key)
        if have is not None and have != want:
            raise SchemaMismatchError(
                f"schema '{settings.db_schema}' foi criado com {key}={have}, mas a "
                f"configuracao pede {key}={want}. Isso invalida os vetores/indices ja "
                f"gravados: use outro schema ou reingira os documentos numa base nova."
            )


async def create_pool(
    settings: MemoryProviderSettings, *, vector_schema: str | None = None
) -> asyncpg.Pool:
    """Pool com search_path fixo e codecs de `vector` e `jsonb` registrados."""
    driver = _require_driver()
    from pgvector.asyncpg import register_vector  # import LAZY (depende do asyncpg)

    dsn = settings.require_database_url()
    resolved_schema = vector_schema or settings.vector_schema

    async def _init(conn: asyncpg.Connection) -> None:
        schema = resolved_schema or await _vector_schema(conn) or "public"
        await register_vector(conn, schema=schema)
        for json_type in ("jsonb", "json"):
            await conn.set_type_codec(
                json_type, encoder=_json_dumps, decoder=json.loads, schema="pg_catalog"
            )

    pool = await driver.create_pool(
        dsn,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        command_timeout=settings.command_timeout_s,
        server_settings=_server_settings(settings, resolved_schema),
        init=_init,
    )
    assert pool is not None
    return pool


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def pgvector_version(pool: asyncpg.Pool) -> tuple[int, ...]:
    raw = await pool.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    if not raw:
        return (0,)
    return tuple(int(part) for part in str(raw).split(".") if part.isdigit())
