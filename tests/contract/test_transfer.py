"""Export e import entre backends (F3.8, prova 8.4).

O ciclo `memory -> memory` roda em qualquer maquina, sem servico externo.
Quando ha banco de teste, o ciclo `pgvector -> memory` tambem roda.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from orkmind.core.config import OrkMindConfig
from orkmind.core.injection import compute_content_hash
from orkmind.core.models import MemoryEntry
from orkmind.store.base import MemoryStore
from orkmind.store.factory import create_store
from orkmind.store.transfer import (
    CHAVE_MANIFESTO,
    LABEL_PRE_IMPORT,
    ConflitoDeImportacaoError,
    ExportacaoInvalidaError,
    PerdaNaoAceitaError,
    exportar,
    importar,
    ler_dump,
)
from tests.contract.backends import obter_backend


def entrada(conteudo: str, **kwargs) -> MemoryEntry:
    base = {
        "content": conteudo,
        "collection": "fact",
        "content_hash": compute_content_hash(conteudo),
    }
    base.update(kwargs)
    return MemoryEntry(**base)  # type: ignore[arg-type]


CORPUS = [
    entrada("regra obrigatoria do deploy", collection="rule", mandatory=True,
            priority="critical", tags={"skill": ["deploy"]}),
    entrada("outra regra obrigatoria", collection="rule", mandatory=True,
            tags={"skill": ["git"]}),
    entrada("um fato comum", collection="fact", tags={"domain": ["infra"]}),
    entrada("uma preferencia", collection="preference"),
    entrada("conteudo com risco declarado", collection="content",
            injection_risk=True),
]


@pytest_asyncio.fixture
async def destino() -> MemoryStore:
    """Backend memory limpo, sempre disponivel."""
    store = create_store(OrkMindConfig(store_backend="memory"))
    await store.initialize()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def origem() -> MemoryStore:
    store = create_store(OrkMindConfig(store_backend="memory"))
    await store.initialize()
    for entry in CORPUS:
        await store.store(entry.model_copy(deep=True))
    yield store
    await store.close()


class TestExport:
    @pytest.mark.asyncio
    async def test_manifesto_e_a_primeira_linha(
        self, origem: MemoryStore, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo)
        primeira = json.loads(arquivo.read_text(encoding="utf-8").splitlines()[0])
        assert primeira[CHAVE_MANIFESTO] == 1
        assert primeira["source_backend"] == "memory"
        assert primeira["entry_count"] == len(CORPUS)
        assert primeira["mandatory_count"] == 2
        assert "rule" in primeira["collections"]

    @pytest.mark.asyncio
    async def test_exporta_todas_as_entries(
        self, origem: MemoryStore, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "dump.jsonl"
        relatorio = await exportar(origem, arquivo)
        assert relatorio.entry_count == len(CORPUS)
        assert relatorio.mandatory_count == 2

    @pytest.mark.asyncio
    async def test_exporta_entry_com_injection_risk(
        self, origem: MemoryStore, tmp_path: Path
    ) -> None:
        """Exportador que perde memoria em silencio seria pior que nenhum."""
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo)
        _, registros = ler_dump(arquivo)
        conteudos = [
            r["dados"]["content"] for r in registros if r["tipo"] == "entry"
        ]
        assert "conteudo com risco declarado" in conteudos

    @pytest.mark.asyncio
    async def test_export_nao_altera_a_origem(
        self, origem: MemoryStore, tmp_path: Path
    ) -> None:
        antes = await origem.count()
        hashes_antes = {e.content_hash for e in CORPUS}
        relatorio = await exportar(origem, tmp_path / "dump.jsonl")
        assert relatorio.origem_intacta is True
        assert await origem.count() == antes
        for h in hashes_antes:
            assert await origem.find_by_content_hash(str(h)) is not None

    @pytest.mark.asyncio
    async def test_filtra_por_colecao(
        self, origem: MemoryStore, tmp_path: Path
    ) -> None:
        relatorio = await exportar(origem, tmp_path / "dump.jsonl", collection="rule")
        assert relatorio.entry_count == 2
        assert relatorio.collections == ["rule"]

    @pytest.mark.asyncio
    async def test_preserva_o_embedding(
        self, destino: MemoryStore, tmp_path: Path
    ) -> None:
        vetor = [0.25] + [0.0] * 1023
        await destino.store(entrada("com vetor", embedding=vetor))
        arquivo = tmp_path / "dump.jsonl"
        await exportar(destino, arquivo)
        _, registros = ler_dump(arquivo)
        dados = [r["dados"] for r in registros if r["tipo"] == "entry"][0]
        assert dados["embedding"][0] == pytest.approx(0.25)
        assert len(dados["embedding"]) == 1024


class TestManifestoInvalido:
    def test_arquivo_inexistente(self, tmp_path: Path) -> None:
        with pytest.raises(ExportacaoInvalidaError, match="nao existe"):
            ler_dump(tmp_path / "nao-existe.jsonl")

    def test_arquivo_vazio(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "vazio.jsonl"
        arquivo.write_text("", encoding="utf-8")
        with pytest.raises(ExportacaoInvalidaError, match="vazio"):
            ler_dump(arquivo)

    def test_primeira_linha_sem_manifesto(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "sem_manifesto.jsonl"
        arquivo.write_text('{"tipo": "entry", "dados": {}}\n', encoding="utf-8")
        with pytest.raises(ExportacaoInvalidaError, match="manifesto"):
            ler_dump(arquivo)

    def test_versao_de_formato_desconhecida(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "futuro.jsonl"
        arquivo.write_text(
            json.dumps({CHAVE_MANIFESTO: 99}) + "\n", encoding="utf-8"
        )
        with pytest.raises(ExportacaoInvalidaError, match="versao"):
            ler_dump(arquivo)

    def test_primeira_linha_nao_e_json(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "lixo.jsonl"
        arquivo.write_text("isto nao e json\n", encoding="utf-8")
        with pytest.raises(ExportacaoInvalidaError):
            ler_dump(arquivo)


class TestCicloMemoryParaMemory:
    @pytest.mark.asyncio
    async def test_ciclo_completo_reconcilia(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "dump.jsonl"
        rel_export = await exportar(origem, arquivo)
        rel_import = await importar(destino, arquivo)

        assert rel_import.importadas == rel_export.entry_count
        assert rel_import.mandatory_reconciliado is True
        assert rel_import.mandatory_origem == 2
        assert rel_import.mandatory_destino == 2
        assert rel_import.hashes_faltando == []
        assert await destino.count() == await origem.count()

    @pytest.mark.asyncio
    async def test_contagem_por_colecao_bate(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo)
        rel = await importar(destino, arquivo)
        assert rel.por_colecao["rule"] == 2
        assert rel.por_colecao["fact"] == 1

    @pytest.mark.asyncio
    async def test_snapshot_pre_import_existe(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo)
        rel = await importar(destino, arquivo)
        assert rel.snapshot_pre_import
        labels = [s["label"] for s in await destino.snapshot_log()]
        assert LABEL_PRE_IMPORT in labels

    @pytest.mark.asyncio
    async def test_origem_continua_intacta_depois_do_import(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        """Migracao nunca remove a origem: nao ha flag de mover."""
        antes = await origem.count()
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo)
        await importar(destino, arquivo)
        assert await origem.count() == antes


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_nao_escreve_nada(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo)
        rel = await importar(destino, arquivo, dry_run=True)
        assert rel.dry_run is True
        assert rel.lidas == len(CORPUS)
        assert rel.importadas == 0
        assert rel.snapshot_pre_import == ""
        assert await destino.count() == 0

    @pytest.mark.asyncio
    async def test_dry_run_relata_colisoes_e_sem_embedding(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo)
        await importar(destino, arquivo)
        rel = await importar(destino, arquivo, dry_run=True)
        assert rel.conflitos_por_id == len(CORPUS)
        assert rel.conflitos_por_hash == len(CORPUS)
        assert rel.sem_embedding == len(CORPUS)


class TestConflitos:
    @pytest.mark.asyncio
    async def test_skip_e_o_default_e_nao_sobrescreve(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo)
        await importar(destino, arquivo)
        rel = await importar(destino, arquivo)
        assert rel.importadas == 0
        assert rel.mandatory_reconciliado is True
        assert await destino.count() == len(CORPUS)

    @pytest.mark.asyncio
    async def test_on_conflict_fail_levanta(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo)
        await importar(destino, arquivo)
        with pytest.raises(ConflitoDeImportacaoError):
            await importar(destino, arquivo, on_conflict="fail")

    @pytest.mark.asyncio
    async def test_import_nunca_apaga_o_que_ja_havia(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        propria = entrada("memoria propria do destino", collection="learning")
        await destino.store(propria)
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo)
        await importar(destino, arquivo)
        assert await destino.retrieve(propria.id) is not None
        assert await destino.count() == len(CORPUS) + 1


class TestPerdaDeclarada:
    @pytest.mark.asyncio
    async def test_versoes_no_dump_exigem_accept_loss(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        alvo = CORPUS[2]
        await origem.update(
            alvo.id, entrada("um fato comum, revisado", collection="fact")
        )
        arquivo = tmp_path / "dump.jsonl"
        rel_export = await exportar(origem, arquivo, include_versions=True)
        assert rel_export.version_count >= 1

        with pytest.raises(PerdaNaoAceitaError, match="accept-loss"):
            await importar(destino, arquivo)

        rel = await importar(destino, arquivo, accept_loss=True)
        assert rel.versoes_descartadas >= 1
        assert any("versoes anteriores" in p for p in rel.perdas)

    @pytest.mark.asyncio
    async def test_dry_run_nao_exige_accept_loss(
        self, origem: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        await origem.update(
            CORPUS[2].id, entrada("revisado de novo", collection="fact")
        )
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem, arquivo, include_versions=True)
        rel = await importar(destino, arquivo, dry_run=True)
        assert rel.perdas


class TestCicloPgvectorParaMemory:
    """Ciclo entre backends de verdade, quando ha banco de teste."""

    @pytest_asyncio.fixture
    async def origem_pgvector(self) -> MemoryStore:
        backend = obter_backend("pgvector")
        if not backend.disponivel:
            pytest.skip(backend.motivo_skip)
        store = backend.construir()
        await store.initialize()
        backend.limpar(store)
        for entry in CORPUS:
            await store.store(entry.model_copy(deep=True))
        yield store
        backend.limpar(store)
        await store.close()

    @pytest.mark.asyncio
    async def test_pgvector_para_memory(
        self, origem_pgvector: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        arquivo = tmp_path / "dump.jsonl"
        rel_export = await exportar(origem_pgvector, arquivo)
        assert rel_export.backend_origem == "pgvector"
        assert rel_export.entry_count == len(CORPUS)
        assert rel_export.origem_intacta is True

        rel = await importar(destino, arquivo)
        assert rel.backend_origem == "pgvector"
        assert rel.backend_destino == "memory"
        assert rel.importadas == len(CORPUS)
        assert rel.mandatory_reconciliado is True
        assert rel.hashes_faltando == []

    @pytest.mark.asyncio
    async def test_origem_pgvector_fica_intacta(
        self, origem_pgvector: MemoryStore, destino: MemoryStore, tmp_path: Path
    ) -> None:
        antes = await origem_pgvector.count()
        arquivo = tmp_path / "dump.jsonl"
        await exportar(origem_pgvector, arquivo)
        await importar(destino, arquivo)
        assert await origem_pgvector.count() == antes
        for entry in CORPUS:
            assert await origem_pgvector.find_by_content_hash(
                str(entry.content_hash)
            ) is not None
