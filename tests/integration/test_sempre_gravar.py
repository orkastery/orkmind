"""Integracao da solucao "sempre gravar": idempotencia real + drainer.

Exercita o caminho completo contra um Postgres de verdade: indice unico
parcial, lookup por content_hash, e o drainer levando um item de
pending/ ate done/ com o entry_id REAL devolvido pelo backend.

Rodar com:
    ORKMIND_TEST_DATABASE_URL=postgresql://.../orkmind_test \\
        .venv/bin/python -m pytest tests/integration/test_sempre_gravar.py -v
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import pytest_asyncio

from orkmind.core.injection import compute_content_hash
from orkmind.core.models import MemoryEntry
from orkmind.spool import (
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    ensure_spool_dirs,
    iter_items,
    stage_item,
)
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

REPO_ROOT = Path(__file__).resolve().parents[2]


def carregar_drainer():
    """Importa scripts/orkmind_drain.py, que nao e um pacote instalado."""
    caminho = REPO_ROOT / "scripts" / "orkmind_drain.py"
    spec = importlib.util.spec_from_file_location("orkmind_drain", caminho)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest_asyncio.fixture
async def adapter():
    assert_destrutivo_permitido(ALVO)
    pg = PostgresAdapter(DATABASE_URL)
    await pg.initialize()
    conn = await pg._get_conn()
    await conn.execute("DELETE FROM memories")
    yield pg
    await conn.execute("DELETE FROM memories")
    await pg.close()


def nova_entry(conteudo: str, collection: str = "content") -> MemoryEntry:
    return MemoryEntry(
        content=conteudo,
        collection=collection,  # type: ignore[arg-type]
        tags={"project": ["orkmind"]},
        source="agent",
        content_hash=compute_content_hash(conteudo),
    )


class TestLookupPorHash:
    @pytest.mark.asyncio
    async def test_encontra_entry_gravada(self, adapter: PostgresAdapter) -> None:
        entry = nova_entry("conteudo procuravel por hash")
        await adapter.store(entry)

        achada = await adapter.find_by_content_hash(entry.content_hash, "content")
        assert achada is not None
        assert achada.id == entry.id

    @pytest.mark.asyncio
    async def test_hash_desconhecido_devolve_none(self, adapter: PostgresAdapter) -> None:
        assert await adapter.find_by_content_hash("f" * 64, "content") is None

    @pytest.mark.asyncio
    async def test_respeita_a_colecao(self, adapter: PostgresAdapter) -> None:
        """A chave de dedupe e (colecao, hash): o par e que importa."""
        entry = nova_entry("mesmo texto", "content")
        await adapter.store(entry)

        assert await adapter.find_by_content_hash(entry.content_hash, "fact") is None
        assert await adapter.find_by_content_hash(entry.content_hash, "content") is not None

    @pytest.mark.asyncio
    async def test_sem_colecao_busca_em_todas(self, adapter: PostgresAdapter) -> None:
        entry = nova_entry("busca ampla", "fact")
        await adapter.store(entry)
        assert await adapter.find_by_content_hash(entry.content_hash) is not None

    @pytest.mark.asyncio
    async def test_hash_vazio_devolve_none(self, adapter: PostgresAdapter) -> None:
        assert await adapter.find_by_content_hash("") is None


class TestIndiceUnico:
    @pytest.mark.asyncio
    async def test_duplicata_e_barrada_pelo_banco(self, adapter: PostgresAdapter) -> None:
        """Idempotencia dura: o banco recusa o segundo INSERT igual."""
        conteudo = "conteudo que nao pode duplicar"
        await adapter.store(nova_entry(conteudo))

        with pytest.raises(Exception):
            await adapter.store(nova_entry(conteudo))

        conn = await adapter._get_conn()
        cur = await conn.execute(
            "SELECT count(*) AS n FROM memories WHERE content_hash = %s",
            (compute_content_hash(conteudo),),
        )
        assert (await cur.fetchone())["n"] == 1

    @pytest.mark.asyncio
    async def test_colecoes_diferentes_convivem(self, adapter: PostgresAdapter) -> None:
        conteudo = "mesmo texto em colecoes distintas"
        await adapter.store(nova_entry(conteudo, "content"))
        await adapter.store(nova_entry(conteudo, "fact"))

        conn = await adapter._get_conn()
        cur = await conn.execute(
            "SELECT count(*) AS n FROM memories WHERE content_hash = %s",
            (compute_content_hash(conteudo),),
        )
        assert (await cur.fetchone())["n"] == 2

    @pytest.mark.asyncio
    async def test_entries_sem_hash_nao_colidem(self, adapter: PostgresAdapter) -> None:
        """O indice e parcial: NULL nunca colide, entries legadas seguem."""
        for _ in range(3):
            e = MemoryEntry(content="legado", collection="content", content_hash=None)
            await adapter.store(e)

        conn = await adapter._get_conn()
        cur = await conn.execute(
            "SELECT count(*) AS n FROM memories WHERE content_hash IS NULL"
        )
        assert (await cur.fetchone())["n"] == 3

    @pytest.mark.asyncio
    async def test_indice_existe_no_banco(self, adapter: PostgresAdapter) -> None:
        conn = await adapter._get_conn()
        cur = await conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'memories' "
            "AND indexname = 'uq_memories_collection_content_hash'"
        )
        assert await cur.fetchone() is not None


class TestDrainerPontaAPonta:
    """O drainer levando itens de pending/ ate done/ com id real."""

    @pytest.fixture
    def spool(self, tmp_path: Path) -> Path:
        return ensure_spool_dirs(tmp_path / "spool")

    @pytest.fixture
    def drainer(self, spool: Path, monkeypatch: pytest.MonkeyPatch):
        """Drainer usando o CLI real, apontado para o banco de TESTE.

        O fallback CLI e exercitado de proposito: e o caminho que roda
        quando a API local nao esta no ar, que e o caso do backfill.
        """
        monkeypatch.setenv("ORKMIND_DATABASE_URL", DATABASE_URL)
        mod = carregar_drainer()
        cli = mod.CliBackend(REPO_ROOT / ".venv" / "bin" / "orkmind")
        if not cli.configured:
            pytest.skip("CLI orkmind nao encontrado no venv do repo")
        return mod.Drainer(root=spool, api=None, cli=cli)

    @pytest.mark.asyncio
    async def test_pending_vira_done_com_entry_id_real(
        self, adapter: PostgresAdapter, drainer, spool: Path
    ) -> None:
        conteudo = "relatorio drenado pelo teste de integracao"
        item = stage_item(content=conteudo, collection="content", root=spool)

        placar = drainer.drain_once()
        assert placar["created"] == 1

        # O item saiu de pending e chegou em done.
        assert list(iter_items(spool, STATE_PENDING)) == []
        finalizados = list(iter_items(spool, STATE_DONE))
        assert len(finalizados) == 1

        gravado = finalizados[0]
        assert gravado.item_id == item.item_id
        assert gravado.status == STATE_DONE
        assert gravado.entry_id, "entry_id real e obrigatorio em done/"

        # O id nao foi inventado: existe no banco, com o mesmo conteudo.
        no_banco = await adapter.retrieve(gravado.entry_id)
        assert no_banco is not None
        assert no_banco.content == conteudo
        assert no_banco.content_hash == compute_content_hash(conteudo)

    @pytest.mark.asyncio
    async def test_drenar_duas_vezes_nao_duplica(
        self, adapter: PostgresAdapter, drainer, spool: Path
    ) -> None:
        """O caminho idempotente: reestagiar e redrenar nao duplica."""
        conteudo = "conteudo drenado duas vezes"
        stage_item(content=conteudo, collection="content", root=spool)
        primeiro = drainer.drain_once()
        assert primeiro["created"] == 1

        # Mesmo conteudo entra de novo na fila (ex.: cron repetido).
        stage_item(content=conteudo, collection="content", root=spool)
        segundo = drainer.drain_once()
        assert segundo["duplicate"] == 1
        assert segundo["created"] == 0

        conn = await adapter._get_conn()
        cur = await conn.execute(
            "SELECT count(*) AS n FROM memories WHERE content_hash = %s",
            (compute_content_hash(conteudo),),
        )
        assert (await cur.fetchone())["n"] == 1, "o conteudo nao pode duplicar"

    @pytest.mark.asyncio
    async def test_duplicata_reaproveita_o_mesmo_entry_id(
        self, adapter: PostgresAdapter, drainer, spool: Path
    ) -> None:
        conteudo = "conteudo com id reaproveitado"
        stage_item(content=conteudo, collection="content", root=spool)
        drainer.drain_once()
        primeiro = next(iter(iter_items(spool, STATE_DONE)))

        stage_item(content=conteudo, collection="content", root=spool)
        drainer.drain_once()

        ids = {i.entry_id for i in iter_items(spool, STATE_DONE)}
        assert ids == {primeiro.entry_id}, "a duplicata aponta para o id existente"

    @pytest.mark.asyncio
    async def test_fila_vazia_e_no_op(self, drainer) -> None:
        assert drainer.drain_once()["total"] == 0

    @pytest.mark.asyncio
    async def test_varios_itens_numa_passada(
        self, adapter: PostgresAdapter, drainer, spool: Path
    ) -> None:
        for i in range(3):
            stage_item(content=f"item numero {i}", collection="content", root=spool)

        placar = drainer.drain_once()
        assert placar["created"] == 3
        assert len(list(iter_items(spool, STATE_DONE))) == 3

    @pytest.mark.asyncio
    async def test_item_corrompido_vai_para_failed(self, drainer, spool: Path) -> None:
        """Hash que nao descreve o conteudo nunca e gravado."""
        item = stage_item(content="conteudo original", collection="content", root=spool)
        # Adultera o conteudo em disco sem atualizar o hash.
        (spool / STATE_PENDING / item.content_file).write_text(
            "conteudo trocado", encoding="utf-8"
        )

        placar = drainer.drain_once()
        assert placar["corrompido"] == 1
        assert list(iter_items(spool, STATE_PENDING)) == []

        retidos = list(iter_items(spool, STATE_FAILED))
        assert len(retidos) == 1
        assert retidos[0].entry_id is None
        # Nada se perde: o conteudo continua em disco para revisao.
        assert (spool / STATE_FAILED / item.content_file).exists()

    @pytest.mark.asyncio
    async def test_dry_run_nao_grava(
        self, adapter: PostgresAdapter, spool: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ORKMIND_DATABASE_URL", DATABASE_URL)
        mod = carregar_drainer()
        seco = mod.Drainer(
            root=spool, api=None,
            cli=mod.CliBackend(REPO_ROOT / ".venv" / "bin" / "orkmind"),
            dry_run=True,
        )
        stage_item(content="nao deve ser gravado", collection="content", root=spool)
        seco.drain_once()

        assert len(list(iter_items(spool, STATE_PENDING))) == 1
        assert await adapter.count() == 0


class TestBackfill:
    """Migracao dos orfaos legados que ficaram fora do OrkMind."""

    @pytest.fixture
    def spool(self, tmp_path: Path) -> Path:
        return ensure_spool_dirs(tmp_path / "spool")

    def test_estagia_markdown_do_diretorio(self, tmp_path: Path, spool: Path) -> None:
        origem = tmp_path / "reports"
        origem.mkdir()
        (origem / "relatorio.md").write_text("# Relatorio\nconteudo", encoding="utf-8")

        mod = carregar_drainer()
        estagiados = mod.backfill(
            source_dir=origem, root=spool, collection="content",
            tags={"project": ["possibilidades-ia"]},
        )

        assert len(estagiados) == 1
        assert estagiados[0].collection == "content"
        assert estagiados[0].tags == {"project": ["possibilidades-ia"]}
        assert estagiados[0].entry_id is None

    def test_backfill_e_repetivel(self, tmp_path: Path, spool: Path) -> None:
        """Rodar duas vezes nao reestagia o que ja esta na fila."""
        origem = tmp_path / "reports"
        origem.mkdir()
        (origem / "relatorio.md").write_text("conteudo unico", encoding="utf-8")

        mod = carregar_drainer()
        assert len(mod.backfill(source_dir=origem, root=spool)) == 1
        assert len(mod.backfill(source_dir=origem, root=spool)) == 0
        assert len(list(iter_items(spool, STATE_PENDING))) == 1

    def test_backfill_nao_apaga_a_origem(self, tmp_path: Path, spool: Path) -> None:
        origem = tmp_path / "reports"
        origem.mkdir()
        arquivo = origem / "relatorio.md"
        arquivo.write_text("conteudo preservado", encoding="utf-8")

        mod = carregar_drainer()
        mod.backfill(source_dir=origem, root=spool)

        assert arquivo.exists()
        assert arquivo.read_text(encoding="utf-8") == "conteudo preservado"

    def test_backfill_ignora_arquivo_vazio(self, tmp_path: Path, spool: Path) -> None:
        origem = tmp_path / "reports"
        origem.mkdir()
        (origem / "vazio.md").write_text("   \n", encoding="utf-8")

        mod = carregar_drainer()
        assert mod.backfill(source_dir=origem, root=spool) == []

    def test_backfill_registra_a_origem(self, tmp_path: Path, spool: Path) -> None:
        """Auditoria: da para saber de qual arquivo veio a memoria."""
        origem = tmp_path / "reports"
        origem.mkdir()
        arquivo = origem / "relatorio.md"
        arquivo.write_text("conteudo rastreavel", encoding="utf-8")

        mod = carregar_drainer()
        item = mod.backfill(source_dir=origem, root=spool)[0]
        assert item.origin == str(arquivo)
        assert item.metadata["arquivo_origem"] == str(arquivo)

    def test_diretorio_inexistente_nao_quebra(self, tmp_path: Path, spool: Path) -> None:
        mod = carregar_drainer()
        assert mod.backfill(source_dir=tmp_path / "nao-existe", root=spool) == []


class TestStatus:
    def test_status_conta_os_estados(self, tmp_path: Path) -> None:
        spool = ensure_spool_dirs(tmp_path / "spool")
        stage_item(content="a", collection="content", root=spool)
        stage_item(content="b", collection="content", root=spool)

        mod = carregar_drainer()
        resumo = mod.status_report(spool)
        assert resumo[STATE_PENDING] == 2
        assert resumo[STATE_DONE] == 0
        assert json.dumps(resumo)  # precisa ser serializavel para o cron
