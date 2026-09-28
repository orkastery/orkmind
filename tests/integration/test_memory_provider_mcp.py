"""Servidor MCP do Memory Provider, contra PostgreSQL + pgvector reais.

Exige ORKMIND_PROVIDER_TEST_DATABASE_URL, com as mesmas guardas do restante
da suite. Cada teste cria e derruba so o proprio schema.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio

from tests.conftest import is_test_database

asyncpg = pytest.importorskip("asyncpg", reason="extra orkmind[memory-provider] nao instalado")

from orkmind.memory_provider import (  # noqa: E402
    AccessLevel,
    ChunkingOptions,
    DocumentIngestRequest,
    DocumentNotFoundError,
    HashingEmbeddingProvider,
    MemoryProviderSettings,
    MemoryScopeFilter,
    NativeMemoryProvider,
    UserContext,
)
from orkmind.memory_provider.mcp import tools  # noqa: E402
from orkmind.memory_provider.mcp.server import build_tools, build_user_context  # noqa: E402

TEST_DB_ENV = "ORKMIND_PROVIDER_TEST_DATABASE_URL"
DATABASE_URL = os.environ.get(TEST_DB_ENV, "")
DIM = 64

pytestmark = [
    pytest.mark.integration,
    pytest.mark.memory_provider,
    pytest.mark.skipif(not DATABASE_URL, reason=f"Defina {TEST_DB_ENV}"),
]

ANA = UserContext(user_id="ana", role=AccessLevel.OPERATIONAL, departments=["noc"], channel="mcp")
DORA = UserContext(user_id="dora", role=AccessLevel.EXECUTIVE, departments=["noc"], channel="mcp")

PLAYBOOK = """# Playbook de Incidentes

## Triagem
Todo incidente do NOC recebe severidade em ate dez minutos. Impacto em parceiros e SEV1.

## Escalonamento
SEV1 aciona o plantao de engenharia de backbone imediatamente.
"""
ESTUDO = "# Estudo Sigiloso\n\n## Tese\nAvaliacao reservada de aquisicao. Nao divulgar.\n"


@pytest_asyncio.fixture
async def memory() -> AsyncIterator[NativeMemoryProvider]:
    if not is_test_database(DATABASE_URL):
        raise RuntimeError(f"{TEST_DB_ENV} nao parece banco de testes")
    schema = f"mp_mcp_{uuid4().hex[:10]}"
    provider = await NativeMemoryProvider.connect(
        MemoryProviderSettings(
            database_url=DATABASE_URL,
            db_schema=schema,
            embedding_dim=DIM,
            chunking=ChunkingOptions(max_tokens=120, min_tokens=8),
        ),
        embedder=HashingEmbeddingProvider(DIM),
    )
    await provider.ingest_document(
        DocumentIngestRequest(
            slug="playbook-incidentes",
            title="Playbook de Incidentes",
            doc_type="playbook",
            raw_content=PLAYBOOK,
            access_level=AccessLevel.OPERATIONAL,
            department_scope=["noc"],
            index_paths=["ENGENHARIA/PLAYBOOK_NOC"],
        )
    )
    await provider.ingest_document(
        DocumentIngestRequest(
            slug="estudo-sigiloso",
            title="Estudo Sigiloso",
            doc_type="deep_research",
            raw_content=ESTUDO,
            access_level=AccessLevel.EXECUTIVE,
            index_paths=["PESQUISA/AQUISICAO"],
        )
    )
    try:
        yield provider
    finally:
        await provider.close()
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await conn.close()


class TestSuperficieDeTools:
    def test_nenhuma_tool_aceita_papel_ou_departamento(self) -> None:
        """A garantia central: identidade vem do ambiente, nunca do modelo.

        Se um dia alguem adicionar `role` ao schema de uma tool, o RBAC inteiro
        vira enfeite - basta o modelo pedir EXECUTIVE.
        """
        proibidos = {
            "role",
            "user_role",
            "access_level",
            "departments",
            "cross_department",
            "user_id",
            "papel",
        }
        for tool in build_tools():
            campos = set(tool.input_schema.get("properties", {}))
            assert not (campos & proibidos), f"{tool.name} expoe identidade: {campos & proibidos}"
            # additionalProperties=False impede contrabandear o campo mesmo assim.
            assert tool.input_schema.get("additionalProperties") is False, tool.name

    def test_somente_leitura_esconde_a_escrita(self) -> None:
        completo = {t.name for t in build_tools()}
        restrito = {t.name for t in build_tools(readonly=True)}
        assert "memory_remember" in completo
        assert completo - restrito == {"memory_remember"}

    def test_contrato_do_openclaw_esta_presente(self) -> None:
        # Nome fora do contrato ja custou uma investigacao inteira.
        nomes = {t.name for t in build_tools()}
        assert {"memory_search", "memory_get"} <= nomes

    def test_identidade_do_ambiente_e_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("USER_ID", "ROLE", "DEPARTMENTS", "DISPLAY_NAME"):
            monkeypatch.delenv(f"ORKMIND_PROVIDER_MCP_{var}", raising=False)
        padrao = build_user_context()
        assert padrao.role is AccessLevel.OPERATIONAL and padrao.channel == "mcp"

        monkeypatch.setenv("ORKMIND_PROVIDER_MCP_ROLE", "executive")
        monkeypatch.setenv("ORKMIND_PROVIDER_MCP_DEPARTMENTS", " noc , financeiro ")
        elevado = build_user_context()
        assert elevado.role is AccessLevel.EXECUTIVE
        assert elevado.departments == ["NOC", "FINANCEIRO"]

        monkeypatch.setenv("ORKMIND_PROVIDER_MCP_ROLE", "ROOT")
        with pytest.raises(ValueError, match="ROLE invalido"):
            build_user_context()


class TestTools:
    @pytest.mark.asyncio
    async def test_busca_devolve_trecho_e_a_perna_de_origem(
        self, memory: NativeMemoryProvider
    ) -> None:
        escopo = MemoryScopeFilter.for_user(ANA)
        saida = await tools.memory_search(memory, escopo, "como escalar um SEV1?")
        assert saida["resultados"], saida
        primeiro = saida["resultados"][0]
        assert primeiro["slug"] == "playbook-incidentes"
        assert primeiro["origem"] in ("semantica", "lexica", "semantica+lexica")
        assert saida["tokens"] == sum(r["tokens"] for r in saida["resultados"])
        assert "aviso" not in saida

    @pytest.mark.asyncio
    async def test_busca_nao_vaza_documento_acima_do_papel(
        self, memory: NativeMemoryProvider
    ) -> None:
        operacional = await tools.memory_search(
            memory, MemoryScopeFilter.for_user(ANA), "avaliacao reservada de aquisicao", limit=10
        )
        assert all(r["slug"] != "estudo-sigiloso" for r in operacional["resultados"])

        executiva = await tools.memory_search(
            memory, MemoryScopeFilter.for_user(DORA), "avaliacao reservada de aquisicao", limit=10
        )
        assert any(r["slug"] == "estudo-sigiloso" for r in executiva["resultados"])

    @pytest.mark.asyncio
    async def test_documento_sai_paginado_nunca_resumido(
        self, memory: NativeMemoryProvider
    ) -> None:
        escopo = MemoryScopeFilter.for_user(ANA)
        paginas: list[str] = []
        offset = 0
        while True:
            p = await tools.memory_get(
                memory, escopo, "playbook-incidentes", offset=offset, max_tokens=20
            )
            paginas.append(p["conteudo"])
            if "continua_em" not in p:
                break
            offset = p["continua_em"]
        assert len(paginas) > 2, "max_tokens=20 deveria paginar"
        # Uniao das paginas == documento original, byte a byte.
        assert "".join(paginas) == PLAYBOOK

    @pytest.mark.asyncio
    async def test_get_de_documento_sigiloso_erra_igual_a_inexistente(
        self, memory: NativeMemoryProvider
    ) -> None:
        escopo = MemoryScopeFilter.for_user(ANA)
        for alvo in ("estudo-sigiloso", "nao-existe"):
            with pytest.raises(DocumentNotFoundError):
                await tools.memory_get(memory, escopo, alvo)

    @pytest.mark.asyncio
    async def test_indice_mostra_so_o_que_a_identidade_ve(
        self, memory: NativeMemoryProvider
    ) -> None:
        ana = await tools.memory_index(memory, MemoryScopeFilter.for_user(ANA))
        caminhos = [n["caminho"] for n in ana["indice"]]
        assert caminhos == ["ENGENHARIA"]

        dora = await tools.memory_index(memory, MemoryScopeFilter.for_user(DORA))
        assert {n["caminho"] for n in dora["indice"]} == {"ENGENHARIA", "PESQUISA"}

    @pytest.mark.asyncio
    async def test_remember_grava_e_nao_deixa_classificar_acima_do_papel(
        self, memory: NativeMemoryProvider
    ) -> None:
        criado = await tools.memory_remember(
            memory,
            ANA,
            slug="decisao-rrf",
            title="Decisao sobre RRF",
            content="# Decisao\n\nAdotamos fusao RRF na busca.",
            doc_type="decision",
        )
        assert criado["situacao"] == "created" and criado["chunks"] >= 1

        repetido = await tools.memory_remember(
            memory,
            ANA,
            slug="decisao-rrf",
            title="Decisao sobre RRF",
            content="# Decisao\n\nAdotamos fusao RRF na busca.",
            doc_type="decision",
        )
        assert repetido["situacao"] == "duplicate"

        with pytest.raises(ValueError, match="nao pode classificar"):
            await tools.memory_remember(
                memory,
                ANA,
                slug="x",
                title="X",
                content="conteudo",
                access_level="EXECUTIVE",
            )

    @pytest.mark.asyncio
    async def test_remember_herda_o_escopo_de_quem_gravou(
        self, memory: NativeMemoryProvider
    ) -> None:
        await tools.memory_remember(
            memory,
            ANA,
            slug="nota-noc",
            title="Nota do NOC",
            content="# Nota\n\nRotina de plantao do centro de operacoes.",
        )
        doc = await memory.get_document("nota-noc", MemoryScopeFilter.for_user(ANA))
        assert doc.access_level is AccessLevel.OPERATIONAL
        assert doc.department_scope == ["NOC"]

    @pytest.mark.asyncio
    async def test_recall_encontra_turno_antigo(self, memory: NativeMemoryProvider) -> None:
        await memory.append_history(
            "s1", "user", "o circuito CKT-7781 caiu em Blumenau", user_id="ana"
        )
        for n in range(8):
            await memory.append_history("s1", "user", f"assunto diferente {n}", user_id="ana")

        recente = await memory.get_recent_history("s1")
        assert all("CKT-7781" not in m.content for m in recente)

        achado = await tools.memory_recall(memory, ANA, "CKT-7781", session_id="s1")
        assert achado["turnos"] and "CKT-7781" in achado["turnos"][0]["conteudo"]

    @pytest.mark.asyncio
    async def test_busca_sem_nada_no_escopo_orienta_o_agente(
        self, memory: NativeMemoryProvider
    ) -> None:
        # Pergunta sem sentido NAO volta vazia: sem `dense_max_distance` a perna
        # semantica sempre entrega os vizinhos mais proximos, por piores que
        # sejam. O caminho de resposta vazia e o do escopo que nao casa nada.
        absurdo = await tools.memory_search(
            memory, MemoryScopeFilter.for_user(ANA), "xilofone quantico submarino"
        )
        assert absurdo["resultados"], "a perna densa sempre devolve vizinhos"

        vazio = await tools.memory_search(
            memory,
            MemoryScopeFilter.for_user(ANA),
            "como escalar um SEV1?",
            doc_types=["tipo_que_nao_existe"],
        )
        assert vazio["resultados"] == [] and "memory_index" in vazio["dica"]
