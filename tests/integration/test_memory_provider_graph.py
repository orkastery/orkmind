"""O grafo local: vizinhanca por arestas autoradas, sob o escopo de quem olha.

O que estes testes defendem, alem do desenho funcionar:

- um documento invisivel nao serve de PONTE. Proteger so o conteudo do no do
  meio e insuficiente: se a caminhada atravessa-lo, a existencia da relacao
  (e do no do outro lado) vaza assim mesmo;
- a mesma base desenha grafos diferentes conforme o papel;
- o que foi citado e nunca escrito aparece como fantasma, nao some.
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
    HashingEmbeddingProvider,
    MemoryProviderSettings,
    MemoryScopeFilter,
    NativeMemoryProvider,
    UserContext,
)

TEST_DB_ENV = "ORKMIND_PROVIDER_TEST_DATABASE_URL"
DATABASE_URL = os.environ.get(TEST_DB_ENV, "")
DIM = 64

pytestmark = [
    pytest.mark.integration,
    pytest.mark.memory_provider,
    pytest.mark.skipif(not DATABASE_URL, reason=f"Defina {TEST_DB_ENV}"),
]

ANA = UserContext(user_id="ana", role=AccessLevel.OPERATIONAL, departments=["noc"])
DORA = UserContext(user_id="dora", role=AccessLevel.EXECUTIVE, departments=["noc"])

# A corrente que importa: ponta-a -> ponte-sigilosa -> ponta-c.
# Para ANA a ponte nao existe, entao `ponta-c` tem de ficar inalcancavel -
# mesmo sendo, ela propria, um documento que ANA pode ler de frente.
CORPUS = [
    (
        "ponta-a",
        "Ponta A",
        AccessLevel.OPERATIONAL,
        "# Ponta A\n\nDepende de [[Ponte Sigilosa]]. Falta escrever [[Mapa de Enlaces]].\n",
    ),
    (
        "ponte-sigilosa",
        "Ponte Sigilosa",
        AccessLevel.EXECUTIVE,
        "# Ponte Sigilosa\n\nAvaliacao reservada que leva a [[Ponta C]].\n",
    ),
    ("ponta-c", "Ponta C", AccessLevel.OPERATIONAL, "# Ponta C\n\nDocumento comum do NOC.\n"),
    (
        "vizinho-direto",
        "Vizinho Direto",
        AccessLevel.OPERATIONAL,
        "# Vizinho Direto\n\nCita a [[Ponta A]] para justificar o plantao.\n",
    ),
]


@pytest_asyncio.fixture
async def memory() -> AsyncIterator[NativeMemoryProvider]:
    if not is_test_database(DATABASE_URL):
        raise RuntimeError(f"{TEST_DB_ENV} nao parece banco de testes")
    schema = f"mp_graph_{uuid4().hex[:10]}"
    provider = await NativeMemoryProvider.connect(
        MemoryProviderSettings(
            database_url=DATABASE_URL,
            db_schema=schema,
            embedding_dim=DIM,
            chunking=ChunkingOptions(max_tokens=120, min_tokens=8),
        ),
        embedder=HashingEmbeddingProvider(DIM),
    )
    for slug, titulo, nivel, conteudo in CORPUS:
        await provider.ingest_document(
            DocumentIngestRequest(
                slug=slug,
                title=titulo,
                doc_type="nota",
                raw_content=conteudo,
                access_level=nivel,
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


def slugs(resultado: dict) -> set[str]:
    return {n["slug"] for n in resultado["nos"]}


class TestVizinhanca:
    @pytest.mark.asyncio
    async def test_um_salto_traz_as_duas_direcoes(self, memory: NativeMemoryProvider) -> None:
        """Vizinhanca e mao dupla: quem A cita e quem cita A."""
        r = await memory.neighborhood("ponta-a", MemoryScopeFilter.for_user(DORA), depth=1)
        assert r["origem"] == "ponta-a"
        # `vizinho-direto` so aparece se a caminhada andar tambem contra a seta.
        assert slugs(r) == {"ponta-a", "ponte-sigilosa", "vizinho-direto"}
        assert {n["slug"]: n["salto"] for n in r["nos"]}["ponta-a"] == 0

    @pytest.mark.asyncio
    async def test_zero_saltos_e_so_o_documento(self, memory: NativeMemoryProvider) -> None:
        r = await memory.neighborhood("ponta-a", MemoryScopeFilter.for_user(DORA), depth=0)
        assert slugs(r) == {"ponta-a"}

    @pytest.mark.asyncio
    async def test_documento_invisivel_nao_serve_de_ponte(
        self, memory: NativeMemoryProvider
    ) -> None:
        """O teste central: sem a ponte, o outro lado nao pode aparecer.

        `ponta-c` e OPERATIONAL - ANA le o documento de frente. Mas o unico
        caminho ate ele passa por um EXECUTIVE. Se a caminhada atravessasse o
        no invisivel, o desenho entregaria que existe relacao entre os dois.
        """
        ana = await memory.neighborhood("ponta-a", MemoryScopeFilter.for_user(ANA), depth=3)
        assert "ponte-sigilosa" not in slugs(ana), "o no sigiloso nao pode aparecer"
        assert "ponta-c" not in slugs(ana), "atravessou o no invisivel e vazou a relacao"
        assert slugs(ana) == {"ponta-a", "vizinho-direto"}

        # Mesma base, outro papel: a corrente inteira.
        dora = await memory.neighborhood("ponta-a", MemoryScopeFilter.for_user(DORA), depth=3)
        assert {"ponta-a", "ponte-sigilosa", "ponta-c", "vizinho-direto"} == slugs(dora)
        assert {n["slug"]: n["salto"] for n in dora["nos"]}["ponta-c"] == 2

    @pytest.mark.asyncio
    async def test_aresta_so_existe_com_as_duas_pontas_visiveis(
        self, memory: NativeMemoryProvider
    ) -> None:
        ana = await memory.neighborhood("ponta-a", MemoryScopeFilter.for_user(ANA), depth=3)
        tocados = {e["origem"] for e in ana["arestas"]} | {
            e["destino"] for e in ana["arestas"] if e["existe"]
        }
        assert "ponte-sigilosa" not in tocados

    @pytest.mark.asyncio
    async def test_fantasma_aparece_marcado(self, memory: NativeMemoryProvider) -> None:
        """Citado e nunca escrito nao some: vira no tracejado, a pauta do vault."""
        r = await memory.neighborhood("ponta-a", MemoryScopeFilter.for_user(ANA), depth=1)
        fantasmas = [e for e in r["arestas"] if not e["existe"]]
        assert [(e["origem"], e["destino"]) for e in fantasmas] == [("ponta-a", "mapa-de-enlaces")]

    @pytest.mark.asyncio
    async def test_fantasma_pode_ser_desligado(self, memory: NativeMemoryProvider) -> None:
        r = await memory.neighborhood(
            "ponta-a", MemoryScopeFilter.for_user(ANA), depth=1, include_ghosts=False
        )
        assert all(e["existe"] for e in r["arestas"])

    @pytest.mark.asyncio
    async def test_invisivel_e_inexistente_dao_a_mesma_resposta(
        self, memory: NativeMemoryProvider
    ) -> None:
        """Distinguir os dois ja entregaria a existencia do documento sigiloso."""
        escopo = MemoryScopeFilter.for_user(ANA)
        sigiloso = await memory.neighborhood("ponte-sigilosa", escopo)
        inexistente = await memory.neighborhood("nunca-existiu", escopo)
        assert (
            sigiloso
            == inexistente
            == {
                "origem": None,
                "nos": [],
                "arestas": [],
                "truncado": False,
            }
        )

    @pytest.mark.asyncio
    async def test_teto_de_nos_avisa_que_cortou(self, memory: NativeMemoryProvider) -> None:
        """Truncar em silencio faria a tela mentir sobre o tamanho da vizinhanca."""
        r = await memory.neighborhood("ponta-a", MemoryScopeFilter.for_user(DORA), depth=3, limit=2)
        assert len(r["nos"]) == 2
        assert r["truncado"] is True

        inteiro = await memory.neighborhood("ponta-a", MemoryScopeFilter.for_user(DORA), depth=3)
        assert inteiro["truncado"] is False

    @pytest.mark.asyncio
    async def test_profundidade_tem_teto(self, memory: NativeMemoryProvider) -> None:
        """Pedir 99 saltos nao pode virar varredura do acervo inteiro."""
        from orkmind.memory_provider.services.graph import MAX_DEPTH

        r = await memory.neighborhood("ponta-a", MemoryScopeFilter.for_user(DORA), depth=99)
        no_teto = await memory.neighborhood(
            "ponta-a", MemoryScopeFilter.for_user(DORA), depth=MAX_DEPTH
        )
        assert slugs(r) == slugs(no_teto)
