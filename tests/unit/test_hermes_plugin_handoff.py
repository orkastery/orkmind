"""Testes do lado do plugin Hermes para o handoff de conteudo (G3).

O plugin NAO rotaciona sessao: expoe a tool orkmind_handoff (validacao
e armazenamento via backend) e injeta o resumo do handoff herdado no
primeiro prefetch da sessao filha.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from tests.unit.test_hermes_plugin import (
    FakeBackend,
    make_provider,
)


class HandoffBackend(FakeBackend):
    """FakeBackend com o contrato de handoff do backend real."""

    def __init__(
        self,
        resultados: Optional[List[Dict[str, Any]]] = None,
        handoff: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.resultados = resultados or [{"status": "armazenado", "handoff_id": "h1"}]
        self.handoff = handoff
        self.submit_calls: List[Dict[str, Any]] = []
        self.fetch_calls: List[str] = []

    async def submit_handoff(self, **kwargs: Any) -> Dict[str, Any]:
        self.submit_calls.append(kwargs)
        indice = min(len(self.submit_calls) - 1, len(self.resultados) - 1)
        return dict(self.resultados[indice])

    async def get_handoff_for_session(
        self, session_id: str, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        self.fetch_calls.append(session_id)
        return dict(self.handoff) if self.handoff else None


PAYLOAD = {
    "progresso": "x" * 60,
    "decisoes": "y" * 60,
    "referencias_criticas": "z" * 60,
    "proximos_passos": "w" * 60,
}


class TestToolHandoff:
    def test_schema_exposto_com_as_demais_tools(self) -> None:
        provider = make_provider()
        nomes = [s["name"] for s in provider.get_tool_schemas()]
        assert "orkmind_handoff" in nomes
        assert len(nomes) == 5

    def test_payload_obrigatorio(self) -> None:
        provider = make_provider(HandoffBackend())
        saida = json.loads(provider.handle_tool_call("orkmind_handoff", {}))
        assert "error" in saida

    def test_submissao_repassa_sessao_e_payload(self) -> None:
        backend = HandoffBackend()
        provider = make_provider(backend)
        provider._session_id = "sessao-42"
        saida = json.loads(provider.handle_tool_call(
            "orkmind_handoff", {"payload": PAYLOAD}
        ))
        assert saida["status"] == "armazenado"
        chamada = backend.submit_calls[0]
        assert chamada["session_id"] == "sessao-42"
        assert chamada["payload"] == PAYLOAD
        assert chamada["refacao"] == 0

    def test_refacao_incrementa_e_zera_apos_aceite(self) -> None:
        backend = HandoffBackend(resultados=[
            {"status": "refazer", "refacao": 1},
            {"status": "refazer", "refacao": 2},
            {"status": "armazenado", "handoff_id": "h1"},
        ])
        provider = make_provider(backend)
        provider.handle_tool_call("orkmind_handoff", {"payload": {"a": "b"}})
        assert provider._handoff_refacoes == 1
        provider.handle_tool_call("orkmind_handoff", {"payload": {"a": "b"}})
        assert provider._handoff_refacoes == 2
        assert backend.submit_calls[1]["refacao"] == 1
        provider.handle_tool_call("orkmind_handoff", {"payload": PAYLOAD})
        assert backend.submit_calls[2]["refacao"] == 2
        assert provider._handoff_refacoes == 0

    def test_backend_sem_suporte_avisa_sem_quebrar(self) -> None:
        provider = make_provider(FakeBackend())
        saida = json.loads(provider.handle_tool_call(
            "orkmind_handoff", {"payload": PAYLOAD}
        ))
        assert "error" in saida

    def test_destination_opcional_repassado(self) -> None:
        backend = HandoffBackend()
        provider = make_provider(backend)
        provider.handle_tool_call(
            "orkmind_handoff",
            {"payload": PAYLOAD, "destination": "s-filha"},
        )
        assert backend.submit_calls[0]["destination"] == "s-filha"

    def test_initialize_zera_o_contador_de_refacoes(self) -> None:
        provider = make_provider(HandoffBackend())
        provider._handoff_refacoes = 2
        provider.initialize("sessao-nova")
        assert provider._handoff_refacoes == 0


class TestInjecaoNaSessaoFilha:
    def _handoff(self) -> Dict[str, Any]:
        return {
            "handoff_id": "h1",
            "package_id": "pkg-123",
            "origin": "sessao-mae",
            "resumo": "### progresso\ntrabalho na metade",
        }

    def test_primeiro_prefetch_injeta_o_resumo(self) -> None:
        backend = HandoffBackend(handoff=self._handoff())
        provider = make_provider(backend)
        provider._session_id = "sessao-filha"
        out = provider.prefetch("continuando o trabalho")
        assert "## OrkMind - Handoff da Sessao Anterior" in out
        assert "package_id: pkg-123" in out
        assert "origem: sessao-mae" in out
        assert "trabalho na metade" in out
        assert backend.fetch_calls == ["sessao-filha"]

    def test_turnos_seguintes_nao_reinjetam(self) -> None:
        backend = HandoffBackend(handoff=self._handoff())
        provider = make_provider(backend)
        provider.prefetch("primeiro turno")
        out = provider.prefetch("segundo turno")
        assert "Handoff da Sessao Anterior" not in out
        assert backend.fetch_calls == [""]

    def test_sem_handoff_pendente_nada_muda(self) -> None:
        backend = HandoffBackend(handoff=None)
        provider = make_provider(backend)
        out = provider.prefetch("primeiro turno")
        assert "Handoff da Sessao Anterior" not in out

    def test_falha_na_busca_nao_derruba_o_prefetch(self) -> None:
        class Quebrado(HandoffBackend):
            async def get_handoff_for_session(
                self, session_id: str, **kwargs: Any
            ) -> Optional[Dict[str, Any]]:
                raise RuntimeError("indisponivel")

        provider = make_provider(Quebrado())
        assert provider.prefetch("primeiro turno") == ""

    def test_backend_sem_contrato_de_handoff_nao_quebra(self) -> None:
        provider = make_provider(FakeBackend())
        assert provider.prefetch("primeiro turno") == ""
