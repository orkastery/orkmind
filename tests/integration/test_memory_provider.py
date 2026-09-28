"""Integracao do Memory Provider nativo contra PostgreSQL + pgvector reais.

Exige ORKMIND_PROVIDER_TEST_DATABASE_URL. Nao ha fallback para nenhuma variavel
de producao, e o nome do banco precisa ter marcador de teste (mesma guarda do
restante da suite). Cada teste cria o PROPRIO schema (`mp_test_<hex>`) e so
derruba esse schema: nada fora dele e tocado.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError

from orkmind.embeddings.provider import EmbeddingError, EmbeddingProvider
from tests.conftest import is_test_database

asyncpg = pytest.importorskip("asyncpg", reason="extra orkmind[memory-provider] nao instalado")

from orkmind.memory_provider import (  # noqa: E402
    AccessLevel,
    ChunkingOptions,
    CoreBlockLabel,
    CoreMemoryMissingError,
    DocumentIngestRequest,
    DocumentNotFoundError,
    EmbeddingDimensionError,
    HashingEmbeddingProvider,
    IngestStatus,
    MemoryProviderSettings,
    MemoryScopeFilter,
    NativeMemoryProvider,
    SchemaMismatchError,
    UserContext,
)
from orkmind.memory_provider.db.migrations import SCHEMA_VERSION, apply_schema  # noqa: E402
from orkmind.memory_provider.models.schemas import IngestJobState  # noqa: E402

TEST_DB_ENV = "ORKMIND_PROVIDER_TEST_DATABASE_URL"
DATABASE_URL = os.environ.get(TEST_DB_ENV, "")
DIM = 64

pytestmark = [
    pytest.mark.integration,
    pytest.mark.memory_provider,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason=f"Sem banco de testes do Memory Provider. Defina {TEST_DB_ENV} apontando para "
        f"um PostgreSQL com pgvector cujo nome de banco contenha 'test'.",
    ),
]

ANA = UserContext(user_id="ana", role=AccessLevel.OPERATIONAL, departments=["noc"])
CAIO = UserContext(user_id="caio", role=AccessLevel.OPERATIONAL, departments=["financeiro"])
DORA = UserContext(user_id="dora", role=AccessLevel.EXECUTIVE, departments=["diretoria"])

CONTRATO = """# Contrato de Transito IP

## Objeto

Contrato CT-2024/0187 firmado com a operadora de transito para o backbone da
rede neutra. Vigencia de 36 meses com renovacao automatica.

## Valores

O valor mensal do contrato CT-2024/0187 e de R$ 184.500,00, reajustado pelo
IPCA a cada doze meses. Multa rescisoria de tres mensalidades.

## SLA

Disponibilidade minima de 99,95% medida mensalmente, com credito proporcional
em caso de descumprimento.
"""

PLAYBOOK = """# Playbook de Incidentes do NOC

## Triagem

Todo incidente entra pela fila do NOC e recebe severidade em ate dez minutos.
Incidentes de rede neutra com impacto em provedores parceiros sao SEV1.

## Escalonamento

SEV1 aciona o plantao de engenharia de backbone imediatamente. SEV2 segue o
fluxo padrao com prazo de uma hora para primeira resposta.
"""

ESTUDO = """# Estudo Estrategico de Aquisicao

## Tese

Avaliacao sigilosa da aquisicao de um provedor regional para expandir a rede
neutra no oeste catarinense. Valor alvo em negociacao, nao divulgar.

## Consolidacao cross-department

Sinergias de OPEX entre engenharia, financeiro e comercial estimadas em 12%
no primeiro ano apos a integracao.
"""


def _doc(slug: str, title: str, raw: str, **overrides: object) -> DocumentIngestRequest:
    base: dict[str, object] = {
        "slug": slug,
        "title": title,
        "doc_type": "playbook",
        "raw_content": raw,
        "access_level": AccessLevel.OPERATIONAL,
    }
    base.update(overrides)
    return DocumentIngestRequest(**base)  # type: ignore[arg-type]


def _settings(schema: str, **overrides: object) -> MemoryProviderSettings:
    base: dict[str, object] = {
        "database_url": DATABASE_URL,
        "db_schema": schema,
        "embedding_dim": DIM,
        "pool_max_size": 6,
        "chunking": ChunkingOptions(max_tokens=120, min_tokens=8),
    }
    base.update(overrides)
    return MemoryProviderSettings(**base)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def schema() -> AsyncIterator[str]:
    if not is_test_database(DATABASE_URL):
        raise RuntimeError(
            f"{TEST_DB_ENV} nao parece um banco de testes. Use um banco com 'test' no nome."
        )
    name = f"mp_test_{uuid4().hex[:12]}"
    yield name
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def memory(schema: str) -> AsyncIterator[NativeMemoryProvider]:
    provider = await NativeMemoryProvider.connect(
        _settings(schema), embedder=HashingEmbeddingProvider(DIM)
    )
    try:
        yield provider
    finally:
        await provider.close()


@pytest_asyncio.fixture
async def wiki(memory: NativeMemoryProvider) -> NativeMemoryProvider:
    """Tres documentos: operacional do NOC, operacional do financeiro, executivo."""
    await memory.ingest_document(
        _doc(
            "playbook-incidentes-noc",
            "Playbook de Incidentes do NOC",
            PLAYBOOK,
            department_scope=["noc"],
            tags=["rede_neutra"],
            index_paths=["ENGENHARIA/PLAYBOOK_NOC"],
        )
    )
    await memory.ingest_document(
        _doc(
            "contrato-transito-ip",
            "Contrato de Transito IP",
            CONTRATO,
            doc_type="opex_analysis",
            department_scope=["financeiro"],
            metadata_tags={"contrato": "CT-2024/0187"},
            index_paths=["FINANCAS_OPEX/CONTRATOS_TI"],
        )
    )
    await memory.ingest_document(
        _doc(
            "estudo-aquisicao-oeste",
            "Estudo Estrategico de Aquisicao",
            ESTUDO,
            doc_type="deep_research",
            access_level=AccessLevel.EXECUTIVE,
            index_paths=["PESQUISA/AQUISICAO_OESTE"],
        )
    )
    return memory


async def _raw(memory: NativeMemoryProvider, sql: str, *args: object) -> list[asyncpg.Record]:
    async with memory._pool.acquire() as conn:
        return list(await conn.fetch(sql, *args))


# ---------------------------------------------------------------------------
# Schema e migracao
# ---------------------------------------------------------------------------


class TestSchema:
    @pytest.mark.asyncio
    async def test_tabelas_e_indices_existem(self, memory: NativeMemoryProvider) -> None:
        tabelas = await _raw(
            memory,
            "SELECT tablename FROM pg_tables WHERE schemaname = $1",
            memory.settings.db_schema,
        )
        assert {
            "core_blocks",
            "chat_history",
            "wiki_documents",
            "wiki_document_versions",
            "wiki_chunks",
            "meta_index",
            "memory_provider_meta",
        } <= {row["tablename"] for row in tabelas}

        indices = await _raw(
            memory,
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = $1",
            memory.settings.db_schema,
        )
        defs = {row["indexname"]: row["indexdef"] for row in indices}
        assert "USING hnsw (embedding vector_cosine_ops)" in defs["wiki_chunks_embedding_hnsw"]
        assert "m='16'" in defs["wiki_chunks_embedding_hnsw"]
        assert "ef_construction='64'" in defs["wiki_chunks_embedding_hnsw"]
        assert "USING gin (tsv)" in defs["wiki_chunks_tsv_gin"]
        assert "USING gin (metadata_tags)" in defs["wiki_chunks_tags_gin"]

    @pytest.mark.asyncio
    async def test_migracao_e_idempotente(self, memory: NativeMemoryProvider) -> None:
        await apply_schema(memory.settings)
        await apply_schema(memory.settings)
        meta = await _raw(memory, "SELECT key, value FROM memory_provider_meta")
        assert {row["key"]: row["value"] for row in meta} == {
            "schema_version": SCHEMA_VERSION,
            "embedding_dim": str(DIM),
            "fts_config": "portuguese",
        }

    @pytest.mark.asyncio
    async def test_trocar_dimensao_e_recusado(self, memory: NativeMemoryProvider) -> None:
        outro = _settings(memory.settings.db_schema, embedding_dim=128)
        with pytest.raises(SchemaMismatchError, match="embedding_dim=64"):
            await apply_schema(outro)

    @pytest.mark.asyncio
    async def test_embedder_com_dimensao_errada_e_recusado(self, schema: str) -> None:
        with pytest.raises(EmbeddingDimensionError):
            await NativeMemoryProvider.connect(
                _settings(schema), embedder=HashingEmbeddingProvider(32)
            )


# ---------------------------------------------------------------------------
# Tier 1 - Core Memory
# ---------------------------------------------------------------------------


class TestCoreMemory:
    @pytest.mark.asyncio
    async def test_sem_blocos_obrigatorios_o_agente_nao_roda(
        self, memory: NativeMemoryProvider
    ) -> None:
        with pytest.raises(CoreMemoryMissingError, match="persona, system_invariants"):
            await memory.get_system_context("assistant", ANA)
        assert (await memory.get_system_context("assistant", ANA, strict=False)).blocks == []

    @pytest.mark.asyncio
    async def test_contexto_pinado_na_ordem_com_perfil_do_usuario(
        self, memory: NativeMemoryProvider
    ) -> None:
        await memory.set_core_block(
            "assistant", "system_invariants", "Nunca exponha dado sigiloso.", created_by="tomas"
        )
        await memory.set_core_block(
            "assistant", "persona", "Voce e o assistente da empresa.", created_by="tomas"
        )
        await memory.set_core_block(
            "assistant", "user_profile", "Perfil generico.", created_by="tomas"
        )
        await memory.set_core_block(
            "assistant",
            "user_profile",
            "Ana prefere respostas curtas.",
            created_by="tomas",
            scope_key="ana",
        )

        ana = await memory.get_system_context("assistant", ANA)
        assert [b.label.value for b in ana.blocks] == [
            "persona",
            "system_invariants",
            "user_profile",
        ]
        assert ana.blocks[2].content == "Ana prefere respostas curtas."
        texto = ana.render()
        assert texto.index("<persona>") < texto.index("<system_invariants>")
        assert "role: OPERATIONAL" in texto and "departments: NOC" in texto
        assert ana.token_estimate > 0

        caio = await memory.get_system_context("assistant", CAIO)
        assert caio.blocks[2].content == "Perfil generico."

    @pytest.mark.asyncio
    async def test_trocar_bloco_versiona_em_vez_de_editar(
        self, memory: NativeMemoryProvider
    ) -> None:
        v1 = await memory.set_core_block("assistant", "persona", "Persona v1.", created_by="tomas")
        mesmo = await memory.set_core_block(
            "assistant", "persona", "Persona v1.", created_by="outro"
        )
        v2 = await memory.set_core_block("assistant", "persona", "Persona v2.", created_by="tomas")
        assert (v1.version, mesmo.id, v2.version) == (1, v1.id, 2)

        historico = await memory.core.history("assistant", CoreBlockLabel.PERSONA)
        assert [b.content for b in historico] == ["Persona v2.", "Persona v1."]

    @pytest.mark.asyncio
    async def test_banco_recusa_editar_ou_apagar_bloco(self, memory: NativeMemoryProvider) -> None:
        await memory.set_core_block("assistant", "persona", "Original.", created_by="tomas")
        for sql in (
            "UPDATE core_blocks SET content = 'adulterado'",
            "DELETE FROM core_blocks",
            "TRUNCATE core_blocks",
        ):
            with pytest.raises(asyncpg.exceptions.RestrictViolationError):
                await _raw(memory, sql)

    @pytest.mark.asyncio
    async def test_scope_key_so_vale_em_user_profile(self, memory: NativeMemoryProvider) -> None:
        with pytest.raises(ValueError, match="scope_key"):
            await memory.set_core_block(
                "assistant", "persona", "x", created_by="j", scope_key="ana"
            )


# ---------------------------------------------------------------------------
# Tier 2 - Recall Memory
# ---------------------------------------------------------------------------


class TestRecallMemory:
    @pytest.mark.asyncio
    async def test_janela_tem_os_ultimos_seis_turnos_e_o_banco_tem_todos(
        self, memory: NativeMemoryProvider
    ) -> None:
        for n in range(1, 11):
            await memory.append_history("s1", "user", f"pergunta {n}", user_id="ana")
            await memory.append_history(
                "s1", "tool_call", "consulta", [{"name": "search_wiki", "args": {"q": n}}]
            )
            await memory.append_history("s1", "tool_result", f"resultado {n}")
            await memory.append_history("s1", "assistant", f"resposta {n}")

        janela = await memory.get_recent_history("s1")
        assert {m.turn_index for m in janela} == {5, 6, 7, 8, 9, 10}
        assert len(janela) == 24
        assert janela[0].content == "pergunta 5" and janela[-1].content == "resposta 10"
        assert [m.seq for m in janela] == sorted(m.seq for m in janela)
        assert janela[1].tool_calls == [{"name": "search_wiki", "args": {"q": 5}}]

        integro = await memory.get_full_history("s1")
        assert len(integro) == 40 and integro[0].content == "pergunta 1"
        assert len(await memory.get_recent_history("s1", turns=2)) == 8
        assert await memory.get_recent_history("outra-sessao") == []

    @pytest.mark.asyncio
    async def test_historico_e_append_only_no_banco(self, memory: NativeMemoryProvider) -> None:
        await memory.append_history("s1", "user", "nao me apague")
        for sql in (
            "UPDATE chat_history SET content = 'resumo vago'",
            "DELETE FROM chat_history",
            "TRUNCATE chat_history",
        ):
            with pytest.raises(asyncpg.exceptions.RestrictViolationError):
                await _raw(memory, sql)
        assert (await memory.get_full_history("s1"))[0].content == "nao me apague"

    @pytest.mark.asyncio
    async def test_appends_concorrentes_nao_corrompem_os_turnos(
        self, memory: NativeMemoryProvider
    ) -> None:
        await asyncio.gather(
            *(memory.append_history("s1", "user", f"mensagem {n}") for n in range(12))
        )
        turnos = [m.turn_index for m in await memory.get_full_history("s1")]
        assert sorted(turnos) == list(range(1, 13))

    @pytest.mark.asyncio
    async def test_mensagem_gigante_entra_paginada_e_se_le_inteira(
        self, memory: NativeMemoryProvider
    ) -> None:
        gigante = " ".join(f"linha {n} do relatorio de trafego" for n in range(4000))
        await memory.append_history("s1", "user", "traga o relatorio")
        gravada = await memory.append_history("s1", "tool_result", gigante)

        vista = (await memory.get_recent_history("s1"))[-1]
        assert vista.truncated and vista.next_offset
        assert "paginado" in vista.content and str(gravada.id) in vista.content
        assert len(vista.content) < len(gigante) / 5

        paginas, offset = [], 0
        while offset is not None:
            pagina = await memory.read_history_message(gravada.id, offset=offset)
            paginas.append(pagina.content)
            offset = pagina.next_offset
        assert len(paginas) > 5 and "".join(paginas) == gigante
        assert (await memory.get_full_history("s1"))[-1].content == gigante

    @pytest.mark.asyncio
    async def test_turno_antigo_se_recupera_por_busca_nao_por_resumo(
        self, memory: NativeMemoryProvider
    ) -> None:
        await memory.append_history(
            "s1", "user", "o circuito CKT-7781 de Blumenau caiu", user_id="ana"
        )
        for n in range(10):
            await memory.append_history("s1", "user", f"assunto diferente {n}", user_id="ana")
        await memory.append_history("s2", "user", "CKT-7781 e meu tambem", user_id="caio")

        assert all("CKT-7781" not in m.content for m in await memory.get_recent_history("s1"))
        achados = await memory.search_history("qual circuito caiu? CKT-7781", session_id="s1")
        assert achados and achados[0].content.startswith("o circuito CKT-7781")
        assert {m.session_id for m in achados} == {"s1"}
        de_ana = await memory.search_history("CKT-7781", user_id="ana")
        assert {m.user_id for m in de_ana} == {"ana"}
        with pytest.raises(ValueError, match="session_id ou user_id"):
            await memory.search_history("CKT-7781")

    @pytest.mark.asyncio
    async def test_tool_calls_so_em_papel_de_agente(self, memory: NativeMemoryProvider) -> None:
        with pytest.raises(ValueError, match="tool_calls"):
            await memory.append_history("s1", "user", "oi", [{"name": "x"}])
        with pytest.raises(ValueError):
            await memory.append_history("s1", "system", "papel inexistente")


# ---------------------------------------------------------------------------
# Tier 3 - Ingestao
# ---------------------------------------------------------------------------


class TestIngestion:
    @pytest.mark.asyncio
    async def test_documento_integro_mais_chunks_indexados(
        self, memory: NativeMemoryProvider
    ) -> None:
        bruto = CONTRATO.replace("\n", "\r\n")
        resultado = await memory.ingest_document(
            _doc("contrato", "Contrato de Transito IP", bruto, tags=["opex"])
        )
        assert resultado.status is IngestStatus.CREATED and resultado.version == 1
        assert resultado.chunk_count >= 3

        doc = (await _raw(memory, "SELECT * FROM wiki_documents"))[0]
        assert doc["raw_content"] == bruto and doc["content_hash"] == resultado.content_hash
        assert doc["chunk_count"] == resultado.chunk_count

        chunks = await _raw(memory, "SELECT * FROM wiki_chunks ORDER BY chunk_index")
        assert [c["chunk_index"] for c in chunks] == list(range(resultado.chunk_count))
        assert all(
            c["document_id"] == doc["id"] and len(c["embedding"].to_list()) == DIM for c in chunks
        )
        valores = next(c for c in chunks if "184.500,00" in c["content"])
        assert valores["heading_path"] == "Contrato de Transito IP > Valores"
        assert valores["metadata_tags"]["tags"] == ["opex"]
        assert valores["metadata_tags"]["heading_path"] == ["Contrato de Transito IP", "Valores"]
        assert valores["metadata_tags"]["total_chunks"] == resultado.chunk_count

    @pytest.mark.asyncio
    async def test_hash_repetido_aborta_sem_gravar_nem_embedar(
        self, memory: NativeMemoryProvider
    ) -> None:
        primeiro = await memory.ingest_document(_doc("contrato", "Contrato", CONTRATO))

        class Proibido(EmbeddingProvider):
            dim = DIM  # type: ignore[assignment]

            async def embed(self, text: str) -> list[float]:
                raise AssertionError("duplicata nao pode chamar embedding")

            async def embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise AssertionError("duplicata nao pode chamar embedding")

        memory.ingestion._embedder = Proibido()
        mesmo_slug = await memory.ingest_document(_doc("contrato", "Contrato", CONTRATO))
        outro_slug = await memory.ingest_document(_doc("contrato-copia", "Copia", CONTRATO))

        assert mesmo_slug.status is outro_slug.status is IngestStatus.DUPLICATE
        assert mesmo_slug.duplicate_of is None and outro_slug.duplicate_of == "contrato"
        assert outro_slug.document_id == primeiro.document_id
        assert len(await _raw(memory, "SELECT 1 FROM wiki_documents")) == 1
        assert len(await _raw(memory, "SELECT 1 FROM wiki_document_versions")) == 1
        assert len(await _raw(memory, "SELECT 1 FROM wiki_chunks")) == primeiro.chunk_count

    @pytest.mark.asyncio
    async def test_force_update_reprocessa_e_versiona(self, memory: NativeMemoryProvider) -> None:
        v1 = await memory.ingest_document(_doc("contrato", "Contrato", CONTRATO))
        v2 = await memory.ingest_document(_doc("contrato", "Contrato", CONTRATO, force_update=True))
        assert (v2.status, v2.version, v2.document_id) == (
            IngestStatus.UPDATED,
            2,
            v1.document_id,
        )
        assert len(await _raw(memory, "SELECT 1 FROM wiki_chunks")) == v1.chunk_count

    @pytest.mark.asyncio
    async def test_conteudo_novo_troca_os_chunks_e_preserva_a_versao_antiga(
        self, memory: NativeMemoryProvider
    ) -> None:
        v1 = await memory.ingest_document(_doc("contrato", "Contrato", CONTRATO))
        aditivo = CONTRATO.replace("184.500,00", "201.300,00")
        v2 = await memory.ingest_document(_doc("contrato", "Contrato", aditivo, ingested_by="dora"))
        assert v2.status is IngestStatus.UPDATED and v2.version == 2
        assert v2.document_id == v1.document_id

        textos = " ".join(
            r["content"] for r in await _raw(memory, "SELECT content FROM wiki_chunks")
        )
        assert "201.300,00" in textos and "184.500,00" not in textos

        versoes = await _raw(
            memory,
            "SELECT version, raw_content, recorded_by FROM wiki_document_versions ORDER BY version",
        )
        assert [v["version"] for v in versoes] == [1, 2]
        assert "184.500,00" in versoes[0]["raw_content"] and versoes[1]["recorded_by"] == "dora"
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await _raw(memory, "DELETE FROM wiki_document_versions")

    @pytest.mark.asyncio
    async def test_reclassificar_gera_revisao_sem_reembedar(
        self, memory: NativeMemoryProvider
    ) -> None:
        v1 = await memory.ingest_document(
            _doc("contrato", "Contrato", CONTRATO, metadata_tags={"projeto": "backbone"})
        )
        antes = await _raw(memory, "SELECT id, embedding FROM wiki_chunks ORDER BY chunk_index")
        v2 = await memory.ingest_document(
            _doc(
                "contrato",
                "Contrato",
                CONTRATO,
                access_level=AccessLevel.EXECUTIVE,
                tags=["sigiloso"],
                metadata_tags={"fase": "renovacao"},
            )
        )
        assert v2.status is IngestStatus.METADATA_UPDATED and v2.version == 2
        assert v2.chunk_count == v1.chunk_count

        depois = await _raw(
            memory, "SELECT id, embedding, metadata_tags FROM wiki_chunks ORDER BY chunk_index"
        )
        assert [r["id"] for r in depois] == [r["id"] for r in antes]
        tags = depois[0]["metadata_tags"]
        assert tags["tags"] == ["sigiloso"] and tags["fase"] == "renovacao"
        assert "projeto" not in tags

        # A reclassificacao vale na hora: o operacional deixa de enxergar.
        assert await memory.search_wiki("contrato CT-2024/0187", MemoryScopeFilter()) == []

    @pytest.mark.asyncio
    async def test_banco_recusa_mudar_campo_auditado_sem_revisao(
        self, memory: NativeMemoryProvider
    ) -> None:
        await memory.ingest_document(
            _doc("estudo", "Estudo", ESTUDO, access_level=AccessLevel.EXECUTIVE)
        )
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await _raw(memory, "UPDATE wiki_documents SET access_level = 'OPERATIONAL'")

    @pytest.mark.asyncio
    async def test_falha_de_embedding_nao_deixa_documento_pela_metade(
        self, memory: NativeMemoryProvider
    ) -> None:
        class Quebrado(HashingEmbeddingProvider):
            async def embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise EmbeddingError("API fora do ar")

        memory.ingestion._embedder = Quebrado(DIM)
        memory.ingestion._settings = memory.settings.model_copy(
            update={"embedding_max_attempts": 1}
        )
        with pytest.raises(EmbeddingError):
            await memory.ingest_document(_doc("contrato", "Contrato", CONTRATO))
        assert await _raw(memory, "SELECT 1 FROM wiki_documents") == []
        assert await _raw(memory, "SELECT 1 FROM wiki_chunks") == []

    @pytest.mark.asyncio
    async def test_ingestoes_concorrentes_do_mesmo_documento_nao_duplicam(
        self, memory: NativeMemoryProvider
    ) -> None:
        resultados = await asyncio.gather(
            *(memory.ingest_document(_doc("contrato", "Contrato", CONTRATO)) for _ in range(5))
        )
        status = sorted(r.status.value for r in resultados)
        assert status == ["created", "duplicate", "duplicate", "duplicate", "duplicate"]
        assert len(await _raw(memory, "SELECT 1 FROM wiki_documents")) == 1
        assert len(await _raw(memory, "SELECT 1 FROM wiki_chunks")) == resultados[0].chunk_count

    @pytest.mark.asyncio
    async def test_submit_devolve_na_hora_e_processa_em_segundo_plano(
        self, memory: NativeMemoryProvider
    ) -> None:
        liberar = asyncio.Event()

        class Lento(HashingEmbeddingProvider):
            async def embed_batch(self, texts: list[str]) -> list[list[float]]:
                await liberar.wait()
                return await super().embed_batch(texts)

        memory.ingestion._embedder = Lento(DIM)
        job = memory.submit_document(_doc("playbook", "Playbook", PLAYBOOK))
        assert job.state is IngestJobState.QUEUED and job.result is None

        # Com a ingestao presa no embedding, o canal segue respondendo.
        await memory.append_history("s1", "user", "o canal nao travou")
        assert memory.get_ingest_job(job.job_id).state is IngestJobState.RUNNING  # type: ignore[union-attr]

        liberar.set()
        await memory.ingestion.join()
        pronto = memory.get_ingest_job(job.job_id)
        assert pronto is not None and pronto.state is IngestJobState.DONE
        assert pronto.result is not None and pronto.result.status is IngestStatus.CREATED

    @pytest.mark.asyncio
    async def test_aposentar_tira_da_busca_e_mantem_o_integro(
        self, wiki: NativeMemoryProvider
    ) -> None:
        assert await wiki.retire_document("playbook-incidentes-noc", actor="tomas")
        assert not await wiki.retire_document("playbook-incidentes-noc", actor="tomas")
        assert (
            await wiki.search_wiki("severidade de incidente", MemoryScopeFilter.for_user(ANA)) == []
        )
        doc = await _raw(
            wiki,
            "SELECT status, raw_content, version FROM wiki_documents "
            "WHERE slug = 'playbook-incidentes-noc'",
        )
        assert (doc[0]["status"], doc[0]["version"]) == ("retired", 2)
        assert doc[0]["raw_content"] == PLAYBOOK


# ---------------------------------------------------------------------------
# Tier 3 - Recuperacao hibrida e RBAC
# ---------------------------------------------------------------------------


class TestRetrieval:
    @pytest.mark.asyncio
    async def test_numero_de_contrato_chega_ao_topo_pela_perna_lexica(
        self, wiki: NativeMemoryProvider
    ) -> None:
        resultados = await wiki.search_wiki(
            "qual o valor mensal do contrato CT-2024/0187?", MemoryScopeFilter.for_user(CAIO)
        )
        assert resultados and resultados[0].slug == "contrato-transito-ip"
        assert "184.500,00" in resultados[0].content
        assert resultados[0].sparse_rank == 1
        assert resultados[0].heading_path == "Contrato de Transito IP > Valores"
        assert 0 < resultados[0].token_count <= 120

    @pytest.mark.asyncio
    async def test_score_e_exatamente_a_formula_do_rrf(self, wiki: NativeMemoryProvider) -> None:
        k = wiki.settings.rrf_k
        resposta = await wiki.search_wiki_detailed(
            "SLA de disponibilidade da rede neutra", MemoryScopeFilter.for_user(DORA), limit=8
        )
        assert resposta.degraded is None and len(resposta.results) > 2
        for r in resposta.results:
            esperado = sum(1 / (k + rank) for rank in (r.dense_rank, r.sparse_rank) if rank)
            assert r.rrf_score == pytest.approx(esperado, rel=1e-9)
        scores = [r.rrf_score for r in resposta.results]
        assert scores == sorted(scores, reverse=True)
        assert any(r.dense_rank and r.sparse_rank for r in resposta.results)
        assert resposta.token_total == sum(r.token_count for r in resposta.results)

    @pytest.mark.asyncio
    async def test_operacional_nunca_recebe_chunk_executivo(
        self, wiki: NativeMemoryProvider
    ) -> None:
        pergunta = "estudo estrategico de aquisicao de provedor regional sinergias de OPEX"
        for usuario in (ANA, CAIO):
            resultados = await wiki.search_wiki(
                pergunta, MemoryScopeFilter.for_user(usuario), limit=20
            )
            assert all(r.access_level is AccessLevel.OPERATIONAL for r in resultados)
            assert all(r.slug != "estudo-aquisicao-oeste" for r in resultados)

        executiva = await wiki.search_wiki(pergunta, MemoryScopeFilter.for_user(DORA), limit=20)
        assert executiva[0].slug == "estudo-aquisicao-oeste"
        assert executiva[0].access_level is AccessLevel.EXECUTIVE

    @pytest.mark.asyncio
    async def test_departamento_e_pre_filtro(self, wiki: NativeMemoryProvider) -> None:
        ana = await wiki.search_wiki(
            "contrato CT-2024/0187", MemoryScopeFilter.for_user(ANA), limit=20
        )
        assert all(r.slug != "contrato-transito-ip" for r in ana)

        # Executiva de outra area: ve a empresa toda e o proprio departamento...
        dora = await wiki.search_wiki(
            "contrato CT-2024/0187", MemoryScopeFilter.for_user(DORA), limit=20
        )
        assert all(r.slug != "contrato-transito-ip" for r in dora)
        # ...e so atravessa departamentos pedindo explicitamente.
        cross = await wiki.search_wiki(
            "contrato CT-2024/0187",
            MemoryScopeFilter.for_user(DORA, cross_department=True),
            limit=20,
        )
        assert cross[0].slug == "contrato-transito-ip"

    @pytest.mark.asyncio
    async def test_escalada_de_escopo_para_no_guard_antes_do_sql(
        self, wiki: NativeMemoryProvider
    ) -> None:
        forjado = MemoryScopeFilter(user_role=AccessLevel.EXECUTIVE, cross_department=True)
        with pytest.raises(ValidationError, match="declara papel EXECUTIVE"):
            await wiki.search_wiki("estudo de aquisicao", forjado, user_context=ANA)
        with pytest.raises(ValidationError):
            await wiki.get_document("estudo-aquisicao-oeste", forjado, user_context=ANA)
        with pytest.raises(ValidationError):
            await wiki.browse_index(forjado, user_context=ANA)

    @pytest.mark.asyncio
    async def test_filtros_de_tipo_tag_e_subarvore_do_indice(
        self, wiki: NativeMemoryProvider
    ) -> None:
        todos = MemoryScopeFilter.for_user(DORA, cross_department=True)
        pergunta = "rede neutra"

        por_tipo = await wiki.search_wiki(
            pergunta, todos.model_copy(update={"doc_types": ["deep_research"]}), limit=20
        )
        assert por_tipo and {r.doc_type for r in por_tipo} == {"deep_research"}

        por_tag = await wiki.search_wiki(
            pergunta, todos.model_copy(update={"tags": {"tags": ["rede_neutra"]}}), limit=20
        )
        assert por_tag and {r.slug for r in por_tag} == {"playbook-incidentes-noc"}

        por_chave = await wiki.search_wiki(
            pergunta, todos.model_copy(update={"tags": {"contrato": "CT-2024/0187"}}), limit=20
        )
        assert por_chave and {r.slug for r in por_chave} == {"contrato-transito-ip"}

        por_indice = await wiki.search_wiki(
            pergunta, todos.model_copy(update={"index_path": "FINANCAS_OPEX"}), limit=20
        )
        assert por_indice and {r.slug for r in por_indice} == {"contrato-transito-ip"}

    @pytest.mark.asyncio
    async def test_embedding_fora_do_ar_degrada_para_lexica_e_avisa(
        self, wiki: NativeMemoryProvider
    ) -> None:
        class ForaDoAr(HashingEmbeddingProvider):
            async def embed(self, text: str) -> list[float]:
                raise EmbeddingError("timeout na API")

        wiki.retrieval._embedder = ForaDoAr(DIM)
        resposta = await wiki.search_wiki_detailed(
            "contrato CT-2024/0187", MemoryScopeFilter.for_user(CAIO)
        )
        assert resposta.degraded and resposta.degraded.startswith("dense_unavailable")
        assert resposta.results[0].slug == "contrato-transito-ip"
        assert all(r.dense_rank is None and r.sparse_rank for r in resposta.results)

    @pytest.mark.asyncio
    async def test_so_stopwords_cai_na_perna_densa_sem_erro(
        self, wiki: NativeMemoryProvider
    ) -> None:
        resultados = await wiki.search_wiki("o de a", MemoryScopeFilter.for_user(ANA))
        assert resultados and all(r.sparse_rank is None for r in resultados)
        assert await wiki.search_wiki("   ", MemoryScopeFilter.for_user(ANA)) == []

    @pytest.mark.asyncio
    async def test_limit_e_orcamento_de_tokens(self, wiki: NativeMemoryProvider) -> None:
        escopo = MemoryScopeFilter.for_user(DORA, cross_department=True)
        assert len(await wiki.search_wiki("rede neutra", escopo, limit=2)) == 2
        apertado = await wiki.search_wiki_detailed("rede neutra", escopo, limit=8, max_tokens=1)
        assert len(apertado.results) == 1
        folgado = await wiki.search_wiki_detailed("rede neutra", escopo, limit=8, max_tokens=150)
        assert 1 <= len(folgado.results) < 8 and folgado.token_total <= 150

    @pytest.mark.asyncio
    async def test_lexical_mode_all_exige_todos_os_termos(self, schema: str) -> None:
        estrito = await NativeMemoryProvider.connect(
            _settings(schema, lexical_mode="all"), embedder=HashingEmbeddingProvider(DIM)
        )
        try:
            await estrito.ingest_document(_doc("contrato", "Contrato", CONTRATO))
            resultados = await estrito.search_wiki(
                "valor mensal do contrato CT-2024/0187 em marte", MemoryScopeFilter()
            )
            assert all(r.sparse_rank is None for r in resultados)
        finally:
            await estrito.close()

    @pytest.mark.asyncio
    async def test_empate_de_rrf_vai_para_a_evidencia_lexica(self, schema: str) -> None:
        """Uma lista por perna, sem intersecao: 1/(k+1) contra 1/(k+1)."""

        class DensaAdversaria(HashingEmbeddingProvider):
            async def embed(self, text: str) -> list[float]:
                # A consulta cai em cima do playbook, longe do contrato.
                return await super().embed("incidente fila NOC severidade plantao backbone SEV1")

        provider = await NativeMemoryProvider.connect(
            _settings(schema, candidate_multiplier=1, min_candidates=1),
            embedder=DensaAdversaria(DIM),
        )
        try:
            await provider.ingest_document(_doc("playbook", "Playbook", PLAYBOOK))
            await provider.ingest_document(_doc("contrato", "Contrato", CONTRATO))
            duas = await provider.search_wiki("CT-2024/0187", MemoryScopeFilter(), limit=2)
            assert {(r.slug, r.dense_rank, r.sparse_rank) for r in duas} == {
                ("contrato", None, 1),
                ("playbook", 1, None),
            }
            assert duas[0].rrf_score == duas[1].rrf_score
            assert duas[0].slug == "contrato"
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_perna_lexica_tem_teto_rigido_de_linhas(self, schema: str) -> None:
        provider = await NativeMemoryProvider.connect(
            _settings(schema, sparse_candidate_cap=1), embedder=HashingEmbeddingProvider(DIM)
        )
        try:
            await provider.ingest_document(_doc("contrato", "Contrato", CONTRATO))
            # "contrato" aparece em varios chunks: passa do teto, cai no AND,
            # e o AND tambem e cortado em 1 linha.
            resultados = await provider.search_wiki("contrato", MemoryScopeFilter(), limit=10)
            assert len(resultados) >= 3
            assert sum(1 for r in resultados if r.sparse_rank is not None) == 1
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_frequencia_velha_em_cache_nao_esconde_documento_novo(
        self, wiki: NativeMemoryProvider
    ) -> None:
        escopo = MemoryScopeFilter.for_user(ANA)
        assert all(
            r.sparse_rank is None for r in await wiki.search_wiki("circuito CKT-99123", escopo)
        )
        velho = dict(wiki.retrieval._df_cache)
        # CKT-99123 vira {ckt, -99123}; nenhum dos dois existe ainda no corpus.
        assert velho["-99123"][0] == 0 and velho["ckt"][0] == 0

        await wiki.ingest_document(
            _doc(
                "circuito",
                "Circuito",
                "# Circuito\n\nO circuito CKT-99123 atende Blumenau.",
                department_scope=["noc"],
            )
        )
        assert wiki.retrieval._df_cache == {}  # ingestao invalida o cache
        wiki.retrieval._df_cache.update(velho)  # ...mas simule outro processo, com cache velho
        achado = await wiki.search_wiki("circuito CKT-99123", escopo)
        assert achado[0].slug == "circuito" and achado[0].sparse_rank == 1

    @pytest.mark.asyncio
    async def test_consulta_hostil_nao_quebra_o_tsquery(self, wiki: NativeMemoryProvider) -> None:
        hostil = """it's 'aspas' \\ & | ! :* <-> (contrato) \\' "x" ; DROP TABLE wiki_chunks; --"""
        resultados = await wiki.search_wiki(hostil, MemoryScopeFilter.for_user(CAIO))
        assert resultados and resultados[0].slug == "contrato-transito-ip"
        assert await _raw(wiki, "SELECT count(*) FROM wiki_chunks")

    @pytest.mark.asyncio
    async def test_busca_repetida_na_mesma_conexao_e_estavel(self, schema: str) -> None:
        """Da 6a execucao em diante o Postgres tentaria o plano generico."""
        provider = await NativeMemoryProvider.connect(
            _settings(schema, pool_min_size=1, pool_max_size=1),
            embedder=HashingEmbeddingProvider(DIM),
        )
        try:
            await provider.ingest_document(_doc("contrato", "Contrato", CONTRATO))
            rodadas = [
                [
                    (r.chunk_id, r.rrf_score)
                    for r in await provider.search_wiki(
                        "valor do contrato CT-2024/0187", MemoryScopeFilter()
                    )
                ]
                for _ in range(9)
            ]
            assert rodadas[0] and all(rodada == rodadas[0] for rodada in rodadas)
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_documento_integro_respeita_o_mesmo_escopo(
        self, wiki: NativeMemoryProvider
    ) -> None:
        doc = await wiki.get_document("estudo-aquisicao-oeste", MemoryScopeFilter.for_user(DORA))
        assert doc.raw_content == ESTUDO and doc.access_level is AccessLevel.EXECUTIVE
        assert (await wiki.get_document(doc.id, MemoryScopeFilter.for_user(DORA))).slug == doc.slug
        # Sigiloso e inexistente devolvem o MESMO erro.
        for alvo in ("estudo-aquisicao-oeste", "nao-existe"):
            with pytest.raises(DocumentNotFoundError):
                await wiki.get_document(alvo, MemoryScopeFilter.for_user(ANA))


# ---------------------------------------------------------------------------
# Meta-indice
# ---------------------------------------------------------------------------


class TestMetaIndex:
    @pytest.mark.asyncio
    async def test_navegacao_recursiva_do_dominio_ao_documento(
        self, wiki: NativeMemoryProvider
    ) -> None:
        await wiki.upsert_index_node("ENGENHARIA", title="Engenharia", description="Rede e SDLC")
        escopo = MemoryScopeFilter.for_user(ANA)

        dominios = await wiki.browse_index(escopo, depth=0)
        assert [(n.key, n.level, n.children) for n in dominios] == [("ENGENHARIA", 0, [])]
        assert dominios[0].title == "Engenharia" and dominios[0].document_count == 1

        arvore = await wiki.browse_index(escopo, depth=2)
        tema = arvore[0].children[0]
        assert (tema.path, tema.level, tema.document_count) == ("ENGENHARIA/PLAYBOOK_NOC", 1, 1)
        ponteiro = tema.children[0]
        assert ponteiro.level == 2 and ponteiro.document is not None
        assert ponteiro.document.slug == "playbook-incidentes-noc"

        a_partir_do_tema = await wiki.browse_index(escopo, "ENGENHARIA/PLAYBOOK_NOC")
        assert [n.path for n in a_partir_do_tema] == ["ENGENHARIA/PLAYBOOK_NOC"]
        assert len(a_partir_do_tema[0].children) == 1

    @pytest.mark.asyncio
    async def test_indice_nao_vaza_nome_de_projeto_nem_documento(
        self, wiki: NativeMemoryProvider
    ) -> None:
        # No criado na ingestao herdou EXECUTIVE do documento.
        ana = await wiki.browse_index(MemoryScopeFilter.for_user(ANA), depth=2)
        assert [n.key for n in ana] == ["ENGENHARIA"]
        assert await wiki.browse_index(MemoryScopeFilter.for_user(ANA), "PESQUISA") == []
        assert (
            await wiki.browse_index(MemoryScopeFilter.for_user(ANA), "PESQUISA/AQUISICAO_OESTE")
            == []
        )

        # Departamento tambem conta: cada area so ve a taxonomia onde tem
        # documento visivel. O NOC nao le FINANCAS_OPEX, nem o financeiro ENGENHARIA.
        caio = await wiki.browse_index(MemoryScopeFilter.for_user(CAIO), depth=2)
        assert [n.key for n in caio] == ["FINANCAS_OPEX"]
        assert caio[0].children[0].children[0].document.slug == "contrato-transito-ip"  # type: ignore[union-attr]
        assert await wiki.browse_index(MemoryScopeFilter.for_user(CAIO), "ENGENHARIA") == []

        dora = await wiki.browse_index(
            MemoryScopeFilter.for_user(DORA, cross_department=True), depth=2
        )
        assert [n.key for n in dora] == ["ENGENHARIA", "FINANCAS_OPEX", "PESQUISA"]
        assert dora[2].children[0].children[0].document.slug == "estudo-aquisicao-oeste"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_hierarquia_e_garantida_pelo_banco(self, wiki: NativeMemoryProvider) -> None:
        dominio = (await _raw(wiki, "SELECT id FROM meta_index WHERE path = 'ENGENHARIA'"))[0]
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await _raw(
                wiki,
                "INSERT INTO meta_index (parent_id, level, key, path, document_id) "
                "SELECT $1, 2, 'atalho', '', id FROM wiki_documents LIMIT 1",
                dominio["id"],
            )

    @pytest.mark.asyncio
    async def test_vincular_e_desvincular_documento(self, wiki: NativeMemoryProvider) -> None:
        await wiki.link_document("ENGENHARIA/REDE_NEUTRA", "playbook-incidentes-noc")
        await wiki.link_document("ENGENHARIA/REDE_NEUTRA", "playbook-incidentes-noc")
        arvore = await wiki.browse_index(MemoryScopeFilter.for_user(ANA), "ENGENHARIA", depth=2)
        assert [t.key for t in arvore[0].children] == ["PLAYBOOK_NOC", "REDE_NEUTRA"]
        assert arvore[0].document_count == 2

        assert await wiki.meta_index.unlink_document(
            "ENGENHARIA/REDE_NEUTRA", "playbook-incidentes-noc"
        )
        with pytest.raises(LookupError):
            await wiki.link_document("ENGENHARIA/REDE_NEUTRA", "nao-existe")


# ---------------------------------------------------------------------------
# Montagem JIT do turno
# ---------------------------------------------------------------------------


class TestTurnContext:
    @pytest.mark.asyncio
    async def test_contexto_do_turno_nao_cresce_com_a_idade_da_conversa(
        self, wiki: NativeMemoryProvider
    ) -> None:
        await wiki.set_core_block(
            "assistant", "persona", "Voce e o assistente.", created_by="tomas"
        )
        await wiki.set_core_block(
            "assistant", "system_invariants", "Respeite o RBAC.", created_by="tomas"
        )

        async def conversar(turnos: int) -> int:
            for n in range(turnos):
                await wiki.append_history("s1", "user", f"pergunta numero {n} sobre incidentes")
                await wiki.append_history("s1", "assistant", f"resposta numero {n} do agente")
            contexto = await wiki.assemble_turn_context(
                "assistant", "s1", ANA, "como escalar um incidente SEV1?", wiki_limit=3
            )
            assert {m.turn_index for m in contexto.history} <= set(range(1, 10_000))
            assert len({m.turn_index for m in contexto.history}) <= 6
            assert contexto.wiki is not None and contexto.wiki.results
            assert contexto.wiki.results[0].slug == "playbook-incidentes-noc"
            assert set(contexto.tokens) == {"core", "recall", "wiki"}
            return contexto.token_total

        com_10 = await conversar(10)
        com_210 = await conversar(200)
        assert abs(com_210 - com_10) <= 12

    @pytest.mark.asyncio
    async def test_sem_query_nao_ha_busca_na_wiki(self, wiki: NativeMemoryProvider) -> None:
        await wiki.set_core_block(
            "assistant", "persona", "Voce e o assistente.", created_by="tomas"
        )
        await wiki.set_core_block(
            "assistant", "system_invariants", "Respeite o RBAC.", created_by="tomas"
        )
        contexto = await wiki.assemble_turn_context("assistant", "s1", ANA)
        assert contexto.wiki is None and contexto.tokens["wiki"] == 0
