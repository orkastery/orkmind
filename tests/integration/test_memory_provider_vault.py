"""A teia: vault, wikilinks, backlinks e tags semanticas, contra banco real.

O que estes testes defendem e a promessa de substituir o Obsidian: documento
inteiro guardado (nao ponteiro), links bidirecionais, link para nota que ainda
nao existe, tags hierarquicas e formatos de arquivo de verdade.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
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
from orkmind.memory_provider.services.extraction import (  # noqa: E402
    UnsupportedFormatError,
    extract_bytes,
)
from orkmind.memory_provider.services.notes import parse_note, slugify  # noqa: E402

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

VAULT = {
    "Rede Neutra.md": """---
title: Rede Neutra
tags: [rede/backbone, estrategia]
---

# Rede Neutra

Atendemos provedores parceiros. Ver [[Playbook de Incidentes]] para SEV1.
O contrato principal esta em [[Contrato de Transito IP]].
Ainda falta escrever [[Mapa de Enlaces]]. #infraestrutura
""",
    "Playbook de Incidentes.md": """---
tags: [rede/operacao]
---

# Playbook de Incidentes

SEV1 aciona o plantao. Contexto em [[Rede Neutra|nossa rede]].
Tambem falta [[Mapa de Enlaces]].
""",
    "Contrato de Transito IP.md": """# Contrato de Transito IP

Contrato CT-2024/0187. Vale para a [[Rede Neutra]]. #financeiro
""",
    "sub/Nota Solta.md": "# Nota Solta\n\nSem links nem tags.\n",
}


@pytest_asyncio.fixture
async def memory() -> AsyncIterator[NativeMemoryProvider]:
    if not is_test_database(DATABASE_URL):
        raise RuntimeError(f"{TEST_DB_ENV} nao parece banco de testes")
    schema = f"mp_vault_{uuid4().hex[:10]}"
    provider = await NativeMemoryProvider.connect(
        MemoryProviderSettings(
            database_url=DATABASE_URL,
            db_schema=schema,
            embedding_dim=DIM,
            chunking=ChunkingOptions(max_tokens=120, min_tokens=8),
        ),
        embedder=HashingEmbeddingProvider(DIM),
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


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    for nome, conteudo in VAULT.items():
        destino = tmp_path / nome
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(conteudo, encoding="utf-8")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (tmp_path / "imagem.png").write_bytes(b"\x89PNG\r\n")
    return tmp_path


class TestVault:
    @pytest.mark.asyncio
    async def test_ingere_o_vault_inteiro_ignorando_o_que_nao_e_documento(
        self, memory: NativeMemoryProvider, vault: Path
    ) -> None:
        resumo = await memory.ingest_vault(vault)
        assert resumo["ingeridos"] == 4, resumo
        assert resumo["falhas"] == []
        assert resumo["ignorados"] == 1  # o png; `.obsidian/` nem e visitado

        escopo = MemoryScopeFilter.for_user(ANA)
        doc = await memory.get_document("rede-neutra", escopo)
        # Documento INTEIRO, frontmatter incluso: nao e ponteiro nem resumo.
        assert doc.raw_content == VAULT["Rede Neutra.md"]
        assert doc.metadata["extra"]["source_format"] == "md"

    @pytest.mark.asyncio
    async def test_frontmatter_nao_entra_no_indice_vetorial(
        self, memory: NativeMemoryProvider, vault: Path
    ) -> None:
        await memory.ingest_vault(vault)
        chunks = await memory._pool.fetch(
            "SELECT c.content FROM wiki_chunks c JOIN wiki_documents d ON d.id = c.document_id "
            "WHERE d.slug = 'rede-neutra'"
        )
        juntos = "\n".join(r["content"] for r in chunks)
        assert "Atendemos provedores parceiros" in juntos
        assert "tags: [rede/backbone" not in juntos, "frontmatter nao deve virar embedding"

    @pytest.mark.asyncio
    async def test_backlinks_com_contexto(self, memory: NativeMemoryProvider, vault: Path) -> None:
        await memory.ingest_vault(vault)
        escopo = MemoryScopeFilter.for_user(ANA)
        entrantes = await memory.backlinks("rede-neutra", escopo)
        origens = {b["slug"] for b in entrantes}
        assert origens == {"playbook-de-incidentes", "contrato-de-transito-ip"}
        playbook = next(b for b in entrantes if b["slug"] == "playbook-de-incidentes")
        assert playbook["alias"] == "nossa rede"
        assert "plantao" in playbook["context"], "backlink sem contexto obriga a abrir a origem"

    @pytest.mark.asyncio
    async def test_link_para_nota_inexistente_resolve_quando_ela_nasce(
        self, memory: NativeMemoryProvider, vault: Path
    ) -> None:
        await memory.ingest_vault(vault)
        escopo = MemoryScopeFilter.for_user(ANA)

        pendentes = await memory.unresolved_links(escopo)
        mapa = {p["target_slug"]: p["citacoes"] for p in pendentes}
        assert mapa.get("mapa-de-enlaces") == 2, "duas notas citam o que falta escrever"

        # A nota fantasma vira real.
        await memory.ingest_document(
            DocumentIngestRequest(
                slug="mapa-de-enlaces",
                title="Mapa de Enlaces",
                doc_type="note",
                raw_content="# Mapa de Enlaces\n\nEnlaces do anel metropolitano.",
                access_level=AccessLevel.OPERATIONAL,
            )
        )
        assert not [
            p
            for p in await memory.unresolved_links(escopo)
            if p["target_slug"] == "mapa-de-enlaces"
        ]
        entrantes = await memory.backlinks("mapa-de-enlaces", escopo)
        assert {b["slug"] for b in entrantes} == {"rede-neutra", "playbook-de-incidentes"}

    @pytest.mark.asyncio
    async def test_saida_marca_resolvido_e_pendente(
        self, memory: NativeMemoryProvider, vault: Path
    ) -> None:
        await memory.ingest_vault(vault)
        saindo = await memory.outgoing_links("rede-neutra", MemoryScopeFilter.for_user(ANA))
        por_alvo = {x["target_slug"]: x for x in saindo}
        assert por_alvo["playbook-de-incidentes"]["resolvido"] is True
        assert por_alvo["mapa-de-enlaces"]["resolvido"] is False

    @pytest.mark.asyncio
    async def test_backlink_nao_vaza_a_existencia_de_documento_sigiloso(
        self, memory: NativeMemoryProvider, vault: Path
    ) -> None:
        await memory.ingest_vault(vault)
        await memory.ingest_document(
            DocumentIngestRequest(
                slug="estudo-reservado",
                title="Estudo Reservado",
                doc_type="deep_research",
                raw_content="# Estudo Reservado\n\nAvaliacao sobre a [[Rede Neutra]].",
                access_level=AccessLevel.EXECUTIVE,
            )
        )
        operacional = await memory.backlinks("rede-neutra", MemoryScopeFilter.for_user(ANA))
        assert all(b["slug"] != "estudo-reservado" for b in operacional), (
            "o conteudo estaria protegido, mas a RELACAO vazaria"
        )
        executiva = await memory.backlinks("rede-neutra", MemoryScopeFilter.for_user(DORA))
        assert any(b["slug"] == "estudo-reservado" for b in executiva)

    @pytest.mark.asyncio
    async def test_tags_com_hierarquia_e_contagem(
        self, memory: NativeMemoryProvider, vault: Path
    ) -> None:
        await memory.ingest_vault(vault)
        escopo = MemoryScopeFilter.for_user(ANA)
        contagem = {t["tag"]: t["documentos"] for t in await memory.list_tags(escopo)}
        assert contagem["rede/backbone"] == 1 and contagem["rede/operacao"] == 1
        assert contagem["infraestrutura"] == 1 and contagem["financeiro"] == 1

        # A tag-mae traz as filhas, sem ninguem ter declarado `rede`.
        sob_rede = {d["slug"] for d in await memory.documents_by_tag("rede", escopo)}
        assert sob_rede == {"rede-neutra", "playbook-de-incidentes"}
        assert {d["slug"] for d in await memory.documents_by_tag("rede/backbone", escopo)} == {
            "rede-neutra"
        }

    @pytest.mark.asyncio
    async def test_reingestao_troca_a_teia(self, memory: NativeMemoryProvider, vault: Path) -> None:
        await memory.ingest_vault(vault)
        (vault / "Contrato de Transito IP.md").write_text(
            "# Contrato de Transito IP\n\nAgora aponta para [[Nota Solta]]. #juridico",
            encoding="utf-8",
        )
        await memory.ingest_file(vault / "Contrato de Transito IP.md")
        escopo = MemoryScopeFilter.for_user(ANA)
        alvos = {
            x["target_slug"] for x in await memory.outgoing_links("contrato-de-transito-ip", escopo)
        }
        assert alvos == {"nota-solta"}, "aresta velha tem de sumir"
        assert not [
            b
            for b in await memory.backlinks("rede-neutra", escopo)
            if b["slug"] == "contrato-de-transito-ip"
        ]
        tags = {t["tag"] for t in await memory.list_tags(escopo)}
        assert "juridico" in tags and "financeiro" not in tags


class TestFormatos:
    @pytest.mark.asyncio
    async def test_csv_vira_tabela_pesquisavel(
        self, memory: NativeMemoryProvider, tmp_path: Path
    ) -> None:
        planilha = tmp_path / "contratos.csv"
        planilha.write_text(
            "contrato,fornecedor,valor\nCT-2024/0187,Operadora X,184500\n"
            "CT-2023/0442,Fibra Y,67900\n",
            encoding="utf-8",
        )
        resultado = await memory.ingest_file(planilha, doc_type="opex_analysis")
        assert resultado.chunk_count >= 1

        escopo = MemoryScopeFilter.for_user(ANA)
        doc = await memory.get_document("contratos", escopo)
        assert "| CT-2024/0187 | Operadora X | 184500 |" in doc.raw_content
        achados = await memory.search_wiki("CT-2023/0442", escopo)
        assert achados and achados[0].slug == "contratos"

    @pytest.mark.asyncio
    async def test_txt_e_guardado_integralmente(
        self, memory: NativeMemoryProvider, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "ata.txt"
        conteudo = "Ata da reuniao.\n\nDecidimos manter o IPCA como indice.\n"
        arquivo.write_text(conteudo, encoding="utf-8")
        await memory.ingest_file(arquivo, doc_type="meeting_minutes")
        doc = await memory.get_document("ata", MemoryScopeFilter.for_user(ANA))
        assert doc.raw_content == conteudo and doc.content_format == "text"

    @pytest.mark.asyncio
    async def test_arquivo_ruim_nao_derruba_o_vault(
        self, memory: NativeMemoryProvider, tmp_path: Path
    ) -> None:
        (tmp_path / "boa.md").write_text("# Boa\n\nConteudo valido.", encoding="utf-8")
        (tmp_path / "quebrada.pdf").write_bytes(b"isto nao e um PDF")
        resumo = await memory.ingest_vault(tmp_path)
        assert resumo["ingeridos"] == 1
        assert len(resumo["falhas"]) == 1 and "quebrada.pdf" in resumo["falhas"][0]["arquivo"]

    def test_formato_desconhecido_diz_o_que_aceita(self) -> None:
        with pytest.raises(UnsupportedFormatError, match="Suportados"):
            extract_bytes(b"x", "planilha.xlsx")


class TestParserDeNota:
    def test_codigo_nao_vira_link_nem_tag(self) -> None:
        nota = parse_note(
            "# T\n\n[[Real]] e #real\n\n```sh\n# comentario\ngrep '[[falso]]'\n```\n\n"
            "Inline `[[nem-esse]]` e `#nemessa`."
        )
        assert [x.target for x in nota.links] == ["real"]
        assert nota.tags == ["real"]

    def test_slug_casa_nome_de_nota_com_acento_e_caminho(self) -> None:
        assert slugify("Manutenção Preventiva.md") == "manutencao-preventiva"
        assert slugify("pasta/Sub Nota") == "sub-nota"
        assert slugify("[[Rede Neutra]]".strip("[]")) == "rede-neutra"

    def test_transclusao_ancora_e_alias(self) -> None:
        nota = parse_note("![[Tabela]] [[Doc#Secao]] [[Outro|apelido]] [nome](interno.md)")
        por_tipo = {x.kind: x for x in nota.links}
        assert por_tipo["embed"].target == "tabela"
        assert por_tipo["markdown"].target == "interno"
        anchor = next(x for x in nota.links if x.anchor)
        assert anchor.anchor == "#Secao"
        alias = next(x for x in nota.links if x.alias)
        assert alias.alias == "apelido"

    def test_link_externo_nao_entra_na_teia(self) -> None:
        nota = parse_note("[site](https://exemplo.com) e [ancora](#secao)")
        assert nota.links == []
