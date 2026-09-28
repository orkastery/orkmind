"""Testes da fila de saida no plugin OrkMind do Hermes.

Cobrem a mudanca central da solucao "sempre gravar": `orkmind_store`
estagia em disco ANTES de tentar gravar, e nunca devolve um entry_id que
o backend nao confirmou.

Reaproveitam o carregador de plugin de test_hermes_plugin.py, que instala
o stub de `agent.memory_provider`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_hermes_plugin import FakeBackend, make_provider, plugin


class StoreBackend(FakeBackend):
    """Backend falso com store_memory, que pode falhar sob comando."""

    def __init__(self, entry_id: str = "entry-real-do-backend", falha: bool = False) -> None:
        super().__init__()
        self.entry_id = entry_id
        self.falha = falha
        self.chamadas: list[dict[str, Any]] = []

    async def store_memory(self, **kwargs: Any) -> str:
        self.chamadas.append(kwargs)
        if self.falha:
            raise RuntimeError("banco indisponivel")
        return self.entry_id


@pytest.fixture
def spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redireciona a spool do plugin para um diretorio descartavel."""
    destino = tmp_path / "spool"
    monkeypatch.setenv("ORKMIND_SPOOL_DIR", str(destino))
    return destino


def pending(spool: Path) -> list[Path]:
    return sorted((spool / "pending").glob("*.json"))


def done(spool: Path) -> list[Path]:
    return sorted((spool / "done").glob("*.json"))


def ler(caminho: Path) -> dict[str, Any]:
    return json.loads(caminho.read_text(encoding="utf-8"))


def store(provider: Any, content: str = "memoria de teste", **over: Any) -> dict[str, Any]:
    args = {"content": content, "collection": "content"}
    args.update(over)
    return json.loads(provider._handle_store(args))


class TestResolucaoDoDiretorio:
    def test_env_redireciona_a_spool(self, spool: Path) -> None:
        assert plugin._resolve_spool_dir() == spool

    def test_sem_env_usa_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ORKMIND_SPOOL_DIR", raising=False)
        assert plugin._resolve_spool_dir().name == "orkmind-spool"


class TestHashDoPlugin:
    def test_hash_bate_com_o_do_core(self) -> None:
        """O plugin reimplementa o hash com stdlib pura.

        Se divergir do core, o drainer nunca reconheceria a entry ja
        gravada e passaria a duplicar memoria.
        """
        from orkmind.core.injection import compute_content_hash

        texto = "conteudo com acentuacao: memoria, licao, atencao"
        assert plugin._spool_content_hash(texto) == compute_content_hash(texto)


class TestCaminhoFeliz:
    def test_grava_e_fecha_o_item_em_done(self, spool: Path) -> None:
        provider = make_provider(StoreBackend("entry-123"))
        resposta = store(provider)

        assert resposta["status"] == "armazenado"
        assert resposta["id"] == "entry-123"
        assert pending(spool) == []
        assert len(done(spool)) == 1

    def test_item_em_done_carrega_o_id_real(self, spool: Path) -> None:
        provider = make_provider(StoreBackend("entry-abc"))
        store(provider)

        item = ler(done(spool)[0])
        assert item["entry_id"] == "entry-abc"
        assert item["status"] == "done"
        assert item["result"] == "created"

    def test_resposta_traz_content_hash(self, spool: Path) -> None:
        provider = make_provider(StoreBackend())
        resposta = store(provider, content="conteudo rastreavel")
        assert resposta["content_hash"] == plugin._spool_content_hash("conteudo rastreavel")

    def test_conteudo_preservado_verbatim(self, spool: Path) -> None:
        provider = make_provider(StoreBackend())
        original = "# Titulo\n\nlinha 1\nlinha 2\n"
        store(provider, content=original)

        item = ler(done(spool)[0])
        md = spool / "done" / item["content_file"]
        assert md.read_text(encoding="utf-8") == original

    def test_backend_recebe_source_agent(self, spool: Path) -> None:
        backend = StoreBackend()
        store(make_provider(backend), collection="fact")
        assert backend.chamadas[0]["source"] == "agent"
        assert backend.chamadas[0]["mandatory"] is False


class TestBackendFora:
    """O incidente original: gravacao direta impossivel."""

    def test_conteudo_fica_seguro_em_pending(self, spool: Path) -> None:
        provider = make_provider(StoreBackend(falha=True))
        resposta = store(provider, content="relatorio que nao pode se perder")

        assert resposta["status"] == "pendente"
        assert len(pending(spool)) == 1
        assert done(spool) == []

    def test_nunca_inventa_entry_id(self, spool: Path) -> None:
        """Regra constitucional: sem confirmacao do backend, sem id."""
        provider = make_provider(StoreBackend(falha=True))
        resposta = store(provider)

        assert resposta["id"] is None
        assert ler(pending(spool)[0])["entry_id"] is None

    def test_resposta_nao_afirma_sucesso(self, spool: Path) -> None:
        provider = make_provider(StoreBackend(falha=True))
        resposta = store(provider)
        assert resposta["status"] != "armazenado"
        assert "NAO confirmada" in resposta["message"]

    def test_resposta_aponta_o_item_da_fila(self, spool: Path) -> None:
        provider = make_provider(StoreBackend(falha=True))
        resposta = store(provider)
        assert resposta["spool_item"] == ler(pending(spool)[0])["item_id"]

    def test_conteudo_recuperavel_do_disco(self, spool: Path) -> None:
        """O que o drainer vai ler depois precisa estar integro agora."""
        provider = make_provider(StoreBackend(falha=True))
        original = "conteudo integral do relatorio semanal"
        store(provider, content=original)

        item = ler(pending(spool)[0])
        md = spool / "pending" / item["content_file"]
        assert md.read_text(encoding="utf-8") == original
        assert item["content_hash"] == plugin._spool_content_hash(original)

    def test_item_pendente_tem_metadados_para_drenar(self, spool: Path) -> None:
        provider = make_provider(StoreBackend(falha=True))
        store(provider, collection="content", tags={"project": ["orkmind"]}, priority="high")

        item = ler(pending(spool)[0])
        assert item["collection"] == "content"
        assert item["tags"] == {"project": ["orkmind"]}
        assert item["priority"] == "high"
        assert item["source"] == "agent"
        assert item["status"] == "pending"
        assert item["attempts"] == 0


class TestOrdemDeEscrita:
    def test_estagia_antes_de_gravar(self, spool: Path, monkeypatch) -> None:
        """A spool tem que existir no momento em que o backend e chamado.

        Se a ordem invertesse, uma queda do processo entre a tentativa e o
        estagio perderia o conteudo, que e exatamente o incidente que a
        solucao elimina.
        """
        visto: dict[str, Any] = {}

        class Espia(StoreBackend):
            async def store_memory(self, **kwargs: Any) -> str:
                visto["pendentes_no_momento"] = len(pending(spool))
                return await super().store_memory(**kwargs)

        store(make_provider(Espia()))
        assert visto["pendentes_no_momento"] == 1


class TestValidacaoPreservada:
    """As protecoes antigas nao podem ter sido perdidas na reordenacao."""

    def test_colecao_invalida_nao_estagia(self, spool: Path) -> None:
        provider = make_provider(StoreBackend())
        resposta = store(provider, collection="inexistente")
        assert "error" in resposta
        assert pending(spool) == []

    def test_priority_critical_recusada(self, spool: Path) -> None:
        provider = make_provider(StoreBackend())
        resposta = store(provider, priority="critical")
        assert "error" in resposta
        assert pending(spool) == []

    def test_dimensao_de_tag_invalida_recusada(self, spool: Path) -> None:
        provider = make_provider(StoreBackend())
        resposta = store(provider, tags={"dimensao_inventada": ["x"]})
        assert "error" in resposta
        assert pending(spool) == []

    def test_content_vazio_recusado(self, spool: Path) -> None:
        provider = make_provider(StoreBackend())
        assert "error" in store(provider, content="")


class TestMarkDone:
    def test_sem_entry_id_nao_move(self, spool: Path) -> None:
        """Salvaguarda: item so sai de pending com id de verdade."""
        item = plugin.stage_to_spool(content="x", collection="fact", root=spool)
        assert plugin.mark_done(item, "", root=spool) is False
        assert len(pending(spool)) == 1

    def test_move_e_preserva_conteudo(self, spool: Path) -> None:
        item = plugin.stage_to_spool(content="conteudo", collection="fact", root=spool)
        assert plugin.mark_done(item, "entry-9", root=spool) is True

        assert pending(spool) == []
        assert ler(done(spool)[0])["entry_id"] == "entry-9"
        assert (spool / "done" / item["content_file"]).read_text(encoding="utf-8") == "conteudo"
