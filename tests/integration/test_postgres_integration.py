"""Integration tests requiring a real PostgreSQL instance.

Run with: ORKMIND_TEST_DATABASE_URL=postgresql://.../orkmind_test pytest tests/integration/ -v
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


@pytest_asyncio.fixture
async def adapter():
    assert_destrutivo_permitido(ALVO)
    pg = PostgresAdapter(DATABASE_URL)
    await pg.initialize()
    conn = await pg._get_conn()
    await conn.execute("DELETE FROM memories")
    yield pg
    await conn.execute("DELETE FROM memories")
    await pg.close()


@pytest.mark.asyncio
async def test_full_crud_cycle(adapter: PostgresAdapter) -> None:
    entry = MemoryEntry(
        content="Integration test rule",
        collection="rule",
        tags={"skill": ["testing"], "domain": ["backend"]},
        priority="high",
        mandatory=True,
    )

    # Create
    entry_id = await adapter.store(entry)
    assert entry_id

    # Read
    retrieved = await adapter.retrieve(entry_id)
    assert retrieved is not None
    assert retrieved.content == "Integration test rule"
    assert retrieved.tags == {"skill": ["testing"], "domain": ["backend"]}
    assert retrieved.mandatory is True

    # Update
    entry.content = "Updated integration test rule"
    await adapter.update(entry_id, entry)
    updated = await adapter.retrieve(entry_id)
    assert updated is not None
    assert updated.content == "Updated integration test rule"
    assert updated.version == 2

    # Delete
    await adapter.delete(entry_id)
    gone = await adapter.retrieve(entry_id)
    assert gone is None


@pytest.mark.asyncio
async def test_fts_search(adapter: PostgresAdapter) -> None:
    await adapter.store(MemoryEntry(
        content="PostgreSQL database migration guide",
        collection="docs",
    ))
    await adapter.store(MemoryEntry(
        content="React component styling best practices",
        collection="docs",
    ))

    results = await adapter.search_by_text("database migration")
    assert len(results) >= 1
    assert "migration" in results[0].content.lower()


@pytest.mark.asyncio
async def test_tag_containment_search(adapter: PostgresAdapter) -> None:
    await adapter.store(MemoryEntry(
        content="Git branching strategy",
        collection="instruction",
        tags={"skill": ["git"], "situation": ["code-review"]},
    ))
    await adapter.store(MemoryEntry(
        content="Docker build optimization",
        collection="instruction",
        tags={"skill": ["docker"], "domain": ["infra"]},
    ))

    # Search for git skills
    results = await adapter.search_by_tags(tags={"skill": ["git"]})
    assert len(results) == 1
    assert "Git" in results[0].content

    # Multi-dimension search
    results = await adapter.search_by_tags(
        tags={"skill": ["docker"], "domain": ["infra"]}
    )
    assert len(results) == 1
    assert "Docker" in results[0].content


@pytest.mark.asyncio
async def test_mandatory_ordering(adapter: PostgresAdapter) -> None:
    await adapter.store(MemoryEntry(
        content="optional fact",
        collection="fact",
        tags={"skill": ["deploy"]},
        mandatory=False,
        priority="low",
    ))
    await adapter.store(MemoryEntry(
        content="MANDATORY RULE",
        collection="rule",
        tags={"skill": ["deploy"]},
        mandatory=True,
        priority="critical",
    ))

    results = await adapter.search_by_tags(tags={"skill": ["deploy"]})
    assert results[0].mandatory is True
    assert results[0].content == "MANDATORY RULE"
