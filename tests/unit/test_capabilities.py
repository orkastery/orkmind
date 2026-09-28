"""Testes do StoreCapabilities (F3.2).

Capabilities e o unico lugar onde uma degradacao de backend pode ser
declarada (R0.3). Estes testes fixam o shape e o default otimista.
"""

from __future__ import annotations

import dataclasses

import pytest

from orkmind.store.capabilities import StoreCapabilities

CAMPOS_ESPERADOS = {
    "backend",
    "vector_search",
    "accepts_external_vectors",
    "stores_embedding",
    "native_text_semantic",
    "text_search",
    "tag_filter",
    "unique_content_hash",
    "versioning",
    "snapshots",
    "parent_id",
    "ttl_gc",
    "durable",
    "native_acl_filter",
    "native_constitutional_order",
    "backfill",
}


class TestDefaults:
    def test_default_e_otimista(self) -> None:
        """Adapter que nao declara nada se comporta como o historico."""
        cap = StoreCapabilities()
        assert cap.backend == "desconhecido"
        assert cap.vector_search is True
        assert cap.accepts_external_vectors is True
        assert cap.stores_embedding is True
        assert cap.text_search == "ranked"
        assert cap.tag_filter is True
        assert cap.versioning is True
        assert cap.snapshots is True
        assert cap.parent_id is True
        assert cap.ttl_gc is True
        assert cap.durable is True
        assert cap.backfill is True

    def test_defaults_conservadores_de_otimizacao(self) -> None:
        """Otimizacao nao se presume: quem nao declara nao e confiado."""
        cap = StoreCapabilities()
        assert cap.native_acl_filter is False
        assert cap.native_constitutional_order is False
        assert cap.native_text_semantic is False
        assert cap.unique_content_hash is False


class TestShape:
    def test_as_dict_cobre_todos_os_campos(self) -> None:
        d = StoreCapabilities().as_dict()
        assert set(d) == CAMPOS_ESPERADOS

    def test_campos_do_dataclass_sao_os_esperados(self) -> None:
        nomes = {f.name for f in dataclasses.fields(StoreCapabilities)}
        assert nomes == CAMPOS_ESPERADOS

    def test_e_imutavel(self) -> None:
        cap = StoreCapabilities(backend="memory")
        with pytest.raises(dataclasses.FrozenInstanceError):
            cap.backend = "outro"  # type: ignore[misc]

    def test_e_hashavel(self) -> None:
        assert {StoreCapabilities(backend="a"), StoreCapabilities(backend="a")} != set()
        assert len({StoreCapabilities(backend="a"), StoreCapabilities(backend="a")}) == 1


class TestAvisos:
    def test_default_otimista_avisa_apenas_o_hash_unico(self) -> None:
        avisos = StoreCapabilities().avisos()
        assert len(avisos) == 1
        assert "content_hash" in avisos[0]

    def test_backend_volatil_avisa_durabilidade(self) -> None:
        avisos = StoreCapabilities(backend="memory", durable=False).avisos()
        assert any("nao persiste entre processos" in a for a in avisos)
        assert any("memory" in a for a in avisos)

    def test_sem_busca_vetorial_avisa(self) -> None:
        avisos = StoreCapabilities(backend="x", vector_search=False).avisos()
        assert any("nao faz busca vetorial" in a for a in avisos)

    def test_busca_textual_sem_ranking_avisa(self) -> None:
        avisos = StoreCapabilities(backend="qdrant", text_search="match").avisos()
        assert any("sem ranking" in a for a in avisos)

    def test_backend_completo_nao_avisa_nada(self) -> None:
        cap = StoreCapabilities(
            backend="pgvector",
            unique_content_hash=True,
            text_search="ranked",
        )
        assert cap.avisos() == []


class TestContratoDeclaraCapabilities:
    def test_memory_store_tem_property_capabilities(self) -> None:
        from orkmind.store.base import MemoryStore

        assert isinstance(MemoryStore.capabilities, property)

    def test_pgvector_declara_o_da_tabela_do_plano(self) -> None:
        from orkmind.store.postgres_adapter import PGVECTOR_CAPABILITIES

        assert PGVECTOR_CAPABILITIES.backend == "pgvector"
        assert PGVECTOR_CAPABILITIES.text_search == "ranked"
        assert PGVECTOR_CAPABILITIES.unique_content_hash is True
        assert PGVECTOR_CAPABILITIES.native_acl_filter is True
        assert PGVECTOR_CAPABILITIES.native_constitutional_order is True
        assert PGVECTOR_CAPABILITIES.durable is True
        assert PGVECTOR_CAPABILITIES.avisos() == []
