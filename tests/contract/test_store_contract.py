"""Contract tests for MemoryStore implementations.

Suite parametrizada por backend (F3.3). O mesmo contrato roda em
`memory` (sempre, sem servico externo), em `pgvector` (com
ORKMIND_TEST_DATABASE_URL) e em `qdrant` (com ORKMIND_TEST_QDRANT_URL e
ORKMIND_TEST_QDRANT_PREFIX). A fixture `store` vem de
`tests/contract/conftest.py`; os alvos e a limpeza vem de
`tests/contract/backends.py`, sempre atras da guarda destrutiva.

Rode com `-rs` para ver o motivo de cada skip: skip sem razao e proibido.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orkmind.core.models import MemoryEntry
from orkmind.core.ontology import ProtectionError
from orkmind.store.base import MemoryStore
from tests.contract.backends import exige_capability


class TestStoreContract:
    @pytest.mark.asyncio
    async def test_store_and_retrieve(self, store: MemoryStore) -> None:
        entry = MemoryEntry(content="test entry", collection="fact")
        entry_id = await store.store(entry)
        retrieved = await store.retrieve(entry_id)
        assert retrieved is not None
        assert retrieved.content == "test entry"
        assert retrieved.collection == "fact"

    @pytest.mark.asyncio
    async def test_update(self, store: MemoryStore) -> None:
        entry = MemoryEntry(content="original", collection="fact")
        entry_id = await store.store(entry)
        entry.content = "updated"
        success = await store.update(entry_id, entry)
        assert success is True
        retrieved = await store.retrieve(entry_id)
        assert retrieved is not None
        assert retrieved.content == "updated"
        assert retrieved.version == 2

    @pytest.mark.asyncio
    async def test_delete(self, store: MemoryStore) -> None:
        entry = MemoryEntry(content="to delete", collection="fact")
        entry_id = await store.store(entry)
        success = await store.delete(entry_id)
        assert success is True
        assert await store.retrieve(entry_id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, store: MemoryStore) -> None:
        success = await store.delete("nonexistent-id")
        assert success is False

    @pytest.mark.asyncio
    async def test_search_by_tags_exact(self, store: MemoryStore) -> None:
        e1 = MemoryEntry(
            content="git rule",
            collection="rule",
            tags={"skill": ["git"]},
        )
        e2 = MemoryEntry(
            content="deploy rule",
            collection="rule",
            tags={"skill": ["deploy"]},
        )
        await store.store(e1)
        await store.store(e2)

        results = await store.search_by_tags(tags={"skill": ["git"]})
        assert len(results) == 1
        assert results[0].content == "git rule"

    @pytest.mark.asyncio
    async def test_mandatory_never_omitted(self, store: MemoryStore) -> None:
        mandatory = MemoryEntry(
            content="MUST be returned",
            collection="rule",
            tags={"skill": ["deploy"]},
            mandatory=True,
            priority="critical",
        )
        optional = MemoryEntry(
            content="optional",
            collection="fact",
            tags={"skill": ["deploy"]},
            mandatory=False,
        )
        await store.store(mandatory)
        await store.store(optional)

        results = await store.search_by_tags(
            tags={"skill": ["deploy"]}, mandatory_only=True
        )
        assert len(results) == 1
        assert results[0].mandatory is True

    @pytest.mark.asyncio
    async def test_search_by_tags_deterministic(self, store: MemoryStore) -> None:
        """Same query must return same results every time."""
        for i in range(5):
            await store.store(MemoryEntry(
                content=f"rule {i}",
                collection="rule",
                tags={"skill": ["git"]},
            ))

        results1 = await store.search_by_tags(tags={"skill": ["git"]})
        results2 = await store.search_by_tags(tags={"skill": ["git"]})
        ids1 = [e.id for e in results1]
        ids2 = [e.id for e in results2]
        assert ids1 == ids2

    @pytest.mark.asyncio
    async def test_collections_isolated(self, store: MemoryStore) -> None:
        await store.store(MemoryEntry(content="a rule", collection="rule", tags={"skill": ["x"]}))
        await store.store(MemoryEntry(content="a fact", collection="fact", tags={"skill": ["x"]}))

        rules = await store.search_by_tags(tags={"skill": ["x"]}, collection="rule")
        facts = await store.search_by_tags(tags={"skill": ["x"]}, collection="fact")
        assert len(rules) == 1
        assert rules[0].collection == "rule"
        assert len(facts) == 1
        assert facts[0].collection == "fact"

    @pytest.mark.asyncio
    async def test_count(self, store: MemoryStore) -> None:
        await store.store(MemoryEntry(content="a", collection="rule"))
        await store.store(MemoryEntry(content="b", collection="fact"))
        total = await store.count()
        assert total == 2
        rule_count = await store.count("rule")
        assert rule_count == 1

    @pytest.mark.asyncio
    async def test_list_collections(self, store: MemoryStore) -> None:
        await store.store(MemoryEntry(content="a", collection="rule"))
        await store.store(MemoryEntry(content="b", collection="fact"))
        collections = await store.list_collections()
        assert "rule" in collections
        assert "fact" in collections

    @pytest.mark.asyncio
    async def test_garbage_collect(self, store: MemoryStore) -> None:
        expired = MemoryEntry(
            content="old",
            collection="fact",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        valid = MemoryEntry(content="current", collection="fact")
        await store.store(expired)
        await store.store(valid)

        removed = await store.garbage_collect()
        assert removed == 1
        total = await store.count()
        assert total == 1

    @pytest.mark.asyncio
    async def test_all_16_collections(
        self, store: MemoryStore, all_16_collections: list[MemoryEntry],
    ) -> None:
        for entry in all_16_collections:
            await store.store(entry)
        total = await store.count()
        assert total == 16
        collections = await store.list_collections()
        assert len(collections) == 16

    @pytest.mark.asyncio
    async def test_store_protected_entry(self, store: MemoryStore) -> None:
        entry = MemoryEntry(
            content="regra protegida", collection="rule",
            protected=True, source="human",
        )
        entry_id = await store.store(entry)
        retrieved = await store.retrieve(entry_id)
        assert retrieved is not None
        assert retrieved.protected is True

    @pytest.mark.governanca
    @pytest.mark.asyncio
    async def test_update_protected_agent_rejected(self, store: MemoryStore) -> None:
        entry = MemoryEntry(
            content="regra critica", collection="rule",
            protected=True, source="human",
        )
        entry_id = await store.store(entry)
        updated = MemoryEntry(
            content="tentativa de edicao", collection="rule",
            source="agent",
        )
        with pytest.raises(ProtectionError, match="protegida"):
            await store.update(entry_id, updated)

    @pytest.mark.asyncio
    async def test_update_protected_human_ok(self, store: MemoryStore) -> None:
        entry = MemoryEntry(
            content="regra critica", collection="rule",
            protected=True, source="human",
        )
        entry_id = await store.store(entry)
        updated = MemoryEntry(
            content="edicao humana ok", collection="rule",
            source="human",
        )
        success = await store.update(entry_id, updated)
        assert success is True
        retrieved = await store.retrieve(entry_id)
        assert retrieved is not None
        assert retrieved.content == "edicao humana ok"

    @pytest.mark.governanca
    @pytest.mark.asyncio
    async def test_delete_protected_agent_rejected(self, store: MemoryStore) -> None:
        entry = MemoryEntry(
            content="regra critica", collection="rule",
            priority="critical", source="human",
        )
        entry_id = await store.store(entry)
        with pytest.raises(ProtectionError, match="protegida"):
            await store.delete(entry_id, source="agent")

    @pytest.mark.asyncio
    async def test_update_creates_version(self, store: MemoryStore) -> None:
        entry = MemoryEntry(content="versao 1", collection="fact")
        entry_id = await store.store(entry)
        updated = MemoryEntry(content="versao 2", collection="fact", source="human")
        await store.update(entry_id, updated)
        history = await store.get_history(entry_id)
        assert len(history) == 1
        assert history[0].content == "versao 1"
        assert history[0].version == 1

    @pytest.mark.asyncio
    async def test_get_history(self, store: MemoryStore) -> None:
        entry = MemoryEntry(content="v1", collection="fact")
        entry_id = await store.store(entry)
        # Fazer 2 updates para criar 2 versoes
        await store.update(entry_id, MemoryEntry(content="v2", collection="fact"))
        await store.update(entry_id, MemoryEntry(content="v3", collection="fact"))
        history = await store.get_history(entry_id)
        assert len(history) == 2
        # Ordem DESC por version
        assert history[0].version > history[1].version
        contents = {h.content for h in history}
        assert "v1" in contents
        assert "v2" in contents

    @pytest.mark.asyncio
    async def test_gc_versions(self, store: MemoryStore) -> None:
        entry = MemoryEntry(content="original", collection="fact")
        entry_id = await store.store(entry)
        await store.update(entry_id, MemoryEntry(content="atualizado", collection="fact"))
        # GC com 0 dias deve remover a versao recem-criada
        removed = await store.gc_versions(max_age_days=0)
        assert removed >= 1
        history = await store.get_history(entry_id)
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_store_retrieve_with_layers(self, store: MemoryStore) -> None:
        """Store entry com essence/structure, retrieve intacto."""
        entry = MemoryEntry(
            content="conteudo completo", collection="fact",
            essence="essencia", structure="estrutura",
        )
        entry_id = await store.store(entry)
        retrieved = await store.retrieve(entry_id)
        assert retrieved is not None
        assert retrieved.essence == "essencia"
        assert retrieved.structure == "estrutura"

    @pytest.mark.asyncio
    async def test_snapshot_commit_and_log(self, store: MemoryStore) -> None:
        """Contrato basico de commit/log."""
        await store.store(MemoryEntry(content="entry1", collection="fact"))
        await store.store(MemoryEntry(content="entry2", collection="rule"))
        snap_id = await store.snapshot_commit("v1", "primeiro")
        assert snap_id
        log = await store.snapshot_log()
        assert len(log) >= 1
        assert log[0]["label"] == "v1"
        assert log[0]["entry_count"] == 2

    @pytest.mark.asyncio
    async def test_snapshot_show_entries(self, store: MemoryStore) -> None:
        """Contrato de show."""
        await store.store(MemoryEntry(content="entry1", collection="fact"))
        snap_id = await store.snapshot_commit("v1")
        data = await store.snapshot_show(snap_id)
        assert data["entry_count"] == 1
        assert len(data["entries"]) == 1

    @pytest.mark.asyncio
    async def test_snapshot_diff_contract(self, store: MemoryStore) -> None:
        """Contrato de diff."""
        e1 = MemoryEntry(content="entry1", collection="fact")
        await store.store(e1)
        snap_a = await store.snapshot_commit("v1")
        e2 = MemoryEntry(content="entry2", collection="rule")
        await store.store(e2)
        snap_b = await store.snapshot_commit("v2")
        diff = await store.snapshot_diff(snap_a, snap_b)
        assert e2.id in diff["added"]
        assert diff["removed"] == []

    @pytest.mark.asyncio
    async def test_snapshot_restore_contract(self, store: MemoryStore) -> None:
        """Contrato de restore."""
        e1 = MemoryEntry(content="entry original", collection="fact")
        await store.store(e1)
        snap_id = await store.snapshot_commit("v1")
        # Adicionar nova entry
        await store.store(MemoryEntry(content="nova", collection="rule"))
        assert await store.count() == 2
        # Restaurar
        count = await store.snapshot_restore(snap_id)
        assert count == 1
        total = await store.count()
        assert total == 1
        retrieved = await store.retrieve(e1.id)
        assert retrieved is not None
        assert retrieved.content == "entry original"

    @pytest.mark.asyncio
    async def test_snapshot_restore_auto_backup_contract(self, store: MemoryStore) -> None:
        """Contrato de auto-backup: restaurar cria snapshot de backup."""
        await store.store(MemoryEntry(content="entry1", collection="fact"))
        snap_id = await store.snapshot_commit("v1")
        await store.snapshot_restore(snap_id)
        log = await store.snapshot_log()
        labels = [s["label"] for s in log]
        assert "auto-backup-pre-restore" in labels

    @pytest.mark.asyncio
    async def test_content_hash_stored(self, store: MemoryStore) -> None:
        from orkmind.core.injection import compute_content_hash
        content = "conteudo para hash"
        entry = MemoryEntry(
            content=content, collection="fact",
            content_hash=compute_content_hash(content),
        )
        entry_id = await store.store(entry)
        retrieved = await store.retrieve(entry_id)
        assert retrieved is not None
        assert retrieved.content_hash == compute_content_hash(content)


class TestCapabilitiesContract:
    """O backend declara o que sabe fazer, e a declaracao e verificada."""

    @pytest.mark.asyncio
    async def test_capabilities_batem_com_a_tabela_do_plano(
        self, store: MemoryStore, request: pytest.FixtureRequest
    ) -> None:
        from tests.contract.backends import obter_backend

        esperadas = obter_backend(request.node.callspec.params["store"]).capabilities_esperadas
        assert store.capabilities.as_dict() == esperadas.as_dict()

    @pytest.mark.asyncio
    async def test_backend_nomeado_nas_capabilities(self, store: MemoryStore) -> None:
        assert store.capabilities.backend != "desconhecido"


class TestBuscaContract:
    @pytest.mark.asyncio
    async def test_busca_textual_encontra_o_conteudo(self, store: MemoryStore) -> None:
        motivo = exige_capability(store, "text_search", "none")
        if motivo is None:
            pytest.skip("backend nao faz busca textual (text_search='none')")
        await store.store(MemoryEntry(content="deployment pipeline", collection="fact"))
        await store.store(MemoryEntry(content="assunto totalmente outro", collection="fact"))
        achadas = await store.search_by_text("deployment")
        assert [e.content for e in achadas] == ["deployment pipeline"]

    @pytest.mark.asyncio
    async def test_busca_semantica_ordena_por_proximidade(self, store: MemoryStore) -> None:
        motivo = exige_capability(store, "vector_search", True)
        if motivo:
            pytest.skip(motivo)
        dim = 1024
        perto = [1.0] + [0.0] * (dim - 1)
        longe = [0.0, 1.0] + [0.0] * (dim - 2)
        await store.store(
            MemoryEntry(content="vetor perto", collection="fact", embedding=perto)
        )
        await store.store(
            MemoryEntry(content="vetor longe", collection="fact", embedding=longe)
        )
        achadas = await store.search_semantic(perto, limit=2)
        assert achadas[0].content == "vetor perto"

    @pytest.mark.asyncio
    async def test_entry_expirada_nao_aparece_em_busca(self, store: MemoryStore) -> None:
        """I4: expirada nunca aparece, em nenhum backend."""
        await store.store(MemoryEntry(
            content="expirada",
            collection="fact",
            tags={"skill": ["ttl"]},
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        ))
        await store.store(MemoryEntry(
            content="viva", collection="fact", tags={"skill": ["ttl"]},
        ))
        achadas = await store.search_by_tags(tags={"skill": ["ttl"]})
        assert [e.content for e in achadas] == ["viva"]


class TestIdempotenciaContract:
    @pytest.mark.asyncio
    async def test_duplicata_barrada_quando_ha_indice_unico(
        self, store: MemoryStore
    ) -> None:
        motivo = exige_capability(store, "unique_content_hash", True)
        if motivo:
            pytest.skip(motivo)
        from orkmind.core.injection import compute_content_hash

        conteudo = "conteudo que nao pode duplicar"
        entrada = MemoryEntry(
            content=conteudo, collection="fact",
            content_hash=compute_content_hash(conteudo),
        )
        await store.store(entrada)
        with pytest.raises(Exception):
            await store.store(MemoryEntry(
                content=conteudo, collection="fact",
                content_hash=compute_content_hash(conteudo),
            ))
        assert await store.count("fact") == 1

    @pytest.mark.asyncio
    async def test_find_by_content_hash_acha_a_entry(self, store: MemoryStore) -> None:
        from orkmind.core.injection import compute_content_hash

        conteudo = "achavel por hash"
        entrada = MemoryEntry(
            content=conteudo, collection="fact",
            content_hash=compute_content_hash(conteudo),
        )
        entry_id = await store.store(entrada)
        achada = await store.find_by_content_hash(compute_content_hash(conteudo))
        assert achada is not None
        assert achada.id == entry_id

    @pytest.mark.asyncio
    async def test_hash_vazio_devolve_none(self, store: MemoryStore) -> None:
        assert await store.find_by_content_hash("") is None


class TestHierarquiaContract:
    @pytest.mark.asyncio
    async def test_get_children_devolve_as_filhas(self, store: MemoryStore) -> None:
        motivo = exige_capability(store, "parent_id", True)
        if motivo:
            pytest.skip(motivo)
        pai = MemoryEntry(content="sessao pai", collection="session",
                          metadata={"session_id": "s1"})
        pai_id = await store.store(pai)
        filha = MemoryEntry(content="pacote filho", collection="semantic_log",
                            parent_id=pai_id, metadata={"package_id": "p1"})
        await store.store(filha)
        await store.store(MemoryEntry(content="sem pai", collection="fact"))

        filhas = await store.get_children(pai_id)
        assert [e.content for e in filhas] == ["pacote filho"]


class TestShapeDeSnapshot:
    """Compensacao da divida D-5: o shape de chaves e contrato."""

    CHAVES_LOG = {"id", "label", "message", "entry_count", "created_at"}
    CHAVES_SHOW = {"id", "label", "message", "entry_count", "created_at", "entries"}
    CHAVES_ENTRY = {"entry_id", "content_hash", "entry_data"}
    CHAVES_DIFF = {"added", "removed", "modified"}

    @pytest.mark.asyncio
    async def test_shape_do_log(self, store: MemoryStore) -> None:
        await store.store(MemoryEntry(content="x", collection="fact"))
        await store.snapshot_commit("shape", "mensagem")
        log = await store.snapshot_log()
        assert self.CHAVES_LOG.issubset(set(log[0]))

    @pytest.mark.asyncio
    async def test_shape_do_show(self, store: MemoryStore) -> None:
        await store.store(MemoryEntry(content="x", collection="fact"))
        snap = await store.snapshot_commit("shape")
        dados = await store.snapshot_show(snap)
        assert self.CHAVES_SHOW.issubset(set(dados))
        assert self.CHAVES_ENTRY.issubset(set(dados["entries"][0]))

    @pytest.mark.asyncio
    async def test_shape_do_diff(self, store: MemoryStore) -> None:
        snap_a = await store.snapshot_commit("a")
        snap_b = await store.snapshot_commit("b")
        diff = await store.snapshot_diff(snap_a, snap_b)
        assert set(diff) == self.CHAVES_DIFF

    @pytest.mark.asyncio
    async def test_snapshot_inexistente_falha_alto(self, store: MemoryStore) -> None:
        with pytest.raises(ValueError, match="nao encontrado"):
            await store.snapshot_show("nao-existe")


class TestGovernancaContract:
    """Invariantes que o GovernedStore garante em qualquer backend.

    Marcados com `governanca`: enquanto a camada nao subir (F3.4), os
    backends sem politica no adapter entram como xfail(strict=True).
    """

    @pytest.mark.governanca
    @pytest.mark.asyncio
    async def test_injection_risk_nao_aparece_em_busca_por_tags(
        self, store: MemoryStore
    ) -> None:
        """I5."""
        await store.store(MemoryEntry(
            content="conteudo suspeito", collection="fact",
            tags={"skill": ["risco"]}, injection_risk=True,
        ))
        await store.store(MemoryEntry(
            content="conteudo limpo", collection="fact",
            tags={"skill": ["risco"]},
        ))
        achadas = await store.search_by_tags(tags={"skill": ["risco"]})
        assert [e.content for e in achadas] == ["conteudo limpo"]

    @pytest.mark.governanca
    @pytest.mark.asyncio
    async def test_mandatory_vem_antes_em_busca_por_tags(
        self, store: MemoryStore
    ) -> None:
        """I6 e I9: ordenacao constitucional em qualquer backend."""
        await store.store(MemoryEntry(
            content="comum", collection="fact",
            tags={"skill": ["ordem"]}, priority="low",
        ))
        await store.store(MemoryEntry(
            content="obrigatoria", collection="rule",
            tags={"skill": ["ordem"]}, mandatory=True, priority="critical",
        ))
        await store.store(MemoryEntry(
            content="importante", collection="fact",
            tags={"skill": ["ordem"]}, priority="high",
        ))
        achadas = await store.search_by_tags(tags={"skill": ["ordem"]})
        assert achadas[0].content == "obrigatoria"
        assert [e.content for e in achadas] == ["obrigatoria", "importante", "comum"]

    @pytest.mark.governanca
    @pytest.mark.asyncio
    async def test_acl_de_leitura_esconde_entry_privada(
        self, store: MemoryStore
    ) -> None:
        """I3: com requester_id, so retorna o que ele pode ler."""
        await store.store(MemoryEntry(
            content="privada da alice", collection="fact",
            tags={"skill": ["acl"]}, visibility="private", author_id="alice",
        ))
        await store.store(MemoryEntry(
            content="publica", collection="fact",
            tags={"skill": ["acl"]}, visibility="public", author_id="alice",
        ))
        do_bob = await store.search_by_tags(tags={"skill": ["acl"]}, requester_id="bob")
        assert [e.content for e in do_bob] == ["publica"]

        da_alice = await store.search_by_tags(
            tags={"skill": ["acl"]}, requester_id="alice"
        )
        assert {e.content for e in da_alice} == {"publica", "privada da alice"}

    @pytest.mark.asyncio
    async def test_sem_requester_id_o_comportamento_antigo_e_preservado(
        self, store: MemoryStore
    ) -> None:
        """I3, segunda metade: sem requester, retorna tudo.

        Sem marcador `governanca` de proposito: este invariante vale
        antes e depois de F3.4 (um adapter sem ACL ja retorna tudo).
        Marcar como xfail seria mentira, e o `strict` pegaria na hora.
        """
        await store.store(MemoryEntry(
            content="privada", collection="fact",
            tags={"skill": ["semacl"]}, visibility="private", author_id="alice",
        ))
        achadas = await store.search_by_tags(tags={"skill": ["semacl"]})
        assert len(achadas) == 1


class TestBackfillContract:
    """F3.6: o backfill passa pelo contrato, nao por SQL cru."""

    def _vetor(self, store: MemoryStore, semente: float = 0.5) -> list[float]:
        return [semente] + [0.0] * 1023

    @pytest.mark.asyncio
    async def test_lista_apenas_entries_sem_embedding(self, store: MemoryStore) -> None:
        motivo = exige_capability(store, "backfill", True)
        if motivo:
            pytest.skip(motivo)
        sem = MemoryEntry(content="sem vetor", collection="fact")
        com = MemoryEntry(
            content="com vetor", collection="fact", embedding=self._vetor(store)
        )
        await store.store(sem)
        await store.store(com)
        pendentes = await store.list_entries_without_embedding()
        assert [e.content for e in pendentes] == ["sem vetor"]

    @pytest.mark.asyncio
    async def test_ordem_e_determinista(self, store: MemoryStore) -> None:
        motivo = exige_capability(store, "backfill", True)
        if motivo:
            pytest.skip(motivo)
        for i in range(5):
            await store.store(MemoryEntry(content=f"pendente {i}", collection="fact"))
        primeira = [e.id for e in await store.list_entries_without_embedding()]
        segunda = [e.id for e in await store.list_entries_without_embedding()]
        assert primeira == segunda
        assert len(primeira) == 5

    @pytest.mark.asyncio
    async def test_ignora_entries_expiradas(self, store: MemoryStore) -> None:
        motivo = exige_capability(store, "backfill", True)
        if motivo:
            pytest.skip(motivo)
        await store.store(MemoryEntry(
            content="expirada", collection="fact",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        ))
        await store.store(MemoryEntry(content="viva", collection="fact"))
        pendentes = await store.list_entries_without_embedding()
        assert [e.content for e in pendentes] == ["viva"]

    @pytest.mark.asyncio
    async def test_filtra_por_colecao(self, store: MemoryStore) -> None:
        motivo = exige_capability(store, "backfill", True)
        if motivo:
            pytest.skip(motivo)
        await store.store(MemoryEntry(content="uma regra", collection="rule"))
        await store.store(MemoryEntry(content="um fato", collection="fact"))
        pendentes = await store.list_entries_without_embedding(collection="rule")
        assert [e.collection for e in pendentes] == ["rule"]

    @pytest.mark.asyncio
    async def test_respeita_o_limite(self, store: MemoryStore) -> None:
        motivo = exige_capability(store, "backfill", True)
        if motivo:
            pytest.skip(motivo)
        for i in range(4):
            await store.store(MemoryEntry(content=f"pendente {i}", collection="fact"))
        assert len(await store.list_entries_without_embedding(limit=2)) == 2

    @pytest.mark.asyncio
    async def test_set_embedding_grava_o_vetor(self, store: MemoryStore) -> None:
        motivo = exige_capability(store, "backfill", True)
        if motivo:
            pytest.skip(motivo)
        entry = MemoryEntry(content="sem vetor", collection="fact")
        entry_id = await store.store(entry)
        vetor = self._vetor(store)
        assert await store.set_embedding(entry_id, vetor) is True
        recuperada = await store.retrieve(entry_id)
        assert recuperada is not None
        assert recuperada.embedding is not None
        assert len(recuperada.embedding) == len(vetor)
        assert await store.list_entries_without_embedding() == []

    @pytest.mark.asyncio
    async def test_set_embedding_nao_versiona_nem_incrementa(
        self, store: MemoryStore
    ) -> None:
        """Backfill nao e edicao de conteudo."""
        motivo = exige_capability(store, "backfill", True)
        if motivo:
            pytest.skip(motivo)
        entry = MemoryEntry(content="sem vetor", collection="fact")
        entry_id = await store.store(entry)
        await store.set_embedding(entry_id, self._vetor(store))
        recuperada = await store.retrieve(entry_id)
        assert recuperada is not None
        assert recuperada.version == 1
        assert await store.get_history(entry_id) == []

    @pytest.mark.asyncio
    async def test_set_embedding_em_id_inexistente_devolve_false(
        self, store: MemoryStore
    ) -> None:
        motivo = exige_capability(store, "backfill", True)
        if motivo:
            pytest.skip(motivo)
        assert await store.set_embedding("nao-existe", self._vetor(store)) is False

    @pytest.mark.asyncio
    async def test_set_embedding_com_dimensao_errada_levanta(
        self, store: MemoryStore
    ) -> None:
        motivo = exige_capability(store, "backfill", True)
        if motivo:
            pytest.skip(motivo)
        entry = MemoryEntry(content="sem vetor", collection="fact")
        entry_id = await store.store(entry)
        with pytest.raises(ValueError, match="dimensoes"):
            await store.set_embedding(entry_id, [0.1, 0.2, 0.3])
