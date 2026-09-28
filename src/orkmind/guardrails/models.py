"""Contrato v0 do guardrail: SessionSnapshot e GuardrailReport.

Dados puros dos dois lados. O snapshot descreve o que o RUNTIME observa
na sessao; o report descreve o que o OrkMind constata sobre o CONTEUDO
governado. Nenhum campo, default ou import aqui nomeia runtime algum:
qualquer consumidor que preencha o snapshot recebe o mesmo report.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# Status de verificacao de regras (por tipo e agregado).
RulesStatus = Literal["ok", "ausente", "desatualizado", "indisponivel"]

# Fonte da medicao de uso da janela de contexto (D-MA11 corrigido).
UsageSource = Literal["informado", "heuristica", "indisponivel"]

# Sinal de janela: o OrkMind SINALIZA, o runtime decide e executa.
Advisory = Literal["none", "rotate_soon", "rotate_now"]


class SessionSnapshot(BaseModel):
    """Entrada do guardrail: o que o runtime observa da sessao.

    Dados puros, nada de runtime. `observed_rules_hash` e o hash D-MA10
    do bloco de regras presente no contexto da sessao (ou None quando o
    runtime nao tem essa informacao). `context_usage_pct` informado pelo
    runtime tem precedencia sobre a heuristica interna.
    """

    session_id: str
    requester_id: str = ""
    session_tags: dict[str, list[str]] = Field(default_factory=dict)
    observed_rules_hash: Optional[str] = None
    context_usage_pct: Optional[float] = None
    turn_count: Optional[int] = None
    estimated_tokens: Optional[int] = None


class TypeReport(BaseModel):
    """Verificacao de um tipo de regra (critical/important/soft)."""

    status: RulesStatus
    count: int = 0
    # Regras criticas: bloco D-MA7 pronto para reinjecao (E3 integral).
    reinjection_block: Optional[str] = None
    # Regras importantes: hint de processo (ex.: recall devido).
    hint: Optional[str] = None


class RulesByType(BaseModel):
    """Verificacao por tipo: critica, importante, soft."""

    critical: TypeReport
    important: TypeReport
    soft: TypeReport


class RulesReport(BaseModel):
    """Estado agregado das regras governadas na sessao."""

    status: RulesStatus
    expected_hash: Optional[str] = None
    by_type: RulesByType


class SessionReport(BaseModel):
    """Sinal de janela e disponibilidade de handoff de conteudo."""

    usage_pct: Optional[float] = None
    usage_source: UsageSource = "indisponivel"
    advisory: Advisory = "none"
    handoff_content_available: bool = False


class GuardrailReport(BaseModel):
    """Saida do guardrail.

    fail_safe=True significa: nao operar a sessao sem tratar `rules`.
    """

    rules: RulesReport
    session: SessionReport
    fail_safe: bool = False
