"""Testes E.1-E.6 da memoria autoritativa (Fase 2.7).

Cobrem os cenarios definidos no plano da memoria autoritativa:

E.1 estresse (mais entries que o budget)
E.2 nao-destruicao (consolidacao e adicao, nunca substituicao)
E.3 projecao (prefetch/recall nunca alteram o armazenamento)
E.4 regra mandatoria anti-destruicao (injecao + protecao D2)
E.5 recuperacao semantica sob pressao de budget
E.6 historico de versoes com content_hash preservado

Requer um banco de TESTES (a fixture apaga todos os dados):
    ORKMIND_TEST_DATABASE_URL=postgresql://.../orkmind_test pytest tests/integration
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from orkmind.core.injection import compute_content_hash
from orkmind.core.models import MemoryEntry
from orkmind.core.ontology import ProtectionError
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.hermes.provider import OrkMindMemoryProvider
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
    await conn.execute("DELETE FROM snapshot_entries")
    await conn.execute("DELETE FROM snapshots")
    await conn.execute("DELETE FROM memory_versions")
    await conn.execute("DELETE FROM memories")
    yield adapter
    await conn.execute("DELETE FROM snapshot_entries")
    await conn.execute("DELETE FROM snapshots")
    await conn.execute("DELETE FROM memory_versions")
    await conn.execute("DELETE FROM memories")
    await adapter.close()


@pytest_asyncio.fixture
async def layer(store: PostgresAdapter):
    return SemanticLayer(store, token_budget=2000)


async def _popular(layer: SemanticLayer, quantidade: int) -> list[str]:
    """Cria N entries diversas e retorna seus ids."""
    ids: list[str] = []
    colecoes = ("fact", "learning", "preference", "decision", "content")
    for i in range(quantidade):
        entry = MemoryEntry(
            content=(
                f"Memoria numero {i}: detalhe operacional relevante sobre o "
                f"subsistema {i % 7} do OrkMind, com contexto suficiente "
                f"para ocupar espaco no budget de contexto."
            ),
            collection=colecoes[i % len(colecoes)],  # type: ignore[arg-type]
            tags={"domain": ["backend"], "project": ["orkmind"]},
            priority="medium",
        )
        entry_id, _ = await layer.add_memory(entry)
        ids.append(entry_id)
    return ids


# --- E.1 Estresse -----------------------------------------------------------


@pytest.mark.asyncio
async def test_e1_estresse_entries_permanecem_intactas(
    layer: SemanticLayer, store: PostgresAdapter
) -> None:
    """E.1: budget baixo com 50+ entries nao remove nada do armazenamento."""
    ids = await _popular(layer, 55)
    antes = await store.count()
    assert antes == 55

    resultado = await layer.query_for_context(
        conversation="preciso de detalhes sobre o subsistema 3",
        token_budget=2000,
    )
    # o contexto e uma projecao limitada, nao a base inteira
    assert len(resultado) < antes

    depois = await store.count()
    assert depois == antes
    for entry_id in ids:
        assert await store.retrieve(entry_id) is not None


@pytest.mark.asyncio
async def test_e1_busca_alcanca_alem_do_contexto_injetado(
    layer: SemanticLayer,
) -> None:
    """E.1: o que nao coube no contexto continua acessivel por busca."""
    await _popular(layer, 55)
    encontrados = await layer.store.search_by_text("subsistema", limit=100)
    assert len(encontrados) > 10


# --- E.2 Nao-destruicao -----------------------------------------------------


@pytest.mark.asyncio
async def test_e2_consolidacao_e_adicao_nao_substituicao(
    layer: SemanticLayer, store: PostgresAdapter
) -> None:
    """E.2: resumir a colecao fact adiciona entry, originais intactas."""
    originais: list[tuple[str, str]] = []
    for i in range(5):
        entry = MemoryEntry(
            content=f"Fato extenso numero {i} com detalhes que nao podem ser perdidos.",
            collection="fact",
            tags={"domain": ["backend"]},
        )
        entry_id, _ = await layer.add_memory(entry)
        armazenada = await store.retrieve(entry_id)
        assert armazenada is not None and armazenada.content_hash
        originais.append((entry_id, armazenada.content_hash))

    resumo = MemoryEntry(
        content="Resumo consolidado dos 5 fatos do backend.",
        collection="fact",
        tags={"domain": ["backend"]},
        source="agent",
    )
    await layer.add_memory(resumo)

    assert await store.count(collection="fact") == 6
    for entry_id, content_hash in originais:
        atual = await store.retrieve(entry_id)
        assert atual is not None
        assert atual.content_hash == content_hash


@pytest.mark.asyncio
async def test_e2_agente_nao_deleta_entry_protegida(
    layer: SemanticLayer, store: PostgresAdapter
) -> None:
    """E.2: agente nao consegue apagar entry protegida para abrir espaco."""
    entry = MemoryEntry(
        content="Conteudo protegido que o agente nao pode remover.",
        collection="fact",
        protected=True,
        source="human",
    )
    entry_id, _ = await layer.add_memory(entry)

    with pytest.raises(ProtectionError):
        await store.delete(entry_id, source="agent")

    assert await store.retrieve(entry_id) is not None


# --- E.3 Projecao -----------------------------------------------------------


@pytest.mark.asyncio
async def test_e3_recall_nao_altera_armazenamento(
    layer: SemanticLayer, store: PostgresAdapter
) -> None:
    """E.3: 10 ciclos de recall nao adicionam, removem ou alteram entries."""
    await _popular(layer, 20)
    snap_antes = await store.snapshot_commit(label="pre-teste-e3")

    provider = OrkMindMemoryProvider(layer=layer)
    for i in range(10):
        await provider.recall(context=f"pergunta numero {i} sobre subsistema")

    snap_depois = await store.snapshot_commit(label="pos-teste-e3")
    diff = await store.snapshot_diff(snap_antes, snap_depois)
    assert diff["removed"] == []
    assert diff["modified"] == []


# --- E.4 Regra mandatoria anti-destruicao -----------------------------------


@pytest.mark.asyncio
async def test_e4_regra_anti_destruicao_e_injetada(layer: SemanticLayer) -> None:
    """E.4: a regra de governanca chega ao contexto do agente."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from seed_governance import ANTI_DESTRUICAO

    regra = MemoryEntry(
        content=ANTI_DESTRUICAO,
        collection="rule",
        tags={"domain": ["governance"]},
        priority="critical",
        mandatory=True,
        protected=True,
        source="human",
    )
    await layer.add_memory(regra)

    provider = OrkMindMemoryProvider(layer=layer)
    regras = await provider.get_rules()
    assert any("NUNCA consolidar ou reduzir memorias" in r["content"] for r in regras)
    assert all(r["mandatory"] for r in regras)


@pytest.mark.asyncio
async def test_e4_regra_critica_resiste_a_alteracao_por_agente(
    layer: SemanticLayer, store: PostgresAdapter
) -> None:
    """E.4: D2 impede que o agente relaxe a propria regra de governanca."""
    regra = MemoryEntry(
        content="REGRA DE GOVERNANCA: nao destruir memorias.",
        collection="rule",
        tags={"domain": ["governance"]},
        priority="critical",
        mandatory=True,
        protected=True,
        source="human",
    )
    entry_id, _ = await layer.add_memory(regra)

    alterada = regra.model_copy(update={"content": "Pode apagar", "source": "agent"})
    with pytest.raises(ProtectionError):
        await store.update(entry_id, alterada)
    with pytest.raises(ProtectionError):
        await store.delete(entry_id, source="agent")

    atual = await store.retrieve(entry_id)
    assert atual is not None
    assert "nao destruir memorias" in atual.content


# --- E.5 Recuperacao semantica sob pressao de budget ------------------------


@pytest.mark.asyncio
async def test_e5_fato_fora_do_contexto_ainda_e_encontravel(
    layer: SemanticLayer,
) -> None:
    """E.5: com 100 entries e budget baixo, a busca alcanca o fato especifico."""
    # a agulha entra primeiro para ficar no fim da ordenacao por recencia
    agulha = MemoryEntry(
        content="A chave de licenca do compilador Zephyrus expira em marco.",
        collection="fact",
        tags={"domain": ["licencas"]},
        priority="low",
    )
    await layer.add_memory(agulha)
    await _popular(layer, 100)

    contexto = await layer.query_for_context(
        conversation="detalhe operacional do subsistema", token_budget=2000
    )
    conteudos = [e.content for e in contexto]
    assert not any("Zephyrus" in c for c in conteudos)

    encontrados = await layer.store.search_by_text("Zephyrus", limit=10)
    assert any("Zephyrus" in e.content for e in encontrados)


@pytest.mark.asyncio
async def test_e5_budget_menor_reduz_contexto(layer: SemanticLayer) -> None:
    """E.5: o budget e respeitado - projecao menor, base inalterada."""
    await _popular(layer, 60)
    pequeno = await layer.query_for_context(conversation="subsistema", token_budget=500)
    grande = await layer.query_for_context(conversation="subsistema", token_budget=8000)
    assert len(pequeno) <= len(grande)
    assert await layer.store.count() == 60


# --- E.6 Historico de versoes -----------------------------------------------


@pytest.mark.asyncio
async def test_e6_historico_preserva_versao_anterior(
    layer: SemanticLayer, store: PostgresAdapter
) -> None:
    """E.6: apos update, a versao anterior fica no historico com seu hash."""
    entry = MemoryEntry(content="Conteudo original", collection="fact")
    entry_id, _ = await layer.add_memory(entry)
    hash_original = compute_content_hash("Conteudo original")

    atualizada = entry.model_copy(
        update={
            "content": "Conteudo revisado",
            "content_hash": compute_content_hash("Conteudo revisado"),
        }
    )
    assert await store.update(entry_id, atualizada) is True

    historico = await store.get_history(entry_id)
    assert len(historico) == 1
    assert historico[0].content == "Conteudo original"
    assert historico[0].content_hash == hash_original

    atual = await store.retrieve(entry_id)
    assert atual is not None
    assert atual.content == "Conteudo revisado"
    assert atual.version == 2
