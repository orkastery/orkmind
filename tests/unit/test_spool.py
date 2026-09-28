"""Testes da fila de saida (spool) do OrkMind.

A spool e a garantia de durabilidade da solucao "sempre gravar": estes
testes cobrem o contrato em disco, a idempotencia por hash, o backoff e
a regra constitucional de que nada e apagado.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orkmind.core.injection import compute_content_hash as hash_do_core
from orkmind.spool import (
    BACKOFF_MINUTES,
    DEFAULT_MAX_ATTEMPTS,
    SPOOL_DIR_ENV,
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    SpoolItem,
    build_item_id,
    compute_content_hash,
    default_spool_dir,
    ensure_spool_dirs,
    iter_items,
    move_item,
    read_content,
    slugify,
    stage_item,
    write_item,
)


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    return ensure_spool_dirs(tmp_path / "spool")


class TestHash:
    def test_hash_bate_com_o_do_core(self) -> None:
        """A spool e o core precisam concordar na chave de dedupe.

        O plugin do Hermes reimplementa o hash com stdlib pura. Se as
        duas implementacoes divergirem, o drainer nunca reconheceria a
        entry ja gravada e passaria a duplicar memoria.
        """
        conteudo = "Relatorio semanal de possibilidades de IA"
        assert compute_content_hash(conteudo) == hash_do_core(conteudo)

    def test_hash_e_deterministico_e_sensivel(self) -> None:
        assert compute_content_hash("abc") == compute_content_hash("abc")
        assert compute_content_hash("abc") != compute_content_hash("abd")

    def test_hash_lida_com_acentos(self) -> None:
        assert len(compute_content_hash("memoria com acentuacao: ao, cao")) == 64


class TestNomeDeItem:
    def test_slug_remove_acento_e_simbolo(self) -> None:
        assert slugify("Possibilidades IA - 2026/W36") == "possibilidades-ia-2026-w36"

    def test_slug_nunca_vazio(self) -> None:
        assert slugify("!!!") == "item"

    def test_item_id_ordenavel_e_com_hash(self) -> None:
        momento = datetime(2026, 8, 31, 9, 11, 0, tzinfo=timezone.utc)
        item_id = build_item_id("relatorio", "a" * 64, momento)
        assert item_id == "20260831_091100_relatorio_aaaaaaaa"

    def test_itens_do_mesmo_segundo_nao_colidem(self) -> None:
        """O sufixo de hash evita sobrescrita entre itens simultaneos."""
        momento = datetime(2026, 8, 31, 9, 11, 0, tzinfo=timezone.utc)
        a = build_item_id("rel", compute_content_hash("conteudo A"), momento)
        b = build_item_id("rel", compute_content_hash("conteudo B"), momento)
        assert a != b


class TestEstagio:
    def test_stage_grava_json_e_md(self, spool: Path) -> None:
        item = stage_item(
            content="conteudo durável de teste",
            collection="content",
            tags={"project": ["orkmind"]},
            label="relatorio",
            root=spool,
        )
        md = spool / STATE_PENDING / item.content_file
        js = spool / STATE_PENDING / f"{item.item_id}.json"
        assert md.exists() and js.exists()
        assert md.read_text(encoding="utf-8") == "conteudo durável de teste"

        dados = json.loads(js.read_text(encoding="utf-8"))
        assert dados["status"] == STATE_PENDING
        assert dados["collection"] == "content"
        assert dados["tags"] == {"project": ["orkmind"]}
        assert dados["attempts"] == 0
        assert dados["max_attempts"] == DEFAULT_MAX_ATTEMPTS

    def test_stage_nunca_declara_entry_id(self, spool: Path) -> None:
        """Regra constitucional: id so vem do backend, nunca do estagio."""
        item = stage_item(content="x", collection="fact", root=spool)
        assert item.entry_id is None
        dados = json.loads(
            (spool / STATE_PENDING / f"{item.item_id}.json").read_text(encoding="utf-8")
        )
        assert dados["entry_id"] is None

    def test_stage_hash_descreve_o_conteudo(self, spool: Path) -> None:
        item = stage_item(content="abc", collection="fact", root=spool)
        assert item.content_hash == compute_content_hash("abc")

    def test_stage_nao_deixa_arquivo_tmp(self, spool: Path) -> None:
        stage_item(content="abc", collection="fact", root=spool)
        assert list((spool / STATE_PENDING).glob("*.tmp")) == []

    def test_read_content_recupera_verbatim(self, spool: Path) -> None:
        original = "linha 1\nlinha 2\n\n# titulo"
        item = stage_item(content=original, collection="content", root=spool)
        assert read_content(item, spool, STATE_PENDING) == original


class TestMovimentacao:
    def test_move_para_done_leva_json_e_md(self, spool: Path) -> None:
        item = stage_item(content="abc", collection="fact", root=spool)
        item.entry_id = "id-real-do-backend"
        move_item(item, spool, STATE_DONE)

        assert not (spool / STATE_PENDING / f"{item.item_id}.json").exists()
        assert not (spool / STATE_PENDING / item.content_file).exists()
        assert (spool / STATE_DONE / f"{item.item_id}.json").exists()
        assert (spool / STATE_DONE / item.content_file).exists()

    def test_move_preserva_conteudo_e_id(self, spool: Path) -> None:
        """Nada se perde: o conteudo continua legivel apos o move."""
        item = stage_item(content="conteudo importante", collection="content", root=spool)
        item.entry_id = "entry-123"
        move_item(item, spool, STATE_DONE)

        dados = json.loads(
            (spool / STATE_DONE / f"{item.item_id}.json").read_text(encoding="utf-8")
        )
        assert dados["status"] == STATE_DONE
        assert dados["entry_id"] == "entry-123"
        assert read_content(item, spool, STATE_DONE) == "conteudo importante"

    def test_move_para_failed_retem_para_revisao(self, spool: Path) -> None:
        item = stage_item(content="abc", collection="fact", root=spool)
        item.last_error = "backend fora"
        move_item(item, spool, STATE_FAILED)
        assert (spool / STATE_FAILED / item.content_file).exists()
        assert read_content(item, spool, STATE_FAILED) == "abc"


class TestIteracao:
    def test_iter_ordena_por_nome(self, spool: Path) -> None:
        for i in range(3):
            item = SpoolItem(
                item_id=f"2026083{i}_090000_rel_abcd000{i}",
                collection="content",
                content_hash=compute_content_hash(str(i)),
                content_file=f"2026083{i}_090000_rel_abcd000{i}.md",
            )
            write_item(item, spool, STATE_PENDING)
        ids = [i.item_id for i in iter_items(spool, STATE_PENDING)]
        assert ids == sorted(ids)

    def test_iter_ignora_json_corrompido(self, spool: Path) -> None:
        """Um .json quebrado nao pode travar a fila inteira."""
        stage_item(content="bom", collection="fact", root=spool)
        (spool / STATE_PENDING / "quebrado.json").write_text("{ nao e json", encoding="utf-8")
        assert len(list(iter_items(spool, STATE_PENDING))) == 1

    def test_iter_em_estado_inexistente_e_vazio(self, tmp_path: Path) -> None:
        assert list(iter_items(tmp_path / "nada", STATE_PENDING)) == []


class TestBackoff:
    def test_item_novo_esta_pronto(self) -> None:
        item = SpoolItem(item_id="a", collection="fact", content_hash="h", content_file="a.md")
        assert item.is_due()

    def test_retry_incrementa_e_agenda(self) -> None:
        agora = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
        item = SpoolItem(item_id="a", collection="fact", content_hash="h", content_file="a.md")
        item.schedule_retry("backend fora", now=agora)

        assert item.attempts == 1
        assert item.last_error == "backend fora"
        esperado = agora + timedelta(minutes=BACKOFF_MINUTES[0])
        assert datetime.fromisoformat(item.next_attempt_at) == esperado

    def test_backoff_e_progressivo(self) -> None:
        agora = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
        item = SpoolItem(item_id="a", collection="fact", content_hash="h", content_file="a.md")
        esperas = []
        for _ in range(4):
            item.schedule_retry("erro", now=agora)
            esperas.append(datetime.fromisoformat(item.next_attempt_at) - agora)
        assert esperas == sorted(esperas)
        assert esperas[0] < esperas[-1]

    def test_item_em_backoff_nao_esta_pronto(self) -> None:
        agora = datetime.now(timezone.utc).astimezone()
        item = SpoolItem(item_id="a", collection="fact", content_hash="h", content_file="a.md")
        item.schedule_retry("erro", now=agora)
        assert not item.is_due(now=agora)
        assert item.is_due(now=agora + timedelta(minutes=BACKOFF_MINUTES[0] + 1))

    def test_exhausted_apos_max_attempts(self) -> None:
        item = SpoolItem(
            item_id="a", collection="fact", content_hash="h",
            content_file="a.md", max_attempts=3,
        )
        for _ in range(2):
            item.schedule_retry("erro")
        assert not item.exhausted
        item.schedule_retry("erro")
        assert item.exhausted

    def test_next_attempt_invalido_nao_trava_item(self) -> None:
        """Data corrompida no .json deve liberar o retry, nao bloquear."""
        item = SpoolItem(
            item_id="a", collection="fact", content_hash="h",
            content_file="a.md", next_attempt_at="data-invalida",
        )
        assert item.is_due()


class TestSerializacao:
    def test_roundtrip_preserva_campos(self) -> None:
        item = SpoolItem(
            item_id="x", collection="content", content_hash="h", content_file="x.md",
            tags={"project": ["orkmind"]}, entry_id="e1", attempts=2,
        )
        volta = SpoolItem.from_dict(item.to_dict())
        assert volta.to_dict() == item.to_dict()

    def test_from_dict_ignora_campo_desconhecido(self) -> None:
        """Um .json de versao futura nao pode derrubar o drainer."""
        item = SpoolItem.from_dict({
            "item_id": "x", "collection": "fact", "content_hash": "h",
            "content_file": "x.md", "campo_do_futuro": 42,
        })
        assert item.item_id == "x"

    def test_from_dict_preenche_faltantes(self) -> None:
        item = SpoolItem.from_dict({"item_id": "x"})
        assert item.collection == "content"
        assert item.content_file == "x.md"


class TestDiretorioPadrao:
    def test_env_redireciona_a_raiz(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(SPOOL_DIR_ENV, str(tmp_path / "outro"))
        assert default_spool_dir() == tmp_path / "outro"

    def test_sem_env_usa_default(self, monkeypatch) -> None:
        monkeypatch.delenv(SPOOL_DIR_ENV, raising=False)
        assert default_spool_dir().name == "orkmind-spool"

    def test_ensure_cria_os_tres_estados(self, tmp_path: Path) -> None:
        base = ensure_spool_dirs(tmp_path / "s")
        for estado in (STATE_PENDING, STATE_DONE, STATE_FAILED):
            assert (base / estado).is_dir()

    def test_ensure_e_idempotente(self, tmp_path: Path) -> None:
        ensure_spool_dirs(tmp_path / "s")
        ensure_spool_dirs(tmp_path / "s")
        assert (tmp_path / "s" / STATE_PENDING).is_dir()
