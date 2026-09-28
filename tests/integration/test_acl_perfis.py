"""Testes F2: perfis e controle de acesso (B14).

Cobrem o CRUD de profiles, o filtro de leitura (visibility + audience),
o filtro de escrita (editors, com D2 prevalecendo), a resolucao de
grupo de um nivel e a retrocompatibilidade de consultas sem requester.

Requer um banco de TESTES (a fixture apaga todos os dados):
    ORKMIND_TEST_DATABASE_URL=postgresql://.../orkmind_test pytest tests/integration
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from orkmind.core.models import MemoryEntry, Profile
from orkmind.core.ontology import ProtectionError
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
async def store():
    assert_destrutivo_permitido(ALVO)
    adapter = PostgresAdapter(DATABASE_URL)
    await adapter.initialize()
    conn = await adapter._get_conn()
    await conn.execute("DELETE FROM memory_versions")
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM profiles")
    yield adapter
    await conn.execute("DELETE FROM memory_versions")
    await conn.execute("DELETE FROM memories")
    await conn.execute("DELETE FROM profiles")
    await adapter.close()


@pytest_asyncio.fixture
async def perfis(store: PostgresAdapter):
    await store.profile_create(
        Profile(id="tomas", display_name="Tomas", profile_type="person")
    )
    await store.profile_create(
        Profile(id="alice", display_name="Alice", profile_type="person")
    )
    await store.profile_create(
        Profile(id="bob", display_name="Bob", profile_type="person")
    )
    await store.profile_create(
        Profile(
            id="hermes-briefing",
            display_name="Hermes Briefing",
            profile_type="agent",
        )
    )
    await store.profile_create(
        Profile(
            id="equipe",
            display_name="Equipe",
            profile_type="group",
            metadata={"members": ["alice", "tomas"]},
        )
    )
    return store


# --- CRUD de profiles -------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_create_e_get(store: PostgresAdapter) -> None:
    await store.profile_create(
        Profile(
            id="tomas",
            display_name="Tomas",
            profile_type="person",
            permissions={"is_admin": True},
        )
    )
    perfil = await store.profile_get("tomas")
    assert perfil is not None
    assert perfil.display_name == "Tomas"
    assert perfil.permissions == {"is_admin": True}


@pytest.mark.asyncio
async def test_profile_get_inexistente(store: PostgresAdapter) -> None:
    assert await store.profile_get("ninguem") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile_type", ["person", "agent", "system", "company", "group"]
)
async def test_todos_os_tipos_de_perfil(
    store: PostgresAdapter, profile_type: str
) -> None:
    await store.profile_create(
        Profile(
            id=f"p-{profile_type}",
            display_name=profile_type,
            profile_type=profile_type,  # type: ignore[arg-type]
        )
    )
    perfil = await store.profile_get(f"p-{profile_type}")
    assert perfil is not None and perfil.profile_type == profile_type


@pytest.mark.asyncio
async def test_profile_list_e_filtro_por_tipo(perfis: PostgresAdapter) -> None:
    todos = await perfis.profile_list()
    assert {p.id for p in todos} == {
        "tomas", "alice", "bob", "hermes-briefing", "equipe"
    }
    agentes = await perfis.profile_list(profile_type="agent")
    assert [p.id for p in agentes] == ["hermes-briefing"]


@pytest.mark.asyncio
async def test_profile_update(perfis: PostgresAdapter) -> None:
    perfil = await perfis.profile_get("bob")
    assert perfil is not None
    perfil.display_name = "Bob Silva"
    assert await perfis.profile_update("bob", perfil) is True
    assert (await perfis.profile_get("bob")).display_name == "Bob Silva"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_profile_update_inexistente(store: PostgresAdapter) -> None:
    perfil = Profile(id="x", display_name="X", profile_type="person")
    assert await store.profile_update("x", perfil) is False


@pytest.mark.asyncio
async def test_profile_delete(perfis: PostgresAdapter) -> None:
    assert await perfis.profile_delete("bob") is True
    assert await perfis.profile_get("bob") is None
    assert await perfis.profile_delete("bob") is False


@pytest.mark.asyncio
async def test_profile_create_e_idempotente(store: PostgresAdapter) -> None:
    perfil = Profile(id="tomas", display_name="Tomas", profile_type="person")
    await store.profile_create(perfil)
    await store.profile_create(perfil.model_copy(update={"display_name": "Tomas C"}))
    assert len(await store.profile_list()) == 1
    assert (await store.profile_get("tomas")).display_name == "Tomas C"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_tipo_de_perfil_invalido_rejeitado(store: PostgresAdapter) -> None:
    import psycopg

    perfil = Profile.model_construct(
        id="x", display_name="X", profile_type="alienigena", permissions={},
        metadata={}, parent_id=None,
    )
    from datetime import datetime, timezone

    perfil.created_at = datetime.now(timezone.utc)
    with pytest.raises(psycopg.errors.CheckViolation):
        await store.profile_create(perfil)


# --- Filtro de leitura ------------------------------------------------------


async def _criar_entries(store: PostgresAdapter) -> dict[str, str]:
    ids = {}
    ids["publica"] = await store.store(
        MemoryEntry(
            content="entry publica",
            collection="fact",
            visibility="public",
            author_id="tomas",
        )
    )
    ids["privada_tomas"] = await store.store(
        MemoryEntry(
            content="entry privada do tomas",
            collection="fact",
            visibility="private",
            author_id="tomas",
        )
    )
    ids["restrita_tomas"] = await store.store(
        MemoryEntry(
            content="entry restrita ao tomas",
            collection="fact",
            visibility="restricted",
            author_id="alice",
            tags={"audience": ["tomas"]},
        )
    )
    ids["restrita_equipe"] = await store.store(
        MemoryEntry(
            content="entry restrita a equipe",
            collection="fact",
            visibility="restricted",
            author_id="bob",
            tags={"audience": ["equipe"]},
        )
    )
    return ids


@pytest.mark.asyncio
async def test_leitura_sem_requester_retorna_tudo(perfis: PostgresAdapter) -> None:
    await _criar_entries(perfis)
    encontrados = await perfis.search_by_tags(tags={})
    assert len(encontrados) == 4


@pytest.mark.asyncio
async def test_publica_visivel_a_todos(perfis: PostgresAdapter) -> None:
    await _criar_entries(perfis)
    encontrados = await perfis.search_by_tags(tags={}, requester_id="bob")
    assert any(e.content == "entry publica" for e in encontrados)


@pytest.mark.asyncio
async def test_privada_visivel_apenas_ao_autor(perfis: PostgresAdapter) -> None:
    await _criar_entries(perfis)
    do_tomas = await perfis.search_by_tags(tags={}, requester_id="tomas")
    do_bob = await perfis.search_by_tags(tags={}, requester_id="bob")
    assert any(e.content == "entry privada do tomas" for e in do_tomas)
    assert not any(e.content == "entry privada do tomas" for e in do_bob)


@pytest.mark.asyncio
async def test_restrita_visivel_ao_audience(perfis: PostgresAdapter) -> None:
    await _criar_entries(perfis)
    do_tomas = await perfis.search_by_tags(tags={}, requester_id="tomas")
    assert any(e.content == "entry restrita ao tomas" for e in do_tomas)


@pytest.mark.asyncio
async def test_restrita_invisivel_fora_do_audience(perfis: PostgresAdapter) -> None:
    await _criar_entries(perfis)
    do_agente = await perfis.search_by_tags(
        tags={}, requester_id="hermes-briefing"
    )
    conteudos = {e.content for e in do_agente}
    assert "entry restrita ao tomas" not in conteudos
    assert "entry publica" in conteudos


@pytest.mark.asyncio
async def test_busca_textual_respeita_acl(perfis: PostgresAdapter) -> None:
    await _criar_entries(perfis)
    do_bob = await perfis.search_by_text("privada", requester_id="bob")
    assert do_bob == []
    do_tomas = await perfis.search_by_text("privada", requester_id="tomas")
    assert len(do_tomas) == 1


@pytest.mark.asyncio
async def test_can_read_espelha_o_sql(perfis: PostgresAdapter) -> None:
    entry = MemoryEntry(
        content="x",
        collection="fact",
        visibility="restricted",
        author_id="alice",
        tags={"audience": ["tomas"]},
    )
    assert perfis.can_read(entry, "tomas") is True
    assert perfis.can_read(entry, "alice") is True
    assert perfis.can_read(entry, "bob") is False
    assert perfis.can_read(entry, None) is True


# --- Resolucao de grupo -----------------------------------------------------


@pytest.mark.asyncio
async def test_is_member_of(perfis: PostgresAdapter) -> None:
    assert await perfis._is_member_of("alice", "equipe") is True
    assert await perfis._is_member_of("bob", "equipe") is False


@pytest.mark.asyncio
async def test_is_member_of_perfil_nao_grupo(perfis: PostgresAdapter) -> None:
    assert await perfis._is_member_of("alice", "tomas") is False


@pytest.mark.asyncio
async def test_resolve_identities_inclui_grupos(perfis: PostgresAdapter) -> None:
    identidades = await perfis.resolve_identities("alice")
    assert set(identidades) == {"alice", "equipe"}
    assert await perfis.resolve_identities("bob") == ["bob"]


@pytest.mark.asyncio
async def test_membro_de_grupo_le_entry_restrita(perfis: PostgresAdapter) -> None:
    await _criar_entries(perfis)
    da_alice = await perfis.search_by_tags(tags={}, requester_id="alice")
    assert any(e.content == "entry restrita a equipe" for e in da_alice)


@pytest.mark.asyncio
async def test_nao_membro_nao_le_entry_do_grupo(perfis: PostgresAdapter) -> None:
    await _criar_entries(perfis)
    do_agente = await perfis.search_by_tags(
        tags={}, requester_id="hermes-briefing"
    )
    assert not any(e.content == "entry restrita a equipe" for e in do_agente)


# --- Filtro de escrita ------------------------------------------------------


@pytest.mark.asyncio
async def test_editor_pode_atualizar(perfis: PostgresAdapter) -> None:
    entry = MemoryEntry(
        content="documento compartilhado",
        collection="content",
        author_id="tomas",
        tags={"editors": ["tomas", "alice"]},
    )
    entry_id = await perfis.store(entry)
    atualizada = entry.model_copy(update={"content": "revisado pela alice"})
    assert await perfis.update(entry_id, atualizada, requester_id="alice") is True


@pytest.mark.asyncio
async def test_nao_editor_recebe_permission_error(perfis: PostgresAdapter) -> None:
    entry = MemoryEntry(
        content="documento compartilhado",
        collection="content",
        author_id="tomas",
        tags={"editors": ["tomas", "alice"]},
    )
    entry_id = await perfis.store(entry)
    with pytest.raises(PermissionError):
        await perfis.update(entry_id, entry, requester_id="bob")


@pytest.mark.asyncio
async def test_autor_sempre_pode_escrever(perfis: PostgresAdapter) -> None:
    entry = MemoryEntry(
        content="meu documento", collection="content", author_id="bob"
    )
    entry_id = await perfis.store(entry)
    assert await perfis.update(entry_id, entry, requester_id="bob") is True


@pytest.mark.asyncio
async def test_coringa_em_editors_libera_todos(perfis: PostgresAdapter) -> None:
    entry = MemoryEntry(
        content="documento aberto",
        collection="content",
        author_id="tomas",
        tags={"editors": ["*"]},
    )
    entry_id = await perfis.store(entry)
    assert await perfis.update(entry_id, entry, requester_id="bob") is True


@pytest.mark.asyncio
async def test_grupo_em_editors(perfis: PostgresAdapter) -> None:
    entry = MemoryEntry(
        content="documento da equipe",
        collection="content",
        author_id="bob",
        tags={"editors": ["equipe"]},
    )
    entry_id = await perfis.store(entry)
    assert await perfis.update(entry_id, entry, requester_id="alice") is True
    with pytest.raises(PermissionError):
        await perfis.update(entry_id, entry, requester_id="hermes-briefing")


@pytest.mark.asyncio
async def test_delete_respeita_editors(perfis: PostgresAdapter) -> None:
    entry = MemoryEntry(
        content="documento", collection="content", author_id="tomas",
        tags={"editors": ["alice"]},
    )
    entry_id = await perfis.store(entry)
    with pytest.raises(PermissionError):
        await perfis.delete(entry_id, requester_id="bob")
    assert await perfis.delete(entry_id, requester_id="alice") is True


@pytest.mark.asyncio
async def test_d2_prevalece_sobre_editors(perfis: PostgresAdapter) -> None:
    """Entry protegida nao pode ser alterada por agente, mesmo em editors."""
    entry = MemoryEntry(
        content="regra protegida",
        collection="rule",
        mandatory=True,
        priority="critical",
        protected=True,
        source="human",
        author_id="tomas",
        tags={"editors": ["hermes-briefing"]},
    )
    entry_id = await perfis.store(entry)
    tentativa = entry.model_copy(update={"content": "relaxada", "source": "agent"})
    with pytest.raises(ProtectionError):
        await perfis.update(entry_id, tentativa, requester_id="hermes-briefing")
    with pytest.raises(ProtectionError):
        await perfis.delete(entry_id, source="agent", requester_id="hermes-briefing")


@pytest.mark.asyncio
async def test_escrita_sem_requester_preserva_comportamento(
    perfis: PostgresAdapter,
) -> None:
    entry = MemoryEntry(
        content="documento", collection="content", author_id="tomas",
        tags={"editors": ["alice"]},
    )
    entry_id = await perfis.store(entry)
    assert await perfis.update(entry_id, entry) is True
    assert await perfis.delete(entry_id) is True
