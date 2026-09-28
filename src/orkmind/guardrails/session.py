"""Sinal de janela de contexto agnostico de runtime (D-MA11 movido, G2).

O OrkMind SINALIZA a proximidade do limite de contexto; quem decide e
executa a rotacao e sempre o runtime (ou um orquestrador com gate
proprio, que pode simplesmente ignorar o advisory). Nada aqui dispara
acao nenhuma.

Fonte da medicao sempre declarada (D-MA11 corrigido):
- `informado`: o runtime passou context_usage_pct e ele prevalece.
- `heuristica`: estimativa interna por tokens acumulados e/ou turnos.
- `indisponivel`: nenhum dado para estimar; advisory fica em `none`.
"""

from __future__ import annotations

from typing import Optional

from orkmind.guardrails.models import Advisory, UsageSource

# Limiar default de AVISO (rotate_soon). Configuravel em
# ~/.orkmind/config.toml, secao [session], chave rotation_threshold.
DEFAULT_ROTATE_SOON = 0.65

# Limiar default de URGENCIA (rotate_now). Configuravel em [session],
# chave rotate_now_threshold.
DEFAULT_ROTATE_NOW = 0.85

# Janela de contexto assumida quando o runtime nao informa (heuristica).
DEFAULT_CONTEXT_WINDOW = 200_000

# Heuristica de fallback por contagem de turnos.
MAX_TURNS_ESTIMATE = 90

# Modo adaptativo (P6): o limiar de aviso deriva da fracao reservada as
# regras mandatorias mais uma margem de seguranca, dentro desta faixa.
MANDATORY_BUDGET_RATIO = 0.3
ADAPTIVE_RANGE = (0.60, 0.80)
ADAPTIVE_SAFETY_MARGIN = 0.10


def clamp_pct(valor: float) -> float:
    """Limita uma fracao ao intervalo [0.0, 1.0]."""
    return min(max(float(valor), 0.0), 1.0)


def resolve_usage(
    context_usage_pct: Optional[float],
    estimated_tokens: Optional[int],
    turn_count: Optional[int],
    context_window: int = DEFAULT_CONTEXT_WINDOW,
) -> tuple[Optional[float], UsageSource]:
    """Fracao de uso da janela e a fonte da medicao.

    O valor informado pelo runtime prevalece sobre a heuristica interna
    (Q1). Sem nenhum dado, o uso e declarado indisponivel: o guardrail
    nunca inventa um numero.
    """
    if context_usage_pct is not None:
        try:
            return clamp_pct(context_usage_pct), "informado"
        except (TypeError, ValueError):
            pass

    if estimated_tokens is None and turn_count is None:
        return None, "indisponivel"

    by_tokens = (estimated_tokens or 0) / max(int(context_window), 1)
    by_turns = (turn_count or 0) / MAX_TURNS_ESTIMATE
    return clamp_pct(max(by_tokens, by_turns)), "heuristica"


def adaptive_rotate_soon() -> float:
    """Limiar de aviso derivado no modo adaptativo (P6)."""
    derived = 1.0 - (MANDATORY_BUDGET_RATIO + ADAPTIVE_SAFETY_MARGIN)
    return min(max(derived, ADAPTIVE_RANGE[0]), ADAPTIVE_RANGE[1])


def resolve_advisory(
    usage: Optional[float],
    rotate_soon: float = DEFAULT_ROTATE_SOON,
    rotate_now: float = DEFAULT_ROTATE_NOW,
) -> Advisory:
    """Advisory a partir do uso medido: none | rotate_soon | rotate_now."""
    if usage is None:
        return "none"
    if usage >= rotate_now:
        return "rotate_now"
    if usage >= rotate_soon:
        return "rotate_soon"
    return "none"


_FONTE_LEGIVEL = {
    "informado": "informada pelo runtime",
    "heuristica": "heuristica interna",
    "indisponivel": "indisponivel",
}

# O que o OrkMind de fato fornece quando o RUNTIME decide rotacionar.
# Substitui a promessa antiga "o OrkMind preparara automaticamente o
# handoff", que nao correspondia a comportamento nenhum.
_OFERTA_HANDOFF = (
    "Quando o runtime decidir rotacionar, o OrkMind fornece as regras "
    "mandatorias ativas, a validacao e o armazenamento do pacote de "
    "handoff (orkmind_handoff) e a injecao do resumo na sessao nova."
)


def build_advisory_block(
    usage: float,
    threshold: float,
    source: UsageSource = "heuristica",
    advisory: Advisory = "rotate_soon",
) -> str:
    """Bloco de advisory injetavel em contexto (texto corrigido, G2)."""
    fonte = _FONTE_LEGIVEL.get(source, source)
    if advisory == "rotate_now":
        return (
            "\n## ADVISORY: Limite de Contexto Critico\n"
            f"Uso estimado: {usage * 100:.0f}% "
            f"(fonte: {fonte}; limiar critico: {threshold * 100:.0f}%). "
            "Conclua apenas o passo em andamento e crie uma sessao limpa "
            "assim que possivel. " + _OFERTA_HANDOFF
        )
    return (
        "\n## ADVISORY: Proximidade do Limite de Contexto\n"
        f"Uso estimado: {usage * 100:.0f}% "
        f"(fonte: {fonte}; limiar: {threshold * 100:.0f}%). "
        "Considere concluir o trabalho em andamento e criar uma sessao "
        "limpa para manter conformidade total com as regras. "
        + _OFERTA_HANDOFF
    )
