"""Integracao do backend Qdrant (F3.5).

Exige alvo de teste aprovado pela guarda de 7.3.1: ORKMIND_TEST_QDRANT_URL
e ORKMIND_TEST_QDRANT_PREFIX com marcador. Sem isso, skip ruidoso.
Nao ha fallback a partir de variavel de producao.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from orkmind.core.models import MemoryEntry
from tests.conftest import SKIP_NO_TEST_QDRANT, resolve_alvo_de_teste
from tests.contract.backends import obter_backend

ALVO = resolve_alvo_de_teste("qdrant")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.qdrant,
    pytest.mark.skipif(not ALVO.destrutivo_ok, reason=SKIP_NO_TEST_QDRANT),
]


@pytest_asyncio.fixture
async def adapter():
    """Adapter Qdrant CRU (sem a camada de governanca)."""
    backend = obter_backend("qdrant")
    if not backend.disponivel:
        pytest.skip(backend.motivo_skip)
    store = backend.construir()
    adapter = getattr(store, "inner", store)
    await adapter.initialize()
    backend.limpar(store)
    await adapter.initialize()
    try:
        yield adapter
    finally:
        backend.limpar(store)
        await adapter.close()


def entrada(**kwargs) -> MemoryEntry:
    base = {"content": "conteudo", "collection": "fact"}
    base.update(kwargs)
    return MemoryEntry(**base)  # type: ignore[arg-type]


class TestInicializacao:
    @pytest.mark.asyncio
    async def test_initialize_e_idempotente(self, adapter) -> None:
        await adapter.initialize()
        await adapter.initialize()
        entry = entrada(content="sobreviveu")
        await adapter.store(entry)
        assert await adapter.count() == 1

    @pytest.mark.asyncio
    async def test_avisa_ausencia_de_indice_unico(
        self, adapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DP-6: a fraqueza de idempotencia e declarada, nunca silenciada."""
        with caplog.at_level(logging.WARNING):
            await adapter.initialize()
        mensagens = [r.getMessage() for r in caplog.records]
        assert any("indice unico" in m and "content_hash" in m for m in mensagens)

    @pytest.mark.asyncio
    async def test_capabilities_declaram_a_tabela_do_plano(self, adapter) -> None:
        cap = adapter.capabilities
        assert cap.backend == "qdrant"
        assert cap.unique_content_hash is False
        assert cap.text_search == "match"
        assert cap.native_acl_filter is False
        assert cap.native_constitutional_order is False


class TestDiscriminadorKind:
    """O `kind` nunca pode vazar entre buscas."""

    @pytest.mark.asyncio
    async def test_versao_nao_aparece_em_busca_por_tags(self, adapter) -> None:
        entry = entrada(content="versao 1", tags={"skill": ["kind"]})
        await adapter.store(entry)
        await adapter.update(
            entry.id, entrada(content="versao 2", tags={"skill": ["kind"]})
        )
        achadas = await adapter.search_by_tags(tags={"skill": ["kind"]})
        assert [e.content for e in achadas] == ["versao 2"]
        assert len(await adapter.get_history(entry.id)) == 1

    @pytest.mark.asyncio
    async def test_snapshot_nao_conta_como_entry(self, adapter) -> None:
        await adapter.store(entrada(content="unica"))
        await adapter.snapshot_commit("v1")
        assert await adapter.count() == 1

    @pytest.mark.asyncio
    async def test_versao_nao_aparece_em_busca_textual(self, adapter) -> None:
        entry = entrada(content="texto original unico")
        await adapter.store(entry)
        await adapter.update(entry.id, entrada(content="texto novo"))
        achadas = await adapter.search_by_text("original")
        assert achadas == []

    @pytest.mark.asyncio
    async def test_versao_nao_aparece_em_busca_vetorial(self, adapter) -> None:
        vetor = [1.0] + [0.0] * 1023
        entry = entrada(content="com vetor", embedding=vetor)
        await adapter.store(entry)
        await adapter.update(entry.id, entrada(content="novo com vetor", embedding=vetor))
        achadas = await adapter.search_semantic(vetor, limit=10)
        assert [e.content for e in achadas] == ["novo com vetor"]


class TestPontoSemEmbedding:
    @pytest.mark.asyncio
    async def test_entry_sem_vetor_e_aceita(self, adapter) -> None:
        """O cliente aceita ponto sem vetor nomeado: nao foi preciso vetor zero."""
        entry = entrada(content="sem vetor")
        await adapter.store(entry)
        recuperada = await adapter.retrieve(entry.id)
        assert recuperada is not None
        assert recuperada.embedding is None

    @pytest.mark.asyncio
    async def test_entry_sem_vetor_nao_aparece_em_busca_vetorial(
        self, adapter
    ) -> None:
        vetor = [1.0] + [0.0] * 1023
        await adapter.store(entrada(content="sem vetor"))
        await adapter.store(entrada(content="com vetor", embedding=vetor))
        achadas = await adapter.search_semantic(vetor, limit=10)
        assert [e.content for e in achadas] == ["com vetor"]


class TestPersistenciaDeCampos:
    @pytest.mark.asyncio
    async def test_todos_os_campos_da_ontologia_sobrevivem(self, adapter) -> None:
        entry = entrada(
            content="completa",
            collection="rule",
            tags={"skill": ["git"], "audience": ["equipe"]},
            priority="critical",
            mandatory=True,
            protected=True,
            injection_risk=True,
            conflict=True,
            visibility="restricted",
            author_id="alice",
            url="https://exemplo.test/x",
            essence="essencia",
            structure="estrutura",
            content_hash="abc123",
        )
        await adapter.store(entry)
        recuperada = await adapter.retrieve(entry.id)
        assert recuperada is not None
        for campo in (
            "content", "collection", "tags", "priority", "mandatory",
            "protected", "injection_risk", "conflict", "visibility",
            "author_id", "url", "essence", "structure", "content_hash",
        ):
            assert getattr(recuperada, campo) == getattr(entry, campo), campo

    @pytest.mark.asyncio
    async def test_expiracao_e_respeitada_na_busca_e_no_gc(self, adapter) -> None:
        passado = datetime.now(timezone.utc) - timedelta(hours=1)
        await adapter.store(entrada(
            content="expirada", tags={"skill": ["ttl"]}, expires_at=passado
        ))
        await adapter.store(entrada(content="viva", tags={"skill": ["ttl"]}))
        achadas = await adapter.search_by_tags(tags={"skill": ["ttl"]})
        assert [e.content for e in achadas] == ["viva"]
        assert await adapter.garbage_collect() == 1
        assert await adapter.count() == 1


class TestZeroPolitica:
    """R0.2 verificado por comportamento: o adapter nao governa."""

    @pytest.mark.asyncio
    async def test_adapter_cru_nao_barra_agente_em_entry_protegida(
        self, adapter
    ) -> None:
        entry = entrada(content="protegida", collection="rule", protected=True)
        await adapter.store(entry)
        # Sem excecao: a protecao D2 e do GovernedStore, nao daqui.
        assert await adapter.delete(entry.id, source="agent") is True

    @pytest.mark.asyncio
    async def test_adapter_cru_nao_aplica_acl(self, adapter) -> None:
        await adapter.store(entrada(
            content="privada", tags={"skill": ["acl"]},
            visibility="private", author_id="alice",
        ))
        achadas = await adapter.search_by_tags(
            tags={"skill": ["acl"]}, requester_id="bob"
        )
        assert len(achadas) == 1

    @pytest.mark.asyncio
    async def test_adapter_cru_nao_filtra_injection_risk(self, adapter) -> None:
        await adapter.store(entrada(
            content="suspeita", tags={"skill": ["inj"]}, injection_risk=True
        ))
        achadas = await adapter.search_by_tags(tags={"skill": ["inj"]})
        assert len(achadas) == 1


class TestLimpezaPorPrefixo:
    def test_limpeza_so_apaga_colecoes_do_prefixo_aprovado(self) -> None:
        """Regra 3 de 7.3.1, verificada no codigo da limpeza."""
        import inspect

        from tests.contract.backends import _limpar_qdrant

        fonte = inspect.getsource(_limpar_qdrant)
        assert "assert_destrutivo_permitido" in fonte
        assert "startswith(alvo.prefixo)" in fonte
        assert "delete_collection" in fonte

    def test_nome_da_colecao_usa_o_prefixo_aprovado(self, adapter) -> None:
        assert adapter.collection_name.startswith(ALVO.prefixo)
