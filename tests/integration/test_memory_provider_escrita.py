"""Escrita em nome de uma pessoa: so grava o que ela conseguiria ler.

A ingestao de sistema (vault, fila, CLI) nao tem dono e continua como sempre.
Quando a escrita vem de uma pessoa - a web, um canal - o escopo dela entra
como `writer`, e o que estes testes defendem e:

- classificacao acima do papel, ou em departamento que ela nao le, e recusada
  ANTES de fatiar e de chamar embedding;
- slug de documento que ela nao le nao e sobrescrito, nem rebaixado de nivel
  no caminho, nem mesmo se o documento estiver aposentado;
- a deduplicacao por conteudo nao atravessa o escopo: nao entrega o slug de
  um documento sigiloso e nao deixa a pessoa sem o proprio documento;
- `ingest_bytes` registra a origem que a pessoa enviou, nao um caminho do
  servidor;
- a sugestao de `[[link]]` so oferece o que o escopo ve, e o texto sugerido
  resolve para o documento sugerido.
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
    ClassificationOutOfReachError,
    DocumentIngestRequest,
    HashingEmbeddingProvider,
    IngestStatus,
    MemoryProviderSettings,
    MemoryScopeFilter,
    NativeMemoryProvider,
    SlugUnavailableError,
    UserContext,
    WriteOutOfScopeError,
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
BIA = UserContext(user_id="bia", role=AccessLevel.EXECUTIVE, departments=["noc"])

ESTUDO = "# Estudo Reservado\n\nAvaliacao sigilosa do contrato de transito.\n"
PLAYBOOK = "# Playbook de Incidentes\n\nTodo incidente recebe severidade.\n"
FINANCEIRO = "# Orcamento do Financeiro\n\nPlanilha de custos do trimestre.\n"


class _EmbedderContado(HashingEmbeddingProvider):
    """Conta chamadas: recusa tem de acontecer antes de gastar embedding."""

    def __init__(self, dim: int) -> None:
        super().__init__(dim)
        self.chamadas = 0

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:  # type: ignore[override]
        self.chamadas += 1
        return await super().embed_batch(texts)


def escopo(user: UserContext) -> MemoryScopeFilter:
    return MemoryScopeFilter.for_user(user)


def pedido(
    slug: str,
    titulo: str,
    conteudo: str,
    nivel: AccessLevel = AccessLevel.OPERATIONAL,
    departamentos: list[str] | None = None,
) -> DocumentIngestRequest:
    return DocumentIngestRequest(
        slug=slug,
        title=titulo,
        doc_type="nota",
        raw_content=conteudo,
        access_level=nivel,
        department_scope=departamentos or [],
    )


@pytest_asyncio.fixture
async def memory() -> AsyncIterator[NativeMemoryProvider]:
    if not is_test_database(DATABASE_URL):
        raise RuntimeError(f"{TEST_DB_ENV} nao parece banco de testes")
    schema = f"mp_escrita_{uuid4().hex[:10]}"
    provider = await NativeMemoryProvider.connect(
        MemoryProviderSettings(
            database_url=DATABASE_URL,
            db_schema=schema,
            embedding_dim=DIM,
            chunking=ChunkingOptions(max_tokens=120, min_tokens=8),
        ),
        embedder=_EmbedderContado(DIM),
    )
    await provider.ingest_document(
        pedido("estudo-reservado", "Estudo Reservado", ESTUDO, AccessLevel.EXECUTIVE, ["noc"])
    )
    await provider.ingest_document(
        pedido("playbook-de-incidentes", "Playbook de Incidentes", PLAYBOOK, departamentos=["noc"])
    )
    await provider.ingest_document(
        pedido(
            "orcamento-do-financeiro", "Orcamento do Financeiro", FINANCEIRO, departamentos=["fin"]
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


async def classificacao(memory: NativeMemoryProvider, slug: str) -> tuple[str, list[str], int]:
    doc = await memory.get_document(
        slug, MemoryScopeFilter(user_role=AccessLevel.SYSTEM_ADMIN, cross_department=True)
    )
    return doc.access_level.value, list(doc.department_scope), doc.version


class TestClassificacaoDoQueSeGrava:
    @pytest.mark.asyncio
    async def test_nivel_acima_do_papel_e_recusado_antes_do_embedding(
        self, memory: NativeMemoryProvider
    ) -> None:
        embedder = memory.ingestion._embedder  # type: ignore[attr-defined]
        antes = embedder.chamadas
        with pytest.raises(ClassificationOutOfReachError):
            await memory.ingest_document(
                pedido(
                    "nota-da-ana", "Nota da Ana", "# Nota da Ana\n\nTexto.\n", AccessLevel.EXECUTIVE
                ),
                writer=escopo(ANA),
            )
        assert embedder.chamadas == antes

    @pytest.mark.asyncio
    async def test_departamento_que_ela_nao_le_e_recusado(
        self, memory: NativeMemoryProvider
    ) -> None:
        with pytest.raises(ClassificationOutOfReachError):
            await memory.ingest_document(
                pedido("nota-fin", "Nota Fin", "# Nota Fin\n\nTexto.\n", departamentos=["fin"]),
                writer=escopo(ANA),
            )

    @pytest.mark.asyncio
    async def test_dentro_do_alcance_grava_e_ela_le_de_volta(
        self, memory: NativeMemoryProvider
    ) -> None:
        r = await memory.ingest_document(
            pedido(
                "nota-da-ana", "Nota da Ana", "# Nota da Ana\n\nTexto.\n", departamentos=["noc"]
            ),
            writer=escopo(ANA),
        )
        assert r.status is IngestStatus.CREATED
        doc = await memory.get_document("nota-da-ana", escopo(ANA))
        assert doc.department_scope == ["NOC"]

    @pytest.mark.asyncio
    async def test_empresa_toda_esta_no_alcance_de_qualquer_papel(
        self, memory: NativeMemoryProvider
    ) -> None:
        r = await memory.ingest_document(
            pedido("aviso-geral", "Aviso Geral", "# Aviso Geral\n\nPara todos.\n"),
            writer=escopo(ANA),
        )
        assert r.status is IngestStatus.CREATED


class TestSlugDeOutroAlcance:
    @pytest.mark.asyncio
    async def test_nao_sobrescreve_nem_rebaixa_documento_sigiloso(
        self, memory: NativeMemoryProvider
    ) -> None:
        with pytest.raises(SlugUnavailableError) as erro:
            await memory.ingest_document(
                pedido("estudo-reservado", "Estudo Reservado", "# Outro texto\n\nNada a ver.\n"),
                writer=escopo(ANA),
            )
        # A recusa diz so que o nome esta em uso: nada sobre o documento.
        assert "EXECUTIVE" not in str(erro.value)
        assert "Estudo" not in str(erro.value)
        assert await classificacao(memory, "estudo-reservado") == ("EXECUTIVE", ["NOC"], 1)

    @pytest.mark.asyncio
    async def test_aposentado_de_outro_nivel_continua_protegido(
        self, memory: NativeMemoryProvider
    ) -> None:
        assert await memory.retire_document("estudo-reservado", actor="teste")
        with pytest.raises(SlugUnavailableError):
            await memory.ingest_document(
                pedido("estudo-reservado", "Estudo Reservado", "# Reuso\n\nTexto novo.\n"),
                writer=escopo(ANA),
            )

    @pytest.mark.asyncio
    async def test_quem_le_atualiza_e_ganha_nova_revisao(
        self, memory: NativeMemoryProvider
    ) -> None:
        r = await memory.ingest_document(
            pedido(
                "playbook-de-incidentes",
                "Playbook de Incidentes",
                PLAYBOOK + "\nSEV1 aciona o plantao.\n",
                departamentos=["noc"],
            ),
            writer=escopo(ANA),
        )
        assert r.status is IngestStatus.UPDATED
        assert (await classificacao(memory, "playbook-de-incidentes"))[2] == 2

    @pytest.mark.asyncio
    async def test_as_duas_recusas_sao_a_mesma_familia(self) -> None:
        """Quem so quer saber "foi recusado por escopo" captura uma classe so."""
        assert issubclass(SlugUnavailableError, WriteOutOfScopeError)
        assert issubclass(ClassificationOutOfReachError, WriteOutOfScopeError)

    @pytest.mark.asyncio
    async def test_sem_writer_a_ingestao_de_sistema_segue_igual(
        self, memory: NativeMemoryProvider
    ) -> None:
        r = await memory.ingest_document(
            pedido(
                "estudo-reservado",
                "Estudo Reservado",
                ESTUDO + "\nRevisado.\n",
                AccessLevel.EXECUTIVE,
                ["noc"],
            )
        )
        assert r.status is IngestStatus.UPDATED


class TestDuplicataNaoAtravessaEscopo:
    @pytest.mark.asyncio
    async def test_conteudo_igual_a_sigiloso_vira_documento_proprio(
        self, memory: NativeMemoryProvider
    ) -> None:
        r = await memory.ingest_document(
            pedido("copia-da-ana", "Estudo Reservado", ESTUDO, departamentos=["noc"]),
            writer=escopo(ANA),
        )
        # Nem aponta para o sigiloso, nem deixa a Ana sem o documento dela.
        assert r.status is IngestStatus.CREATED
        assert r.slug == "copia-da-ana"
        assert (await memory.get_document("copia-da-ana", escopo(ANA))).slug == "copia-da-ana"

    @pytest.mark.asyncio
    async def test_conteudo_igual_dentro_do_alcance_continua_duplicata(
        self, memory: NativeMemoryProvider
    ) -> None:
        r = await memory.ingest_document(
            pedido("outro-playbook", "Playbook de Incidentes", PLAYBOOK, departamentos=["noc"]),
            writer=escopo(ANA),
        )
        assert r.status is IngestStatus.DUPLICATE
        assert r.slug == "playbook-de-incidentes"


class TestIngestBytes:
    @pytest.mark.asyncio
    async def test_registra_a_origem_enviada_e_nao_um_caminho_do_servidor(
        self, memory: NativeMemoryProvider
    ) -> None:
        r = await memory.ingest_bytes(
            "# Ata da Reuniao\n\nDecidimos revisar o [[Playbook de Incidentes]].\n".encode(),
            "atas/Ata da Reuniao.md",
            department_scope=["noc"],
            ingested_by="ana",
            source="upload:atas/Ata da Reuniao.md",
            writer=escopo(ANA),
        )
        assert r.status is IngestStatus.CREATED
        assert r.slug == "ata-da-reuniao"
        doc = await memory.get_document("ata-da-reuniao", escopo(ANA))
        assert doc.title == "Ata da Reuniao"
        assert doc.source_url_or_path == "upload:atas/Ata da Reuniao.md"
        citado = await memory.backlinks("playbook-de-incidentes", escopo(ANA))
        assert [c["slug"] for c in citado] == ["ata-da-reuniao"]

    @pytest.mark.asyncio
    async def test_csv_vira_documento(self, memory: NativeMemoryProvider) -> None:
        r = await memory.ingest_bytes(
            b"circuito,custo\nA,10\nB,20\n", "custos.csv", writer=escopo(BIA)
        )
        assert r.status is IngestStatus.CREATED
        doc = await memory.get_document("custos", escopo(BIA))
        assert "circuito" in doc.raw_content

    @pytest.mark.asyncio
    async def test_formato_nao_suportado_falha_sem_gravar(
        self, memory: NativeMemoryProvider
    ) -> None:
        from orkmind.memory_provider.services.extraction import UnsupportedFormatError

        with pytest.raises(UnsupportedFormatError):
            await memory.ingest_bytes(b"\x00\x01", "imagem.png", writer=escopo(ANA))

    @pytest.mark.asyncio
    async def test_ingest_file_continua_idempotente(
        self, memory: NativeMemoryProvider, tmp_path
    ) -> None:
        """Mesma metadata de antes: reingerir o mesmo arquivo nao cria revisao."""
        arquivo = tmp_path / "Nota Local.md"
        arquivo.write_text("# Nota Local\n\nTexto.\n", encoding="utf-8")
        primeira = await memory.ingest_file(arquivo)
        segunda = await memory.ingest_file(arquivo)
        assert primeira.status is IngestStatus.CREATED
        assert segunda.status is IngestStatus.DUPLICATE
        doc = await memory.get_document("nota-local", escopo(ANA))
        assert doc.source_url_or_path == str(arquivo.resolve())


class TestSugestaoDeLink:
    @pytest.mark.asyncio
    async def test_so_sugere_o_que_o_escopo_ve(self, memory: NativeMemoryProvider) -> None:
        ana = {s["slug"] for s in await memory.suggest_documents("", escopo(ANA))}
        bia = {s["slug"] for s in await memory.suggest_documents("", escopo(BIA))}
        assert ana == {"playbook-de-incidentes"}
        assert bia == {"playbook-de-incidentes", "estudo-reservado"}

    @pytest.mark.asyncio
    async def test_comeco_do_nome_vem_primeiro(self, memory: NativeMemoryProvider) -> None:
        await memory.ingest_document(
            pedido("guia-do-playbook", "Guia do Playbook", "# Guia do Playbook\n\nTexto.\n")
        )
        r = await memory.suggest_documents("Playbook", escopo(ANA))
        assert [s["slug"] for s in r] == ["playbook-de-incidentes", "guia-do-playbook"]

    @pytest.mark.asyncio
    async def test_texto_do_link_resolve_para_o_documento(
        self, memory: NativeMemoryProvider
    ) -> None:
        await memory.ingest_document(
            pedido(
                "sla-2026", "Acordo de Nivel de Servico", "# Acordo de Nivel de Servico\n\nTexto.\n"
            )
        )
        por_slug = {s["slug"]: s for s in await memory.suggest_documents("", escopo(ANA))}
        # Titulo que vira o proprio slug serve de texto; senao, o slug.
        assert por_slug["playbook-de-incidentes"]["link"] == "Playbook de Incidentes"
        assert por_slug["sla-2026"]["link"] == "sla-2026"

    @pytest.mark.asyncio
    async def test_curinga_digitado_e_literal(self, memory: NativeMemoryProvider) -> None:
        assert await memory.suggest_documents("%", escopo(ANA)) == []
        assert await memory.suggest_documents("_", escopo(ANA)) == []
