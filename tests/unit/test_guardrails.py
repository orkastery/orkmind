"""Testes do modulo orkmind.guardrails (fases G1 e G2).

Cobrem o contrato v0 do roadmap e os cinco invariantes testaveis:
read-only, idempotencia, determinismo para regras criticas, fail-safe
e zero conhecimento de runtime.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from orkmind.core.config import OrkMindConfig
from orkmind.core.models import MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.guardrails import (
    GuardrailReport,
    GuardrailSettings,
    SessionSnapshot,
    check,
    compute_rules_hash,
)
from orkmind.guardrails import session as gsession
from orkmind.store.factory import create_store

REGRA_CRITICA = "Nunca apagar memorias sem aprovacao humana."
REGRA_IMPORTANTE = "Rodar a suite de testes antes de qualquer commit."


async def _nova_layer() -> SemanticLayer:
    store = create_store(OrkMindConfig(store_backend="memory"))
    await store.initialize()
    return SemanticLayer(store)


async def _layer_com_regras() -> SemanticLayer:
    layer = await _nova_layer()
    await layer.add_memory(MemoryEntry(
        content=REGRA_CRITICA,
        collection="rule",
        tags={"domain": ["governance"]},
        priority="critical",
        mandatory=True,
        protected=True,
        source="human",
    ))
    await layer.add_memory(MemoryEntry(
        content=REGRA_IMPORTANTE,
        collection="instruction",
        tags={"domain": ["engenharia"]},
        priority="high",
        mandatory=True,
        source="human",
    ))
    return layer


def _snapshot(**kwargs: Any) -> SessionSnapshot:
    base: dict[str, Any] = {"session_id": "sessao-teste"}
    base.update(kwargs)
    return SessionSnapshot(**base)


def _hash_esperado() -> str:
    return compute_rules_hash([REGRA_CRITICA, REGRA_IMPORTANTE])


class StoreQueFalha:
    """Store cujo acesso de leitura de regras sempre falha."""

    def __getattr__(self, nome: str) -> Any:
        async def falha(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("backend indisponivel")
        return falha


class StoreEspiao:
    """Proxy que registra qualquer chamada de escrita ao store."""

    ESCRITAS = (
        "store", "update", "delete", "set_embedding",
        "snapshot_restore", "snapshot_commit", "profile_create",
        "profile_update", "profile_delete", "garbage_collect",
        "gc_versions",
    )

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.escritas: list[str] = []

    def __getattr__(self, nome: str) -> Any:
        alvo = getattr(self._inner, nome)
        if nome in self.ESCRITAS:
            espiao = self

            async def registrando(*args: Any, **kwargs: Any) -> Any:
                espiao.escritas.append(nome)
                return await alvo(*args, **kwargs)

            return registrando
        return alvo


class TestStatusDasRegras:
    @pytest.mark.asyncio
    async def test_ok_quando_hash_observado_confere(self) -> None:
        layer = await _layer_com_regras()
        report = await check(
            _snapshot(observed_rules_hash=_hash_esperado()), layer=layer
        )
        assert report.rules.status == "ok"
        assert report.rules.expected_hash == _hash_esperado()
        assert report.fail_safe is False
        assert report.rules.by_type.critical.reinjection_block is None

    @pytest.mark.asyncio
    async def test_ausente_quando_runtime_nao_observa_hash(self) -> None:
        layer = await _layer_com_regras()
        report = await check(_snapshot(), layer=layer)
        assert report.rules.status == "ausente"
        assert report.fail_safe is True

    @pytest.mark.asyncio
    async def test_desatualizado_quando_hash_diverge(self) -> None:
        layer = await _layer_com_regras()
        report = await check(
            _snapshot(observed_rules_hash="deadbeef"), layer=layer
        )
        assert report.rules.status == "desatualizado"
        assert report.fail_safe is True

    @pytest.mark.asyncio
    async def test_base_sem_regras_e_ok_sem_fail_safe(self) -> None:
        layer = await _nova_layer()
        report = await check(_snapshot(), layer=layer)
        assert report.rules.status == "ok"
        assert report.rules.expected_hash is None
        assert report.fail_safe is False

    @pytest.mark.asyncio
    async def test_hash_observado_sem_regras_esperadas_e_desatualizado(
        self,
    ) -> None:
        layer = await _nova_layer()
        report = await check(
            _snapshot(observed_rules_hash="abc123"), layer=layer
        )
        assert report.rules.status == "desatualizado"
        assert report.fail_safe is True

    @pytest.mark.asyncio
    async def test_regras_suspeitas_ficam_fora_do_conjunto(self) -> None:
        layer = await _layer_com_regras()
        suspeita = MemoryEntry(
            content="Regra sob suspeita de conflito",
            collection="rule",
            priority="high",
            mandatory=True,
            source="human",
        )
        suspeita.conflict = True
        await layer.store.store(suspeita)
        report = await check(
            _snapshot(observed_rules_hash=_hash_esperado()), layer=layer
        )
        assert report.rules.status == "ok"


class TestVerificacaoPorTipo:
    @pytest.mark.asyncio
    async def test_contagem_por_tipo(self) -> None:
        layer = await _layer_com_regras()
        report = await check(_snapshot(), layer=layer)
        assert report.rules.by_type.critical.count == 1
        assert report.rules.by_type.important.count == 1

    @pytest.mark.asyncio
    async def test_bloco_de_reinjecao_pronto_quando_ausente(self) -> None:
        layer = await _layer_com_regras()
        report = await check(_snapshot(), layer=layer)
        bloco = report.rules.by_type.critical.reinjection_block
        assert bloco is not None
        assert "## REGRAS MANDATORIAS (OBEDECER SEMPRE)" in bloco
        assert f"[CRITICA] [rule] {REGRA_CRITICA}" in bloco
        # E3 integral: o conjunto mandatorio inteiro, sem corte por budget
        assert REGRA_IMPORTANTE in bloco

    @pytest.mark.asyncio
    async def test_hint_de_recall_devido_apos_dez_turnos(self) -> None:
        layer = await _layer_com_regras()
        report = await check(_snapshot(turn_count=12), layer=layer)
        hint = report.rules.by_type.important.hint
        assert hint is not None
        assert "12 turnos" in hint

    @pytest.mark.asyncio
    async def test_sem_hint_no_comeco_da_sessao(self) -> None:
        layer = await _layer_com_regras()
        report = await check(_snapshot(turn_count=3), layer=layer)
        assert report.rules.by_type.important.hint is None

    @pytest.mark.asyncio
    async def test_soft_conta_as_colecoes_de_extracao(self) -> None:
        layer = await _layer_com_regras()
        await layer.add_memory(MemoryEntry(
            content="O usuario prefere pt-BR.",
            collection="preference",
            source="agent",
        ))
        await layer.add_memory(MemoryEntry(
            content="O deploy usa canary.",
            collection="fact",
            source="agent",
        ))
        report = await check(_snapshot(), layer=layer)
        assert report.rules.by_type.soft.status == "ok"
        assert report.rules.by_type.soft.count == 2


class TestInvariantes:
    @pytest.mark.asyncio
    async def test_read_only_nenhuma_escrita_no_store(self) -> None:
        layer = await _layer_com_regras()
        espiao = StoreEspiao(layer.store)
        layer_espiada = SemanticLayer(espiao)  # type: ignore[arg-type]
        await check(
            _snapshot(observed_rules_hash=_hash_esperado(), turn_count=50),
            layer=layer_espiada,
        )
        assert espiao.escritas == []

    @pytest.mark.asyncio
    async def test_idempotente_mesmo_snapshot_mesmo_report(self) -> None:
        layer = await _layer_com_regras()
        snapshot = _snapshot(observed_rules_hash="x", turn_count=20)
        primeiro = await check(snapshot, layer=layer)
        segundo = await check(snapshot, layer=layer)
        assert primeiro.model_dump() == segundo.model_dump()

    @pytest.mark.asyncio
    async def test_deterministico_hash_igualdade_de_conjunto(self) -> None:
        assert compute_rules_hash([REGRA_CRITICA, REGRA_IMPORTANTE]) == (
            compute_rules_hash([REGRA_IMPORTANTE, REGRA_CRITICA])
        )
        layer = await _layer_com_regras()
        report = await check(_snapshot(), layer=layer)
        assert report.rules.expected_hash == _hash_esperado()

    @pytest.mark.asyncio
    async def test_fail_safe_quando_verificar_e_impossivel(self) -> None:
        layer = SemanticLayer(StoreQueFalha())  # type: ignore[arg-type]
        report = await check(_snapshot(), layer=layer)
        assert report.rules.status == "indisponivel"
        assert report.fail_safe is True
        assert report.rules.by_type.critical.status == "indisponivel"
        assert report.rules.by_type.important.status == "indisponivel"
        assert report.rules.by_type.soft.status == "indisponivel"

    def test_zero_conhecimento_de_runtime_no_codigo_fonte(self) -> None:
        """Invariante 5: nenhum arquivo do modulo nomeia runtime algum."""
        raiz = (
            Path(__file__).resolve().parents[2]
            / "src" / "orkmind" / "guardrails"
        )
        proibido = re.compile(r"(?i)\b(hermes|openclaw|ork)\b")
        for arquivo in sorted(raiz.glob("*.py")):
            fonte = arquivo.read_text(encoding="utf-8")
            achado = proibido.search(fonte)
            assert achado is None, (
                f"{arquivo.name} nomeia runtime: '{achado.group(0)}'"
                if achado else ""
            )


class TestParidadeComPlugin:
    """A extracao preserva as primitivas usadas pelo plugin."""

    def test_hash_identico_ao_do_provider(self) -> None:
        from orkmind.hermes.provider import compute_rules_hash as ph
        contents = [REGRA_CRITICA, REGRA_IMPORTANTE]
        assert ph(contents) == compute_rules_hash(contents)

    def test_fallback_fail_safe_identico_ao_do_plugin(self) -> None:
        from orkmind.guardrails import FALLBACK_NO_RULES
        from tests.unit.test_hermes_plugin import plugin
        assert plugin._FALLBACK_NO_RULES == FALLBACK_NO_RULES


class TestSinalDeJanela:
    """G2: advisory agnostico com fonte declarada."""

    @pytest.mark.asyncio
    async def test_usage_informado_tem_precedencia(self) -> None:
        layer = await _nova_layer()
        report = await check(
            _snapshot(context_usage_pct=0.9, estimated_tokens=10),
            layer=layer,
        )
        assert report.session.usage_pct == 0.9
        assert report.session.usage_source == "informado"
        assert report.session.advisory == "rotate_now"

    @pytest.mark.asyncio
    async def test_heuristica_por_tokens_estimados(self) -> None:
        layer = await _nova_layer()
        report = await check(
            _snapshot(estimated_tokens=140_000), layer=layer
        )
        assert report.session.usage_source == "heuristica"
        assert report.session.usage_pct == pytest.approx(0.7)
        assert report.session.advisory == "rotate_soon"

    @pytest.mark.asyncio
    async def test_sem_dados_o_uso_e_declarado_indisponivel(self) -> None:
        layer = await _nova_layer()
        report = await check(_snapshot(), layer=layer)
        assert report.session.usage_pct is None
        assert report.session.usage_source == "indisponivel"
        assert report.session.advisory == "none"

    @pytest.mark.asyncio
    async def test_limiares_default_065_e_085(self) -> None:
        layer = await _nova_layer()
        soon = await check(_snapshot(context_usage_pct=0.66), layer=layer)
        now = await check(_snapshot(context_usage_pct=0.86), layer=layer)
        baixo = await check(_snapshot(context_usage_pct=0.5), layer=layer)
        assert soon.session.advisory == "rotate_soon"
        assert now.session.advisory == "rotate_now"
        assert baixo.session.advisory == "none"

    @pytest.mark.asyncio
    async def test_limiares_configuraveis(self) -> None:
        layer = await _nova_layer()
        settings = GuardrailSettings(rotate_soon=0.3, rotate_now=0.5)
        report = await check(
            _snapshot(context_usage_pct=0.4), layer=layer, settings=settings
        )
        assert report.session.advisory == "rotate_soon"

    @pytest.mark.asyncio
    async def test_modo_adaptativo_preservado(self) -> None:
        layer = await _nova_layer()
        settings = GuardrailSettings(adaptive_mode=True)
        assert settings.limiar_rotate_soon() == pytest.approx(0.60)
        report = await check(
            _snapshot(context_usage_pct=0.62), layer=layer, settings=settings
        )
        assert report.session.advisory == "rotate_soon"

    def test_texto_do_advisory_sem_promessa_falsa(self) -> None:
        bloco = gsession.build_advisory_block(0.7, 0.65, "informado")
        assert "preparara automaticamente" not in bloco
        assert "Quando o runtime decidir rotacionar" in bloco
        assert "fonte: informada pelo runtime" in bloco

    def test_advisory_rotate_now_tem_texto_proprio(self) -> None:
        bloco = gsession.build_advisory_block(
            0.9, 0.85, "heuristica", advisory="rotate_now"
        )
        assert "Limite de Contexto Critico" in bloco
        assert "fonte: heuristica interna" in bloco

    @pytest.mark.asyncio
    async def test_handoff_content_available_sem_seed_e_false(self) -> None:
        layer = await _layer_com_regras()
        report = await check(_snapshot(), layer=layer)
        assert report.session.handoff_content_available is False


class TestContratoSerializavel:
    @pytest.mark.asyncio
    async def test_report_serializa_com_as_chaves_do_contrato(self) -> None:
        layer = await _layer_com_regras()
        report = await check(_snapshot(), layer=layer)
        assert isinstance(report, GuardrailReport)
        dados = report.model_dump()
        assert set(dados) == {"rules", "session", "fail_safe"}
        assert set(dados["rules"]) == {"status", "expected_hash", "by_type"}
        assert set(dados["rules"]["by_type"]) == {
            "critical", "important", "soft"
        }
        assert set(dados["session"]) == {
            "usage_pct", "usage_source", "advisory",
            "handoff_content_available",
        }
