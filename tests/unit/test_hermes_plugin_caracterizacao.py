"""Testes de caracterizacao do plugin Hermes (pre-refactor G1).

Congelam BYTE A BYTE a saida atual do plugin antes da extracao do modulo
orkmind.guardrails. O refactor do G1 (plugin vira consumidor do modulo)
so e valido se estes testes continuarem verdes sem alteracao.

Excecao documentada: o texto do advisory D-MA11 sera corrigido de
proposito na fase G2 (remocao da promessa de handoff automatico). O
teste do advisory sera atualizado NAQUELE commit, junto com a mudanca.
"""

from __future__ import annotations

import hashlib
from typing import Any

from tests.unit.test_hermes_plugin import (
    FakeBackend,
    make_provider,
    plugin,
)

REGRA_CRITICA = "Nunca apagar memorias sem aprovacao humana."
REGRA_ALTA = "Rodar a suite de testes antes de qualquer commit."


def _regras_padrao() -> list[dict[str, Any]]:
    return [
        {
            "id": "r1",
            "content": REGRA_CRITICA,
            "collection": "rule",
            "tags": {"domain": ["governance"]},
            "priority": "critical",
            "mandatory": True,
            "injection_risk": False,
            "conflict": False,
        },
        {
            "id": "r2",
            "content": REGRA_ALTA,
            "collection": "instruction",
            "tags": {"domain": ["engenharia"]},
            "priority": "high",
            "mandatory": True,
            "injection_risk": False,
            "conflict": False,
        },
    ]


def _hash_esperado() -> str:
    contents = sorted([REGRA_CRITICA, REGRA_ALTA])
    return hashlib.sha256("|".join(contents).encode("utf-8")).hexdigest()


BLOCO_REGRAS_ESPERADO = (
    "\n"
    "---\n"
    "## REGRAS MANDATORIAS (OBEDECER SEMPRE)\n"
    "\n"
    "### DOMINIO: ENGENHARIA\n"
    f"- [MANDATORIA] [instruction] {REGRA_ALTA}\n"
    "\n"
    "### DOMINIO: GOVERNANCE\n"
    f"- [CRITICA] [rule] {REGRA_CRITICA}\n"
    "\n"
    "---\n"
)

FAIL_SAFE_ESPERADO = (
    "\n## ALERTA: Regras de Governanca Indisponiveis\n"
    "O OrkMind nao conseguiu carregar as regras mandatorias.\n"
    "NAO prossiga com nenhuma operacao critica ate que as\n"
    "regras estejam disponiveis. Tente reconectar via orkmind_rules.\n"
)


class TestCaracterizacaoSystemPrompt:
    """Saida integral do system_prompt_block, byte a byte."""

    def test_bloco_de_regras_byte_a_byte(self) -> None:
        provider = make_provider(FakeBackend(rules=_regras_padrao()))
        provider._token_budget = 4000
        bloco = provider._format_rules_block(_regras_padrao())
        assert bloco == BLOCO_REGRAS_ESPERADO

    def test_system_prompt_completo_byte_a_byte(self) -> None:
        provider = make_provider(FakeBackend(rules=_regras_padrao()))
        provider._token_budget = 4000
        esperado = (
            plugin._SYSTEM_PROMPT_HEADER
            + BLOCO_REGRAS_ESPERADO
            + plugin._GUARDRAIL_ANTI_DESTRUICAO
        )
        assert provider.system_prompt_block() == esperado

    def test_fail_safe_byte_a_byte(self) -> None:
        backend = FakeBackend(rules=_regras_padrao())
        provider = make_provider(backend)
        provider.system_prompt_block()
        backend.fail = True
        esperado = (
            plugin._SYSTEM_PROMPT_HEADER
            + FAIL_SAFE_ESPERADO
            + plugin._GUARDRAIL_ANTI_DESTRUICAO
        )
        assert provider.system_prompt_block() == esperado


class TestCaracterizacaoPrefetch:
    """Saida integral do prefetch, byte a byte."""

    def _provider(self) -> Any:
        backend = FakeBackend(
            rules=_regras_padrao(),
            memories=[
                {
                    "id": "m1",
                    "collection": "fact",
                    "content": "O deploy usa canary.",
                    "priority": "high",
                    "tags": {"domain": ["deploy"]},
                }
            ],
            stats={"fact": 2, "rule": 1},
        )
        provider = make_provider(backend)
        provider._token_budget = 4000
        provider._adaptive_mode = False
        provider._rotation_threshold_cfg = 0.65
        provider._rotate_now_threshold_cfg = 0.85
        provider.system_prompt_block()
        return provider

    def test_prefetch_turno_um_byte_a_byte(self) -> None:
        provider = self._provider()
        saida = provider.prefetch("como esta o deploy?", context_usage_pct=0.1)
        esperado = (
            "## OrkMind - Contexto Relevante\n"
            "- [HIGH] [fact] (domain:deploy) O deploy usa canary.\n"
            "\n"
            "## OrkMind - Memorias Disponiveis (busque sob demanda)\n"
            "- fact: 2 entries\n"
            "- rule: 1 entries\n"
            "Use orkmind_recall ou orkmind_search para acessar qualquer "
            "informacao. NUNCA assuma que informacao ausente do contexto "
            "nao existe.\n"
            "\n"
            "## REGRAS ATIVAS (obedecer SEMPRE)\n"
            f"- {REGRA_CRITICA}\n"
            f"- {REGRA_ALTA}\n"
            "(Regras completas no system prompt. Duvida? Use orkmind_rules.)\n"
            f"[OrkMind rules_hash: sha256:{_hash_esperado()}]"
        )
        assert saida == esperado

    def test_lembrete_periodico_byte_a_byte(self) -> None:
        provider = self._provider()
        provider._turn_count = plugin.REINFORCEMENT_INTERVAL - 1
        saida = provider.prefetch("seguindo", context_usage_pct=0.1)
        esperado = (
            "\n## LEMBRETE DE CONFORMIDADE (turno 10)\n"
            "As regras mandatorias continuam ativas e devem ser obedecidas. "
            "Consulte orkmind_rules se precisar do texto completo."
        )
        assert esperado in saida

    def test_advisory_byte_a_byte(self) -> None:
        """Texto do advisory D-MA11 corrigido na fase G2.

        Mudanca DELIBERADA de caracterizacao: a promessa antiga "o
        OrkMind preparara automaticamente o handoff" nao correspondia a
        comportamento nenhum e foi removida. O texto novo declara a
        fonte da medicao e informa o que o OrkMind fornece quando o
        runtime decide rotacionar.
        """
        provider = self._provider()
        saida = provider.prefetch("pergunta", context_usage_pct=0.7)
        esperado = (
            "\n## ADVISORY: Proximidade do Limite de Contexto\n"
            "Uso estimado: 70% (fonte: informada pelo runtime; limiar: 65%). "
            "Considere concluir o trabalho em andamento e criar uma sessao "
            "limpa para manter conformidade total com as regras. Quando o "
            "runtime decidir rotacionar, o OrkMind fornece as regras "
            "mandatorias ativas, a validacao e o armazenamento do pacote de "
            "handoff (orkmind_handoff) e a injecao do resumo na sessao nova."
        )
        assert esperado in saida
        assert "preparara automaticamente" not in saida

    def test_advisory_rotate_now_byte_a_byte(self) -> None:
        """G2: acima do limiar de urgencia o advisory muda de tom."""
        provider = self._provider()
        saida = provider.prefetch("pergunta", context_usage_pct=0.9)
        esperado = (
            "\n## ADVISORY: Limite de Contexto Critico\n"
            "Uso estimado: 90% (fonte: informada pelo runtime; "
            "limiar critico: 85%). "
            "Conclua apenas o passo em andamento e crie uma sessao limpa "
            "assim que possivel. Quando o runtime decidir rotacionar, o "
            "OrkMind fornece as regras mandatorias ativas, a validacao e o "
            "armazenamento do pacote de handoff (orkmind_handoff) e a "
            "injecao do resumo na sessao nova."
        )
        assert esperado in saida


class TestCaracterizacaoHash:
    """D-MA10: o hash e igualdade de conjunto, sem LLM."""

    def test_hash_byte_a_byte(self) -> None:
        provider = make_provider()
        assert provider._compute_rules_hash(_regras_padrao()) == _hash_esperado()
