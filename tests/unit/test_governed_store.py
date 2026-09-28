"""GovernedStore sobre um MemoryStore falso (F3.4).

Prova o que a camada faz ANTES e DEPOIS de delegar: com que limite ela
chama o backend, o que filtra, como ordena e quando avisa. Sem servico
externo.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock

import pytest

from orkmind.core.models import MemoryEntry, Profile
from orkmind.core.ontology import ProtectionError
from orkmind.store.base import MemoryStore
from orkmind.store.capabilities import StoreCapabilities
from orkmind.store.governed import GovernedStore, montar_profile_store
from orkmind.store.memory_adapter import MemoryAdapter
from orkmind.store.postgres_adapter import PGVECTOR_CAPABILITIES
from orkmind.store.profiles import EntryProfileStore, PostgresProfileStore

CAP_PGVECTOR = PGVECTOR_CAPABILITIES
CAP_SIMPLES = StoreCapabilities(
    backend="falso",
    native_acl_filter=False,
    native_constitutional_order=False,
    unique_content_hash=False,
)


def entrada(**kwargs) -> MemoryEntry:
    base = {"content": "conteudo", "collection": "fact"}
    base.update(kwargs)
    return MemoryEntry(**base)  # type: ignore[arg-type]


class PerfisFalsos:
    """ProfileStore minimo, sem I/O."""

    def __init__(self, grupos: Optional[dict[str, list[str]]] = None) -> None:
        self._grupos = grupos or {}

    async def get(self, profile_id: str) -> Optional[Profile]:
        return None

    async def create(self, profile: Profile) -> str:
        return profile.id

    async def update(self, profile_id: str, profile: Profile) -> bool:
        return True

    async def delete(self, profile_id: str) -> bool:
        return True

    async def list(self, profile_type: Optional[str] = None) -> list[Profile]:
        return []

    async def get_members(self, group_id: str) -> list[str]:
        return self._grupos.get(group_id, [])

    async def resolve_identities(self, requester_id: str) -> list[str]:
        grupos = [g for g, membros in self._grupos.items() if requester_id in membros]
        return [requester_id, *grupos]


def montar(
    capabilities: StoreCapabilities = CAP_SIMPLES,
    grupos: Optional[dict[str, list[str]]] = None,
    overfetch: int = 500,
) -> tuple[GovernedStore, AsyncMock]:
    inner = AsyncMock(spec=MemoryStore)
    type(inner).capabilities = property(lambda self: capabilities)
    inner.search_by_tags.return_value = []
    inner.search_by_text.return_value = []
    inner.search_semantic.return_value = []
    inner.search_semantic_text.return_value = []
    inner.retrieve.return_value = None
    governado = GovernedStore(
        inner, profiles=PerfisFalsos(grupos), candidate_overfetch=overfetch
    )
    return governado, inner


class TestCapabilitiesRepassadas:
    @pytest.mark.asyncio
    async def test_repassa_as_do_backend(self) -> None:
        governado, _ = montar(CAP_PGVECTOR)
        assert governado.capabilities is CAP_PGVECTOR

    @pytest.mark.asyncio
    async def test_expoe_o_adapter_em_inner(self) -> None:
        governado, inner = montar()
        assert governado.inner is inner


class TestSemOverfetchNoPgvector:
    """8.3, criterio 4: o limit enviado e o limit pedido."""

    @pytest.mark.asyncio
    async def test_search_by_tags_usa_o_limit_pedido(self) -> None:
        governado, inner = montar(CAP_PGVECTOR)
        await governado.search_by_tags({"skill": ["x"]}, limit=50, requester_id="bob")
        assert inner.search_by_tags.await_args.args[3] == 50

    @pytest.mark.asyncio
    async def test_search_by_text_usa_o_limit_pedido(self) -> None:
        governado, inner = montar(CAP_PGVECTOR)
        await governado.search_by_text("q", limit=10, requester_id="bob")
        assert inner.search_by_text.await_args.args[2] == 10

    @pytest.mark.asyncio
    async def test_search_semantic_usa_o_limit_pedido(self) -> None:
        governado, inner = montar(CAP_PGVECTOR)
        await governado.search_semantic([0.1], limit=10, requester_id="bob")
        assert inner.search_semantic.await_args.args[2] == 10


class TestOverfetchOndeFalta:
    @pytest.mark.asyncio
    async def test_backend_sem_ordenacao_recebe_janela_maior(self) -> None:
        governado, inner = montar(CAP_SIMPLES)
        await governado.search_by_tags({"skill": ["x"]}, limit=10)
        assert inner.search_by_tags.await_args.args[3] == 60

    @pytest.mark.asyncio
    async def test_teto_configuravel_e_respeitado(self) -> None:
        governado, inner = montar(CAP_SIMPLES, overfetch=80)
        await governado.search_by_tags({"skill": ["x"]}, limit=100)
        assert inner.search_by_tags.await_args.args[3] == 100

    @pytest.mark.asyncio
    async def test_avisa_saturacao_da_janela(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """I6: se a janela encheu, a garantia de mandatory pode ter sido tocada."""
        governado, inner = montar(CAP_SIMPLES)
        inner.search_by_tags.return_value = [entrada() for _ in range(60)]
        with caplog.at_level(logging.WARNING):
            await governado.search_by_tags({"skill": ["x"]}, limit=10)
        assert any("saturou a janela" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_nao_avisa_quando_a_janela_sobra(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        governado, inner = montar(CAP_SIMPLES)
        inner.search_by_tags.return_value = [entrada() for _ in range(5)]
        with caplog.at_level(logging.WARNING):
            await governado.search_by_tags({"skill": ["x"]}, limit=10)
        assert not [r for r in caplog.records if "saturou" in r.getMessage()]


class TestFiltragemEOrdenacao:
    @pytest.mark.asyncio
    async def test_filtra_expirada_injection_e_invisivel_e_ordena(self) -> None:
        agora = datetime.now(timezone.utc)
        brutas = [
            entrada(id="comum", priority="low", visibility="public"),
            entrada(id="expirada", visibility="public",
                    expires_at=agora - timedelta(hours=1)),
            entrada(id="suja", visibility="public", injection_risk=True),
            entrada(id="privada", visibility="private", author_id="alice"),
            entrada(id="obrigatoria", mandatory=True, visibility="public"),
        ]
        governado, inner = montar(CAP_SIMPLES)
        inner.search_by_tags.return_value = brutas
        achadas = await governado.search_by_tags({"skill": ["x"]}, requester_id="bob")
        assert [e.id for e in achadas] == ["obrigatoria", "comum"]

    @pytest.mark.asyncio
    async def test_trunca_no_limit_depois_de_ordenar(self) -> None:
        brutas = [entrada(id=f"c{i}", priority="low") for i in range(5)]
        brutas.append(entrada(id="obrigatoria", mandatory=True))
        governado, inner = montar(CAP_SIMPLES)
        inner.search_by_tags.return_value = brutas
        achadas = await governado.search_by_tags({"skill": ["x"]}, limit=2)
        assert len(achadas) == 2
        assert achadas[0].id == "obrigatoria"

    @pytest.mark.asyncio
    async def test_mandatory_only_e_reforcado_pela_camada(self) -> None:
        """Backend que ignora o filtro nao vaza entry comum."""
        governado, inner = montar(CAP_SIMPLES)
        inner.search_by_tags.return_value = [
            entrada(id="comum"), entrada(id="obrigatoria", mandatory=True)
        ]
        achadas = await governado.search_by_tags({"skill": ["x"]}, mandatory_only=True)
        assert [e.id for e in achadas] == ["obrigatoria"]

    @pytest.mark.asyncio
    async def test_busca_por_relevancia_preserva_a_ordem_do_backend(self) -> None:
        governado, inner = montar(CAP_SIMPLES)
        inner.search_by_text.return_value = [
            entrada(id="r1", priority="low"),
            entrada(id="m1", mandatory=True, priority="low"),
            entrada(id="r2", priority="critical"),
        ]
        achadas = await governado.search_by_text("q")
        assert [e.id for e in achadas] == ["m1", "r1", "r2"]

    @pytest.mark.asyncio
    async def test_busca_por_relevancia_nao_filtra_injection(self) -> None:
        """Paridade com o SQL: o filtro de injection e de search_by_tags."""
        governado, inner = montar(CAP_SIMPLES)
        inner.search_by_text.return_value = [entrada(id="suja", injection_risk=True)]
        achadas = await governado.search_by_text("q")
        assert [e.id for e in achadas] == ["suja"]

    @pytest.mark.asyncio
    async def test_acl_por_grupo_na_busca(self) -> None:
        governado, inner = montar(CAP_SIMPLES, grupos={"equipe": ["bob"]})
        inner.search_by_tags.return_value = [
            entrada(id="do-grupo", visibility="restricted", author_id="alice",
                    tags={"audience": ["equipe"]}),
            entrada(id="de-ninguem", visibility="private", author_id="alice"),
        ]
        achadas = await governado.search_by_tags({"skill": ["x"]}, requester_id="bob")
        assert [e.id for e in achadas] == ["do-grupo"]


class TestProtecaoEAcl:
    @pytest.mark.asyncio
    async def test_update_de_protegida_por_agente_e_barrado(self) -> None:
        governado, inner = montar()
        inner.retrieve.return_value = entrada(id="alvo", protected=True)
        with pytest.raises(ProtectionError):
            await governado.update("alvo", entrada(source="agent"))
        inner.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_de_critica_por_agente_e_barrado(self) -> None:
        governado, inner = montar()
        inner.retrieve.return_value = entrada(id="alvo", priority="critical")
        with pytest.raises(ProtectionError):
            await governado.delete("alvo", source="agent")
        inner.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_i2_protecao_avaliada_antes_da_acl(self) -> None:
        """Estar em editors nao autoriza mexer em entry protegida."""
        governado, inner = montar()
        inner.retrieve.return_value = entrada(
            id="alvo", protected=True, author_id="bob", tags={"editors": ["*"]}
        )
        with pytest.raises(ProtectionError):
            await governado.update("alvo", entrada(source="agent"), requester_id="bob")

    @pytest.mark.asyncio
    async def test_escrita_sem_permissao_e_barrada(self) -> None:
        governado, inner = montar()
        inner.retrieve.return_value = entrada(id="alvo", author_id="alice")
        with pytest.raises(PermissionError):
            await governado.update("alvo", entrada(), requester_id="bob")
        inner.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entry_inexistente_delega_sem_politica(self) -> None:
        governado, inner = montar()
        inner.retrieve.return_value = None
        inner.update.return_value = False
        assert await governado.update("nao-existe", entrada()) is False
        inner.update.assert_awaited_once()


class TestIdempotenciaBestEffort:
    """DP-6: sem indice unico, consultar por hash antes de gravar."""

    @pytest.mark.asyncio
    async def test_reaproveita_id_existente_quando_nao_ha_indice(self) -> None:
        governado, inner = montar(CAP_SIMPLES)
        inner.find_by_content_hash.return_value = entrada(id="ja-existe")
        novo = entrada(content_hash="abc")
        assert await governado.store(novo) == "ja-existe"
        inner.store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_grava_quando_nao_ha_colisao(self) -> None:
        governado, inner = montar(CAP_SIMPLES)
        inner.find_by_content_hash.return_value = None
        inner.store.return_value = "novo"
        assert await governado.store(entrada(content_hash="abc")) == "novo"

    @pytest.mark.asyncio
    async def test_backend_com_indice_nao_faz_consulta_extra(self) -> None:
        governado, inner = montar(CAP_PGVECTOR)
        inner.store.return_value = "novo"
        await governado.store(entrada(content_hash="abc"))
        inner.find_by_content_hash.assert_not_awaited()


class TestVersionamento:
    @pytest.mark.asyncio
    async def test_avisa_quando_o_backend_nao_incrementa(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """I7 verificado, nao presumido."""
        governado, inner = montar(CAP_SIMPLES)
        inner.retrieve.side_effect = [entrada(id="a", version=1), entrada(id="a", version=1)]
        inner.update.return_value = True
        with caplog.at_level(logging.WARNING):
            await governado.update("a", entrada())
        assert any("I7" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_nao_avisa_quando_o_backend_cumpre(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        governado, inner = montar(CAP_SIMPLES)
        inner.retrieve.side_effect = [entrada(id="a", version=1), entrada(id="a", version=2)]
        inner.update.return_value = True
        with caplog.at_level(logging.WARNING):
            await governado.update("a", entrada())
        assert not [r for r in caplog.records if "I7" in r.getMessage()]


class TestDelegacaoPura:
    """O que a camada nao governa, ela repassa sem tocar."""

    @pytest.mark.asyncio
    async def test_backfill_nao_passa_por_governanca(self) -> None:
        governado, inner = montar()
        inner.set_embedding.return_value = True
        assert await governado.set_embedding("id", [0.1]) is True
        inner.set_embedding.assert_awaited_once_with("id", [0.1])

    @pytest.mark.asyncio
    async def test_snapshots_e_contagens_sao_repassados(self) -> None:
        governado, inner = montar()
        inner.count.return_value = 7
        inner.snapshot_commit.return_value = "snap"
        assert await governado.count("rule") == 7
        assert await governado.snapshot_commit("v1", "msg") == "snap"
        inner.snapshot_commit.assert_awaited_once_with("v1", "msg")

    @pytest.mark.asyncio
    async def test_atributo_desconhecido_nao_vaza_para_o_backend(self) -> None:
        governado, _ = montar()
        with pytest.raises(AttributeError, match="store.inner"):
            governado._get_conn  # noqa: B018


class TestMontarProfileStore:
    def test_pgvector_usa_a_tabela_profiles(self) -> None:
        from orkmind.store.postgres_adapter import PostgresAdapter

        adapter = PostgresAdapter("postgresql://localhost/nunca-conecta")
        assert isinstance(montar_profile_store("pgvector", adapter), PostgresProfileStore)

    def test_alias_postgres_tambem(self) -> None:
        from orkmind.store.postgres_adapter import PostgresAdapter

        adapter = PostgresAdapter("postgresql://localhost/nunca-conecta")
        assert isinstance(montar_profile_store("postgres", adapter), PostgresProfileStore)

    def test_memory_usa_entries(self) -> None:
        assert isinstance(
            montar_profile_store("memory", MemoryAdapter()), EntryProfileStore
        )

    def test_qdrant_usa_entries(self) -> None:
        assert isinstance(
            montar_profile_store("qdrant", MemoryAdapter()), EntryProfileStore
        )


class TestGovernedStorePorCimaDoMemoryAdapter:
    """Integracao leve: a camada real sobre o adapter real, sem servico."""

    @pytest.fixture
    def governado(self) -> GovernedStore:
        adapter = MemoryAdapter()
        return GovernedStore(adapter, profiles=EntryProfileStore(adapter))

    @pytest.mark.asyncio
    async def test_ciclo_completo_com_governanca(
        self, governado: GovernedStore
    ) -> None:
        protegida = entrada(content="regra", collection="rule", protected=True)
        await governado.store(protegida)
        with pytest.raises(ProtectionError):
            await governado.delete(protegida.id, source="agent")
        assert await governado.count() == 1

    @pytest.mark.asyncio
    async def test_acl_por_grupo_ponta_a_ponta(
        self, governado: GovernedStore
    ) -> None:
        await governado.profile_create(
            Profile(
                id="equipe", display_name="Equipe", profile_type="group",
                metadata={"members": ["bob"]},
            )
        )
        await governado.store(entrada(
            content="restrita", collection="fact", tags={"audience": ["equipe"]},
            visibility="restricted", author_id="alice",
        ))
        do_bob = await governado.search_by_text("restrita", requester_id="bob")
        assert [e.content for e in do_bob] == ["restrita"]
        da_carol = await governado.search_by_text("restrita", requester_id="carol")
        assert da_carol == []

    @pytest.mark.asyncio
    async def test_perfil_e_marcado_como_perfil(
        self, governado: GovernedStore
    ) -> None:
        """DP-A: a marca existe para que um filtro futuro seja possivel."""
        await governado.profile_create(
            Profile(id="tomas", display_name="Tomas", profile_type="person")
        )
        entry = await governado.retrieve("tomas")
        assert entry is not None
        assert entry.collection == "users"
        assert entry.metadata["orkmind_profile"] is True
