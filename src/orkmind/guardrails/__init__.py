"""orkmind.guardrails: auditoria de regras por tipo e sinal de janela.

Principio: o OrkMind governa CONTEUDO e emite SINAL; o runtime executa
PROCESSO. Este modulo audita a presenca das regras governadas numa
sessao (por tipo: critica, importante, soft), sinaliza a proximidade do
limite de contexto e valida/armazena handoff de conteudo. Ele NUNCA
controla o handoff de runtime nenhum: nao rotaciona, nao encerra nem
cria sessao, nao chama runtime.

Consumo: qualquer runtime preenche um SessionSnapshot (dados puros) e
recebe um GuardrailReport. Nenhuma linha aqui conhece runtime algum.
"""

from orkmind.guardrails.engine import (
    SOFT_COLLECTIONS,
    GuardrailSettings,
    check,
)
from orkmind.guardrails.handoff import (
    MAX_REFACOES,
    SECOES_OBRIGATORIAS_DEFAULT,
    SITUACAO_HANDOFF_RULES,
    HandoffValidation,
    handoff_para_sessao,
    submeter_handoff,
    validar_payload,
)
from orkmind.guardrails.models import (
    Advisory,
    GuardrailReport,
    RulesByType,
    RulesReport,
    RulesStatus,
    SessionReport,
    SessionSnapshot,
    TypeReport,
    UsageSource,
)
from orkmind.guardrails.rules import (
    FALLBACK_NO_RULES,
    compute_rules_hash,
    filtrar_regras_seguras,
    format_rules_block,
    format_rules_reinforcement,
    formatar_bloco_reinjecao,
    rules_hash_from_dicts,
)

__all__ = [
    "Advisory",
    "FALLBACK_NO_RULES",
    "GuardrailReport",
    "GuardrailSettings",
    "HandoffValidation",
    "MAX_REFACOES",
    "SECOES_OBRIGATORIAS_DEFAULT",
    "SITUACAO_HANDOFF_RULES",
    "handoff_para_sessao",
    "submeter_handoff",
    "validar_payload",
    "RulesByType",
    "RulesReport",
    "RulesStatus",
    "SOFT_COLLECTIONS",
    "SessionReport",
    "SessionSnapshot",
    "TypeReport",
    "UsageSource",
    "check",
    "compute_rules_hash",
    "filtrar_regras_seguras",
    "format_rules_block",
    "format_rules_reinforcement",
    "formatar_bloco_reinjecao",
    "rules_hash_from_dicts",
]
