"""Testes F1: ontologia expandida no armazenamento (B4/B5/B6).

Cobrem a migracao SQL, o CRUD com os novos campos (url, visibility,
author_id, parent_id), navegacao hierarquica via get_children() e
retrocompatibilidade com entries antigas.

Requer um banco de TESTES (a fixture apaga todos os dados):
    ORKMIND_TEST_DATABASE_URL=postgresql://.../orkmind_test pytest tests/integration
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from orkmind.core.models import MemoryEntry
from orkmind.store.postgres_adapter import PostgresAdapter
from tests.conftest import (
    SKIP_NO_TEST_DB,
    assert_destrutivo_permitido,
    resolve_alvo_de_teste,
    resolve_test_database_url,
)

DATABASE_URL = resolve_test_database_url()
# F3.3: cinto e suspensorio. A resolucao acima ja filtrou o alvo; a
# assercao volta a checar no momento exato do apagamento.
ALVO = resolve_alvo_de_teste("pgvector")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason=SKIP_NO_TEST_DB),
]

NOVAS_COLUNAS = ("url", "visibility", "author_id", "parent_id")


@pytest_asyncio.fixture
async def store():
    assert_destrutivo_permitido(ALVO)
    adapter = PostgresAdapter(DATABASE_URL)
    await adapter.initialize()
    conn = await adapter._get_conn()
    await conn.execute("DELETE FROM memory_versions")
    await conn.execute("DELETE FROM memories")
    yield adapter
    await conn.execute("DELETE FROM memory_versions")
    await conn.execute("DELETE FROM memories")
    await adapter.close()


async def _colunas(store: PostgresAdapter) -> dict[str, dict]:
    conn = await store._get_conn()
    cur = await conn.execute(
        """
        SELECT column_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = 'memories'
        """
    )
    return {r["column_name"]: dict(r) for r in await cur.fetchall()}


# --- Migracao SQL -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("coluna", NOVAS_COLUNAS)
async def test_migracao_cria_coluna(store: PostgresAdapter, coluna: str) -> None:
    assert coluna in await _colunas(store)


@pytest.mark.asyncio
async def test_visibility_tem_default_private(store: PostgresAdapter) -> None:
    coluna = (await _colunas(store))["visibility"]
    assert "private" in (coluna["column_default"] or "")
    assert coluna["is_nullable"] == "NO"


@pytest.mark.asyncio
async def test_migracao_e_idempotente(store: PostgresAdapter) -> None:
    await store.initialize()
    await store.initialize()
    colunas = await _colunas(store)
    for coluna in NOVAS_COLUNAS:
        assert coluna in colunas


@pytest.mark.asyncio
async def test_indices_dos_novos_campos(store: PostgresAdapter) -> None:
    conn = await store._get_conn()
    cur = await conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'memories'"
    )
    indices = {r["indexname"] for r in await cur.fetchall()}
    assert {
        "idx_memories_visibility",
        "idx_memories_author_id",
        "idx_memories_parent_id",
    } <= indices


# --- CRUD com novos campos --------------------------------------------------


@pytest.mark.asyncio
async def test_store_persiste_novos_campos(store: PostgresAdapter) -> None:
    entry = MemoryEntry(
        content="Artigo publicado",
        collection="artifact",
        metadata={"artifact_type": "article"},
        url="https://exemplo.dev/artigo",
        visibility="public",
        author_id="tomas",
    )
    entry_id = await store.store(entry)
    recuperada = await store.retrieve(entry_id)
    assert recuperada is not None
    assert recuperada.url == "https://exemplo.dev/artigo"
    assert recuperada.visibility == "public"
    assert recuperada.author_id == "tomas"
    assert recuperada.parent_id is None


@pytest.mark.asyncio
async def test_defaults_quando_campos_ausentes(store: PostgresAdapter) -> None:
    entry_id = await store.store(MemoryEntry(content="simples", collection="fact"))
    recuperada = await store.retrieve(entry_id)
    assert recuperada is not None
    assert recuperada.visibility == "private"
    assert recuperada.url is None
    assert recuperada.author_id is None


@pytest.mark.asyncio
async def test_update_altera_novos_campos(store: PostgresAdapter) -> None:
    entry = MemoryEntry(content="rascunho", collection="content")
    entry_id = await store.store(entry)
    atualizada = entry.model_copy(
        update={
            "url": "https://exemplo.dev/final",
            "visibility": "restricted",
            "author_id": "ana",
            "parent_id": "pai-1",
        }
    )
    assert await store.update(entry_id, atualizada) is True
    recuperada = await store.retrieve(entry_id)
    assert recuperada is not None
    assert recuperada.url == "https://exemplo.dev/final"
    assert recuperada.visibility == "restricted"
    assert recuperada.author_id == "ana"
    assert recuperada.parent_id == "pai-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["public", "private", "restricted"])
async def test_todas_as_visibilidades(
    store: PostgresAdapter, visibility: str
) -> None:
    entry_id = await store.store(
        MemoryEntry(content="x", collection="fact", visibility=visibility)  # type: ignore[arg-type]
    )
    recuperada = await store.retrieve(entry_id)
    assert recuperada is not None and recuperada.visibility == visibility


@pytest.mark.asyncio
async def test_busca_por_tags_devolve_novos_campos(store: PostgresAdapter) -> None:
    await store.store(
        MemoryEntry(
            content="com autor",
            collection="fact",
            tags={"domain": ["backend"]},
            author_id="tomas",
            visibility="public",
        )
    )
    encontrados = await store.search_by_tags(tags={"domain": ["backend"]})
    assert encontrados[0].author_id == "tomas"
    assert encontrados[0].visibility == "public"


@pytest.mark.asyncio
async def test_busca_textual_devolve_novos_campos(store: PostgresAdapter) -> None:
    await store.store(
        MemoryEntry(
            content="documento sobre pgvector",
            collection="docs",
            url="https://exemplo.dev/doc",
        )
    )
    encontrados = await store.search_by_text("pgvector")
    assert encontrados[0].url == "https://exemplo.dev/doc"


# --- Novas colecoes no armazenamento ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collection,metadata",
    [
        ("session", {"session_id": "20260828_1200"}),
        ("artifact", {"artifact_type": "report"}),
        ("compliance", {"compliance_type": "compliance_review"}),
        ("semantic_log", {"package_id": "pkg-1"}),
    ],
)
async def test_novas_colecoes_persistem(
    store: PostgresAdapter, collection: str, metadata: dict
) -> None:
    entry_id = await store.store(
        MemoryEntry(
            content=f"entry de {collection}",
            collection=collection,  # type: ignore[arg-type]
            metadata=metadata,
        )
    )
    recuperada = await store.retrieve(entry_id)
    assert recuperada is not None
    assert recuperada.collection == collection
    assert recuperada.metadata == metadata


@pytest.mark.asyncio
async def test_novas_colecoes_aparecem_no_list_collections(
    store: PostgresAdapter,
) -> None:
    for collection in ("session", "artifact", "compliance", "semantic_log"):
        await store.store(
            MemoryEntry(content="x", collection=collection)  # type: ignore[arg-type]
        )
    colecoes = set(await store.list_collections())
    assert {"session", "artifact", "compliance", "semantic_log"} <= colecoes


@pytest.mark.asyncio
async def test_count_por_nova_colecao(store: PostgresAdapter) -> None:
    for i in range(3):
        await store.store(
            MemoryEntry(content=f"pacote {i}", collection="semantic_log")
        )
    assert await store.count(collection="semantic_log") == 3


# --- Hierarquia (get_children) ---------------------------------------------


@pytest.mark.asyncio
async def test_get_children_retorna_filhas(store: PostgresAdapter) -> None:
    pai_id = await store.store(
        MemoryEntry(content="sessao pai", collection="session")
    )
    for i in range(3):
        await store.store(
            MemoryEntry(
                content=f"pacote {i}", collection="semantic_log", parent_id=pai_id
            )
        )
    filhas = await store.get_children(pai_id)
    assert len(filhas) == 3
    assert all(f.parent_id == pai_id for f in filhas)


@pytest.mark.asyncio
async def test_get_children_sem_filhas(store: PostgresAdapter) -> None:
    entry_id = await store.store(MemoryEntry(content="solitaria", collection="fact"))
    assert await store.get_children(entry_id) == []


@pytest.mark.asyncio
async def test_get_children_id_inexistente(store: PostgresAdapter) -> None:
    assert await store.get_children("nao-existe") == []


@pytest.mark.asyncio
async def test_get_children_ordena_por_criacao(store: PostgresAdapter) -> None:
    pai_id = await store.store(MemoryEntry(content="pai", collection="session"))
    for i in range(4):
        await store.store(
            MemoryEntry(
                content=f"filha {i}", collection="fact", parent_id=pai_id
            )
        )
    filhas = await store.get_children(pai_id)
    assert [f.content for f in filhas] == [f"filha {i}" for i in range(4)]


@pytest.mark.asyncio
async def test_get_children_ignora_expiradas(store: PostgresAdapter) -> None:
    from datetime import datetime, timedelta, timezone

    pai_id = await store.store(MemoryEntry(content="pai", collection="session"))
    await store.store(
        MemoryEntry(content="viva", collection="fact", parent_id=pai_id)
    )
    await store.store(
        MemoryEntry(
            content="expirada",
            collection="fact",
            parent_id=pai_id,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    filhas = await store.get_children(pai_id)
    assert [f.content for f in filhas] == ["viva"]


# --- Retrocompatibilidade ---------------------------------------------------


@pytest.mark.asyncio
async def test_entry_legada_sem_novos_campos(store: PostgresAdapter) -> None:
    """Linha inserida direto no SQL, como uma entry anterior a migracao."""
    conn = await store._get_conn()
    await conn.execute(
        """
        INSERT INTO memories (id, content, collection, tags, priority,
            mandatory, scope, source, version, created_at, updated_at, metadata)
        VALUES ('legada-1', 'entry antiga', 'fact', '{}', 'medium',
            false, 'global', 'human', 1, now(), now(), '{}')
        """
    )
    recuperada = await store.retrieve("legada-1")
    assert recuperada is not None
    assert recuperada.visibility == "private"
    assert recuperada.url is None
    assert recuperada.author_id is None
    assert recuperada.parent_id is None


@pytest.mark.asyncio
async def test_snapshot_preserva_novos_campos(store: PostgresAdapter) -> None:
    entry_id = await store.store(
        MemoryEntry(
            content="com metadados",
            collection="artifact",
            url="https://exemplo.dev/a",
            visibility="public",
            author_id="tomas",
        )
    )
    snapshot_id = await store.snapshot_commit(label="teste-f1")
    conn = await store._get_conn()
    await conn.execute("DELETE FROM memories")
    await store.snapshot_restore(snapshot_id)

    restaurada = await store.retrieve(entry_id)
    assert restaurada is not None
    assert restaurada.url == "https://exemplo.dev/a"
    assert restaurada.visibility == "public"
    assert restaurada.author_id == "tomas"


@pytest.mark.asyncio
async def test_versionamento_continua_funcionando(store: PostgresAdapter) -> None:
    entry = MemoryEntry(content="v1", collection="fact", author_id="tomas")
    entry_id = await store.store(entry)
    await store.update(entry_id, entry.model_copy(update={"content": "v2"}))
    historico = await store.get_history(entry_id)
    assert len(historico) == 1
    assert historico[0].content == "v1"
