"""Contrato de perfis, parametrizado por backend (F3.7).

A ACL do OrkMind depende de resolver identidades. Ate F3.3 isso so
existia no `PostgresAdapter`, o que amarrava a ACL a um backend. Estes
testes provam que o `ProfileStore` entrega o mesmo comportamento em
`pgvector` (tabela `profiles`) e nos demais (entries da colecao `users`),
sem migrar dado de producao nenhum (DP-10, DD-6, divida D-3).
"""

from __future__ import annotations

import pytest

from orkmind.core.models import MemoryEntry, Profile
from orkmind.store.base import MemoryStore
from orkmind.store.profiles import MARCA_PERFIL, ProfileStore


def perfis_de(store: MemoryStore) -> ProfileStore:
    perfis = getattr(store, "profiles", None)
    if perfis is None:
        pytest.skip("store sem ProfileStore: a governanca nao subiu neste alvo")
    return perfis


class TestCrudDePerfis:
    @pytest.mark.asyncio
    async def test_cria_e_le(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(
            Profile(id="tomas", display_name="Tomas", profile_type="person")
        )
        lido = await perfis.get("tomas")
        assert lido is not None
        assert lido.display_name == "Tomas"
        assert lido.profile_type == "person"

    @pytest.mark.asyncio
    async def test_perfil_inexistente_devolve_none(self, store: MemoryStore) -> None:
        assert await perfis_de(store).get("nao-existe") is None

    @pytest.mark.asyncio
    async def test_atualiza(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(
            Profile(id="alice", display_name="Alice", profile_type="person")
        )
        assert await perfis.update(
            "alice",
            Profile(
                id="alice", display_name="Alice Silva", profile_type="person",
                permissions={"can_read_all_rules": True},
            ),
        ) is True
        lido = await perfis.get("alice")
        assert lido is not None
        assert lido.display_name == "Alice Silva"
        assert lido.permissions["can_read_all_rules"] is True

    @pytest.mark.asyncio
    async def test_create_e_idempotente(self, store: MemoryStore) -> None:
        """Espelha o ON CONFLICT DO UPDATE do SQL."""
        perfis = perfis_de(store)
        await perfis.create(Profile(id="bob", display_name="Bob", profile_type="person"))
        await perfis.create(Profile(id="bob", display_name="Bob II", profile_type="person"))
        lido = await perfis.get("bob")
        assert lido is not None
        assert lido.display_name == "Bob II"
        assert len(await perfis.list()) == 1

    @pytest.mark.asyncio
    async def test_remove(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(Profile(id="temp", display_name="Temp", profile_type="agent"))
        assert await perfis.delete("temp") is True
        assert await perfis.get("temp") is None
        assert await perfis.delete("temp") is False


class TestListagem:
    @pytest.mark.asyncio
    async def test_lista_todos(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(Profile(id="a", display_name="A", profile_type="person"))
        await perfis.create(Profile(id="b", display_name="B", profile_type="agent"))
        assert [p.id for p in await perfis.list()] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_lista_por_tipo(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(Profile(id="pessoa", display_name="P", profile_type="person"))
        await perfis.create(Profile(id="agente", display_name="A", profile_type="agent"))
        await perfis.create(Profile(id="grupo", display_name="G", profile_type="group"))
        assert [p.id for p in await perfis.list(profile_type="agent")] == ["agente"]
        assert [p.id for p in await perfis.list(profile_type="group")] == ["grupo"]

    @pytest.mark.asyncio
    async def test_lista_vazia_quando_nao_ha_perfil(self, store: MemoryStore) -> None:
        assert await perfis_de(store).list() == []


class TestGruposEIdentidades:
    @pytest.fixture(autouse=True)
    def _sem_estado(self) -> None:
        return None

    @pytest.mark.asyncio
    async def test_membros_de_um_grupo(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(Profile(
            id="equipe", display_name="Equipe", profile_type="group",
            metadata={"members": ["alice", "bob"]},
        ))
        assert await perfis.get_members("equipe") == ["alice", "bob"]

    @pytest.mark.asyncio
    async def test_membros_de_quem_nao_e_grupo(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(Profile(id="alice", display_name="A", profile_type="person"))
        assert await perfis.get_members("alice") == []

    @pytest.mark.asyncio
    async def test_resolve_identities_inclui_os_grupos(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(Profile(
            id="equipe", display_name="Equipe", profile_type="group",
            metadata={"members": ["bob"]},
        ))
        await perfis.create(Profile(
            id="outra", display_name="Outra", profile_type="group",
            metadata={"members": ["carol"]},
        ))
        identidades = await perfis.resolve_identities("bob")
        assert set(identidades) == {"bob", "equipe"}

    @pytest.mark.asyncio
    async def test_resolve_identities_sem_grupo(self, store: MemoryStore) -> None:
        assert await perfis_de(store).resolve_identities("sozinho") == ["sozinho"]

    @pytest.mark.asyncio
    async def test_grupo_aninhado_nao_e_resolvido(self, store: MemoryStore) -> None:
        """Um nivel, sem recursao: mesmo comportamento de hoje."""
        perfis = perfis_de(store)
        await perfis.create(Profile(
            id="interno", display_name="Interno", profile_type="group",
            metadata={"members": ["bob"]},
        ))
        await perfis.create(Profile(
            id="externo", display_name="Externo", profile_type="group",
            metadata={"members": ["interno"]},
        ))
        assert set(await perfis.resolve_identities("bob")) == {"bob", "interno"}


class TestAclPorGrupoEmQualquerBackend:
    """O ponto da tarefa: a ACL por grupo vale nos tres backends."""

    @pytest.mark.asyncio
    async def test_membro_do_grupo_le_entry_restrita(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(Profile(
            id="equipe", display_name="Equipe", profile_type="group",
            metadata={"members": ["bob"]},
        ))
        await store.store(MemoryEntry(
            content="restrita ao grupo", collection="fact",
            tags={"skill": ["acl"], "audience": ["equipe"]},
            visibility="restricted", author_id="alice",
        ))
        do_bob = await store.search_by_tags(
            tags={"skill": ["acl"]}, requester_id="bob"
        )
        assert [e.content for e in do_bob] == ["restrita ao grupo"]

    @pytest.mark.asyncio
    async def test_quem_esta_fora_do_grupo_nao_le(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(Profile(
            id="equipe", display_name="Equipe", profile_type="group",
            metadata={"members": ["bob"]},
        ))
        await store.store(MemoryEntry(
            content="restrita ao grupo", collection="fact",
            tags={"skill": ["acl"], "audience": ["equipe"]},
            visibility="restricted", author_id="alice",
        ))
        da_carol = await store.search_by_tags(
            tags={"skill": ["acl"]}, requester_id="carol"
        )
        assert da_carol == []

    @pytest.mark.asyncio
    async def test_escrita_por_grupo_em_editors(self, store: MemoryStore) -> None:
        perfis = perfis_de(store)
        await perfis.create(Profile(
            id="editores", display_name="Editores", profile_type="group",
            metadata={"members": ["bob"]},
        ))
        entry = MemoryEntry(
            content="editavel pelo grupo", collection="fact",
            tags={"editors": ["editores"]}, author_id="alice",
        )
        entry_id = await store.store(entry)
        atualizada = MemoryEntry(
            content="editada pelo bob", collection="fact",
            tags={"editors": ["editores"]}, author_id="alice",
        )
        assert await store.update(entry_id, atualizada, requester_id="bob") is True

    @pytest.mark.asyncio
    async def test_escrita_por_estranho_e_barrada(self, store: MemoryStore) -> None:
        perfis_de(store)
        entry = MemoryEntry(
            content="so da alice", collection="fact", author_id="alice"
        )
        entry_id = await store.store(entry)
        with pytest.raises(PermissionError):
            await store.update(
                entry_id,
                MemoryEntry(content="tentativa", collection="fact"),
                requester_id="carol",
            )


class TestMapeamentoDeclarado:
    """DP-A: perfis viram entries em backends genericos, e isso e visivel."""

    @pytest.mark.asyncio
    async def test_perfil_marcado_quando_persistido_como_entry(
        self, store: MemoryStore
    ) -> None:
        from orkmind.store.profiles import EntryProfileStore

        perfis = perfis_de(store)
        await perfis.create(
            Profile(id="tomas", display_name="Tomas", profile_type="person")
        )
        if not isinstance(perfis, EntryProfileStore):
            pytest.skip(
                f"backend {store.capabilities.backend} usa PostgresProfileStore: "
                f"perfis ficam na tabela 'profiles', nao viram entries (DD-6)"
            )
        entry = await store.retrieve("tomas")
        assert entry is not None
        assert entry.collection == "users"
        assert entry.metadata[MARCA_PERFIL] is True
        assert entry.metadata["profile_type"] == "person"

    @pytest.mark.asyncio
    async def test_entry_comum_de_users_nao_e_lida_como_perfil(
        self, store: MemoryStore
    ) -> None:
        """A marca protege contra confundir memoria sobre gente com identidade."""
        perfis = perfis_de(store)
        await store.store(MemoryEntry(
            content="tomas prefere commits atomicos", collection="users"
        ))
        assert await perfis.list() == []
