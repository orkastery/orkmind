"""Testes do handoff de conteudo governado (fase G3, D-MA12 reescopado).

O OrkMind valida e armazena handoff; nunca o dispara. Cobrem a
validacao contra handoff-rules (secoes obrigatorias, conteudo minimo,
ate 2 refacoes), o armazenamento encadeado (handoff + semantic_log com
parent_id na entry session), a entrega para a sessao filha e o seed
idempotente.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from orkmind.core.config import OrkMindConfig
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.guardrails import SessionSnapshot, check
from orkmind.guardrails.handoff import (
    MAX_REFACOES,
    SECOES_OBRIGATORIAS_DEFAULT,
    handoff_para_sessao,
    normalizar_secao,
    submeter_handoff,
    validar_payload,
)
from orkmind.store.factory import create_store

RAIZ = Path(__file__).resolve().parents[2]


def _carregar_seed() -> Any:
    spec = importlib.util.spec_from_file_location(
        "seed_handoff_rules", RAIZ / "scripts" / "seed_handoff_rules.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_mod = _carregar_seed()


async def _nova_layer() -> SemanticLayer:
    store = create_store(OrkMindConfig(store_backend="memory"))
    await store.initialize()
    return SemanticLayer(store)


def _payload_valido() -> dict[str, Any]:
    return {
        "progresso": "Implementadas as fases G1 e G2 do roadmap de guardrails, "
                     "com a suite completa passando apos cada fase.",
        "decisoes": "O advisory deixou de prometer handoff automatico porque "
                    "o OrkMind nunca dispara processo de runtime.",
        "referencias_criticas": "docs/sempre-gravar.md, modulo "
                                "orkmind.guardrails, testes de caracterizacao.",
        "proximos_passos": "Implementar a fase G3 (handoff de conteudo "
                           "governado) e rodar a suite completa de novo.",
    }


class TestValidacaoDePayload:
    def test_payload_completo_e_valido(self) -> None:
        resultado = validar_payload(
            _payload_valido(), list(SECOES_OBRIGATORIAS_DEFAULT)
        )
        assert resultado.valido is True
        assert resultado.secoes_faltantes == []
        assert resultado.secoes_curtas == []

    def test_secao_ausente_e_apontada(self) -> None:
        payload = _payload_valido()
        del payload["decisoes"]
        resultado = validar_payload(payload, list(SECOES_OBRIGATORIAS_DEFAULT))
        assert resultado.valido is False
        assert resultado.secoes_faltantes == ["decisoes"]

    def test_conteudo_minimo_por_secao(self) -> None:
        payload = _payload_valido()
        payload["progresso"] = "pouco"
        resultado = validar_payload(payload, list(SECOES_OBRIGATORIAS_DEFAULT))
        assert resultado.valido is False
        assert resultado.secoes_curtas == ["progresso"]

    def test_nomes_de_secao_sao_normalizados(self) -> None:
        payload = _payload_valido()
        payload["Referências Críticas"] = payload.pop("referencias_criticas")
        resultado = validar_payload(payload, list(SECOES_OBRIGATORIAS_DEFAULT))
        assert resultado.valido is True

    def test_normalizar_secao(self) -> None:
        assert normalizar_secao("Próximos Passos") == "proximos_passos"
        assert normalizar_secao("referencias-criticas") == "referencias_criticas"

    def test_secao_com_valor_estruturado_conta_como_texto(self) -> None:
        payload = _payload_valido()
        payload["referencias_criticas"] = {
            "arquivos": ["src/a.py", "src/b.py"],
            "entries": ["id-1", "id-2"],
        }
        resultado = validar_payload(payload, list(SECOES_OBRIGATORIAS_DEFAULT))
        assert resultado.valido is True


class TestSubmissaoComRefacoes:
    @pytest.mark.asyncio
    async def test_payload_invalido_volta_para_refazer(self) -> None:
        layer = await _nova_layer()
        payload = {"progresso": "so isso"}
        resultado = await submeter_handoff(
            layer, session_id="s-origem", payload=payload, refacao=0
        )
        assert resultado["status"] == "refazer"
        assert resultado["refacao"] == 1
        assert "decisoes" in resultado["secoes_faltantes"]
        assert "progresso" in resultado["secoes_curtas"]

    @pytest.mark.asyncio
    async def test_refacoes_esgotadas_aceitam_com_avisos(self) -> None:
        layer = await _nova_layer()
        payload = {"progresso": "so isso"}
        resultado = await submeter_handoff(
            layer, session_id="s-origem", payload=payload, refacao=MAX_REFACOES
        )
        assert resultado["status"] == "armazenado"
        assert any("pendencias" in a for a in resultado["avisos"])

    @pytest.mark.asyncio
    async def test_payload_valido_e_armazenado_direto(self) -> None:
        layer = await _nova_layer()
        resultado = await submeter_handoff(
            layer, session_id="s-origem", payload=_payload_valido()
        )
        assert resultado["status"] == "armazenado"
        assert resultado["handoff_id"]
        assert resultado["package_id"]


class TestArmazenamentoEncadeado:
    @pytest.mark.asyncio
    async def test_handoff_e_pacote_encadeados_na_entry_session(self) -> None:
        layer = await _nova_layer()
        resultado = await submeter_handoff(
            layer, session_id="s-origem", payload=_payload_valido(),
            destination="s-filha",
        )
        sessao_id = resultado["session_entry_id"]

        sessao = await layer.store.retrieve(sessao_id)
        assert sessao is not None
        assert sessao.collection == "session"
        assert sessao.metadata["session_id"] == "s-origem"

        handoff = await layer.store.retrieve(resultado["handoff_id"])
        assert handoff is not None
        assert handoff.collection == "handoff"
        assert handoff.parent_id == sessao_id
        assert handoff.metadata["origin"] == "s-origem"
        assert handoff.metadata["destination"] == "s-filha"
        assert handoff.metadata["package_id"] == resultado["package_id"]

        pacote = await layer.store.retrieve(resultado["package_entry_id"])
        assert pacote is not None
        assert pacote.collection == "semantic_log"
        assert pacote.parent_id == sessao_id
        assert pacote.metadata["package_id"] == resultado["package_id"]
        assert json.loads(pacote.content) == _payload_valido()

    @pytest.mark.asyncio
    async def test_entry_session_e_reaproveitada(self) -> None:
        layer = await _nova_layer()
        payload_a = _payload_valido()
        payload_b = {**_payload_valido(), "extra": "segunda rodada da sessao"}
        r1 = await submeter_handoff(
            layer, session_id="s-origem", payload=payload_a
        )
        r2 = await submeter_handoff(
            layer, session_id="s-origem", payload=payload_b
        )
        assert r1["session_entry_id"] == r2["session_entry_id"]

    @pytest.mark.asyncio
    async def test_resumo_compacto_cobre_as_secoes(self) -> None:
        layer = await _nova_layer()
        resultado = await submeter_handoff(
            layer, session_id="s-origem", payload=_payload_valido()
        )
        handoff = await layer.store.retrieve(resultado["handoff_id"])
        assert handoff is not None
        for secao in SECOES_OBRIGATORIAS_DEFAULT:
            assert f"### {secao}" in handoff.content


class TestEntregaParaSessaoFilha:
    @pytest.mark.asyncio
    async def test_destination_explicito_tem_prioridade(self) -> None:
        layer = await _nova_layer()
        await submeter_handoff(
            layer, session_id="s-a", payload=_payload_valido(),
            destination="s-filha",
        )
        entrega = await handoff_para_sessao(layer, "s-filha")
        assert entrega is not None
        assert entrega["origin"] == "s-a"
        assert "### progresso" in entrega["resumo"]

    @pytest.mark.asyncio
    async def test_handoff_sem_destino_e_consumido_e_marcado(self) -> None:
        layer = await _nova_layer()
        resultado = await submeter_handoff(
            layer, session_id="s-a", payload=_payload_valido()
        )
        entrega = await handoff_para_sessao(layer, "s-nova")
        assert entrega is not None
        assert entrega["package_id"] == resultado["package_id"]

        marcado = await layer.store.retrieve(resultado["handoff_id"])
        assert marcado is not None
        assert marcado.metadata["destination"] == "s-nova"

    @pytest.mark.asyncio
    async def test_origem_nao_consome_o_proprio_handoff(self) -> None:
        layer = await _nova_layer()
        await submeter_handoff(
            layer, session_id="s-a", payload=_payload_valido()
        )
        assert await handoff_para_sessao(layer, "s-a") is None

    @pytest.mark.asyncio
    async def test_sem_handoff_pendente_devolve_none(self) -> None:
        layer = await _nova_layer()
        assert await handoff_para_sessao(layer, "s-x") is None


class TestSeedHandoffRules:
    @pytest.mark.asyncio
    async def test_seed_cria_as_quatro_rules_protegidas(self) -> None:
        layer = await _nova_layer()
        resultado = await seed_mod.seed_handoff_rules(layer, verbose=False)
        assert resultado == {"created": 4, "skipped": 0}
        rules = await layer.store.search_by_tags(
            tags={"situation": ["handoff-rules"]}, collection="rule"
        )
        assert len(rules) == 4
        for rule in rules:
            assert rule.protected is True
            assert rule.mandatory is False
            assert rule.visibility == "public"
            assert rule.metadata["handoff_section"] in (
                SECOES_OBRIGATORIAS_DEFAULT
            )

    @pytest.mark.asyncio
    async def test_seed_e_idempotente(self) -> None:
        layer = await _nova_layer()
        await seed_mod.seed_handoff_rules(layer, verbose=False)
        segunda = await seed_mod.seed_handoff_rules(layer, verbose=False)
        assert segunda == {"created": 0, "skipped": 4}

    @pytest.mark.asyncio
    async def test_validacao_usa_as_rules_seedadas(self) -> None:
        layer = await _nova_layer()
        await seed_mod.seed_handoff_rules(layer, verbose=False)
        resultado = await submeter_handoff(
            layer, session_id="s-a", payload={"progresso": "curto"}
        )
        assert resultado["status"] == "refazer"
        assert not any("nao seedadas" in a for a in resultado.get("avisos", []))

    @pytest.mark.asyncio
    async def test_handoff_content_available_vira_true_com_seed(self) -> None:
        layer = await _nova_layer()
        antes = await check(SessionSnapshot(session_id="s1"), layer=layer)
        assert antes.session.handoff_content_available is False
        await seed_mod.seed_handoff_rules(layer, verbose=False)
        depois = await check(SessionSnapshot(session_id="s1"), layer=layer)
        assert depois.session.handoff_content_available is True
