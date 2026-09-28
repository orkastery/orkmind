"""Motor do guardrail: check(SessionSnapshot) -> GuardrailReport (G1).

Invariantes do contrato v0 (todos testados):
1. Read-only: nenhuma escrita no store, nenhum side effect alem de log.
2. Idempotente: mesmo snapshot e mesmo estado do store, mesmo report.
3. Deterministico para regras criticas: hash e igualdade de conjunto,
   sem LLM.
4. Fail-safe, nunca fail-open: impossibilidade de verificar vira
   status `indisponivel` com fail_safe=True.
5. Zero conhecimento de runtime: nenhum import, campo ou default
   nomeia runtime algum.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from orkmind.guardrails.handoff import SITUACAO_HANDOFF_RULES
from orkmind.guardrails.models import (
    GuardrailReport,
    RulesByType,
    RulesReport,
    RulesStatus,
    SessionReport,
    SessionSnapshot,
    TypeReport,
)
from orkmind.guardrails.rules import (
    formatar_bloco_reinjecao,
    rules_hash_from_dicts,
)
from orkmind.guardrails.session import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_ROTATE_NOW,
    DEFAULT_ROTATE_SOON,
    adaptive_rotate_soon,
    resolve_advisory,
    resolve_usage,
)

if TYPE_CHECKING:
    from orkmind.core.models import MemoryEntry
    from orkmind.core.semantic_layer import SemanticLayer

logger = logging.getLogger(__name__)

# Colecoes soft: as unicas onde extracao automatica pode escrever (D9).
# A garantia do guardrail para elas e VISIBILIDADE (manifesto), nao
# presenca obrigatoria no contexto.
SOFT_COLLECTIONS = ("fact", "learning", "preference", "content")

# Espelho do intervalo D-MA8: a partir deste numero de turnos, o report
# emite o hint de recall devido para regras importantes.
RECALL_HINT_TURNS = 10


@dataclass
class GuardrailSettings:
    """Parametros do guardrail, com os defaults do roadmap (0.65/0.85).

    `from_config` le a secao [session] do config.toml via OrkMindConfig;
    passar uma instancia explicita dispensa qualquer leitura de disco.
    """

    rotate_soon: float = DEFAULT_ROTATE_SOON
    rotate_now: float = DEFAULT_ROTATE_NOW
    adaptive_mode: bool = False
    context_window: int = DEFAULT_CONTEXT_WINDOW

    @classmethod
    def from_config(cls, config: Any) -> "GuardrailSettings":
        """Constroi a partir de um OrkMindConfig (campos session_*)."""
        return cls(
            rotate_soon=getattr(config, "session_rotate_soon", DEFAULT_ROTATE_SOON),
            rotate_now=getattr(config, "session_rotate_now", DEFAULT_ROTATE_NOW),
            adaptive_mode=getattr(config, "session_adaptive_mode", False),
            context_window=getattr(
                config, "session_context_window", DEFAULT_CONTEXT_WINDOW
            ),
        )

    def limiar_rotate_soon(self) -> float:
        """Limiar de aviso efetivo (modo adaptativo P6 preservado)."""
        if self.adaptive_mode:
            return adaptive_rotate_soon()
        return self.rotate_soon


def _entry_para_dict(entry: "MemoryEntry") -> dict[str, Any]:
    """Projecao minima usada pelas funcoes puras de formatacao."""
    return {
        "content": entry.content,
        "collection": entry.collection,
        "priority": entry.priority,
        "tags": entry.tags,
    }


def _status_das_regras(
    expected_hash: str, observed_hash: Optional[str]
) -> RulesStatus:
    """Igualdade de conjunto via hash D-MA10, sem LLM (invariante 3)."""
    if not expected_hash:
        return "ok" if not observed_hash else "desatualizado"
    if not observed_hash:
        return "ausente"
    if observed_hash == expected_hash:
        return "ok"
    return "desatualizado"


def _hint_recall(snapshot: SessionSnapshot, importantes: int) -> Optional[str]:
    """Hint de processo para regras importantes (deterministico)."""
    if importantes <= 0:
        return None
    turnos = snapshot.turn_count
    if turnos is None or turnos < RECALL_HINT_TURNS:
        return None
    return (
        f"recall devido: {turnos} turnos de sessao. Ha {importantes} "
        "regra(s) importante(s) governada(s); execute um recall/prefetch "
        "com as tags da sessao para garantir a relevancia no contexto."
    )


async def _contar_soft(layer: "SemanticLayer") -> int:
    """Contagem de entries nas colecoes soft (visibilidade, D-MA3)."""
    total = 0
    for collection in SOFT_COLLECTIONS:
        total += await layer.store.count(collection=collection)
    return total


async def _handoff_rules_presentes(
    layer: "SemanticLayer", requester_id: Optional[str]
) -> bool:
    """True quando ha handoff-rules governadas visiveis para a sessao."""
    try:
        entries = await layer.store.search_by_tags(
            tags={"situation": [SITUACAO_HANDOFF_RULES]},
            collection="rule",
            requester_id=requester_id,
        )
        return bool(entries)
    except Exception as e:
        logger.debug("Falha ao verificar handoff-rules: %s", e)
        return False


def _report_indisponivel(session: SessionReport) -> GuardrailReport:
    """Fail-safe (invariante 4): verificar foi impossivel, nunca operar."""
    tipo = TypeReport(status="indisponivel", count=0)
    return GuardrailReport(
        rules=RulesReport(
            status="indisponivel",
            expected_hash=None,
            by_type=RulesByType(
                critical=tipo.model_copy(),
                important=tipo.model_copy(),
                soft=tipo.model_copy(),
            ),
        ),
        session=session,
        fail_safe=True,
    )


async def check(
    snapshot: SessionSnapshot,
    layer: "SemanticLayer",
    settings: Optional[GuardrailSettings] = None,
) -> GuardrailReport:
    """Audita a presenca das regras governadas numa sessao.

    Read-only por contrato: o unico acesso ao store e de leitura. Quem
    aplica a reinjecao, decide rotacao ou dispara handoff e o runtime.
    """
    cfg = settings or GuardrailSettings()

    usage, usage_source = resolve_usage(
        snapshot.context_usage_pct,
        snapshot.estimated_tokens,
        snapshot.turn_count,
        context_window=cfg.context_window,
    )
    advisory = resolve_advisory(
        usage, cfg.limiar_rotate_soon(), cfg.rotate_now
    )

    requester = snapshot.requester_id or None
    session = SessionReport(
        usage_pct=usage,
        usage_source=usage_source,
        advisory=advisory,
        handoff_content_available=False,
    )

    try:
        entries = await layer.get_mandatory_rules(requester_id=requester)
    except Exception as e:
        logger.warning(
            "Guardrail: impossivel verificar regras (fail-safe): %s", e
        )
        return _report_indisponivel(session)

    # Defesa em profundidade: regra sob suspeita nao conta como ativa.
    seguras = [e for e in entries if not e.injection_risk and not e.conflict]
    criticas = [e for e in seguras if e.priority == "critical"]
    importantes = [e for e in seguras if e.priority != "critical"]

    expected_hash = rules_hash_from_dicts([_entry_para_dict(e) for e in seguras])
    status = _status_das_regras(expected_hash, snapshot.observed_rules_hash)

    # Bloco de reinjecao pronto quando o contexto esta sem as regras ou
    # com um conjunto desatualizado. Inclui TODAS as mandatorias seguras
    # (o bloco D-MA1 e um so); o count reflete apenas as criticas.
    reinjecao: Optional[str] = None
    if status in ("ausente", "desatualizado") and seguras:
        reinjecao = formatar_bloco_reinjecao(
            [_entry_para_dict(e) for e in seguras]
        )

    try:
        soft_count = await _contar_soft(layer)
        soft = TypeReport(status="ok", count=soft_count)
    except Exception as e:
        logger.debug("Guardrail: contagem soft indisponivel: %s", e)
        soft = TypeReport(status="indisponivel", count=0)

    session.handoff_content_available = await _handoff_rules_presentes(
        layer, requester
    )

    return GuardrailReport(
        rules=RulesReport(
            status=status,
            expected_hash=expected_hash or None,
            by_type=RulesByType(
                critical=TypeReport(
                    status=status,
                    count=len(criticas),
                    reinjection_block=reinjecao,
                ),
                important=TypeReport(
                    status=status,
                    count=len(importantes),
                    hint=_hint_recall(snapshot, len(importantes)),
                ),
                soft=soft,
            ),
        ),
        session=session,
        fail_safe=status != "ok",
    )
