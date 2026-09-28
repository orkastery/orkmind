"""Ontology constants and validation for OrkMind's typed collections."""

from __future__ import annotations

from orkmind.core.injection import compute_content_hash, detect_injection
from orkmind.core.models import VALID_COLLECTIONS, MemoryEntry

VALID_TAG_DIMENSIONS = (
    "skill",
    "agent",
    "domain",
    "project",
    "situation",
    # F1: ontologia expandida
    "person",    # quem e mencionado ou relevante na entry
    "audience",  # quem pode VER a entry (ACL de leitura)
    "editors",   # quem pode ALTERAR a entry (ACL de escrita)
    "prod",      # produto de negócio, separado do tenant físico
    "proj",      # projeto/demanda dentro de um produto
    "init",      # iniciativa dentro de um projeto
)

VALID_PRIORITIES = ("critical", "high", "medium", "low")
VALID_SCOPES = ("global", "project", "session")
VALID_SOURCES = ("human", "agent", "system", "bootstrap")

# Per-collection validation rules
COLLECTION_RULES: dict[str, dict[str, object]] = {
    "rule": {"recommend_mandatory": True},
    "instruction": {},
    "fact": {},
    "learning": {},
    "preference": {},
    "decision": {},
    "content": {},
    "agenda": {},
    "contacts": {},
    "handoff": {"required_metadata": ["origin", "destination"]},
    "roadmap": {},
    "files": {"required_metadata": ["path"]},
    "docs": {},
    "dags": {},
    "tools": {},
    "users": {},
    # F1: ontologia expandida
    "session": {"required_metadata": ["session_id"]},
    "artifact": {
        "required_metadata": ["artifact_type"],
        "valid_artifact_types": [
            "article", "report", "presentation", "digest",
            "commit", "release", "research", "bookmark",
            "document", "prompt_output",
            # artefatos operacionais governaveis
            "skill", "cron", "instruction",
        ],
    },
    "compliance": {
        "required_metadata": ["compliance_type"],
        "valid_compliance_types": [
            "compliance_review",     # revisao aprovada
            "compliance_violation",  # violacao detectada
        ],
    },
    "semantic_log": {"required_metadata": ["package_id"]},
    "product": {"required_metadata": ["entity_schema", "status"]},
    "project": {
        "required_metadata": ["entity_schema", "product_id", "status", "workspace_ids"],
    },
    "initiative": {
        "required_metadata": ["entity_schema", "project_id", "status"],
    },
}


class ValidationError(Exception):
    """Raised when a memory entry fails ontology validation."""


class ProtectionError(Exception):
    """Raised when uma operacao viola a protecao de uma entry."""


PROTECTION_MSG_UPDATE = (
    "Entry '{id}' e protegida (protected=true ou priority=critical). "
    "Apenas humano autenticado pode editar. "
    "Use CLI ou Web com autenticacao para alterar."
)

PROTECTION_MSG_DELETE = (
    "Entry '{id}' e protegida (protected=true ou priority=critical). "
    "Apenas humano autenticado pode deletar. "
    "Use CLI ou Web com autenticacao para remover."
)


PERMISSION_MSG_WRITE = (
    "Perfil '{requester}' nao tem permissao de escrita na entry '{id}'. "
    "Apenas o autor ou perfis listados na dimensao de tag 'editors' "
    "podem alterar ou remover esta entry."
)


def validate_entry(entry: MemoryEntry) -> list[str]:
    """Validate a MemoryEntry against ontology rules.

    Returns a list of warning messages (empty if fully valid).
    Raises ValidationError for hard failures.
    """
    warnings: list[str] = []

    if entry.collection not in VALID_COLLECTIONS:
        raise ValidationError(
            f"Invalid collection '{entry.collection}'. "
            f"Must be one of: {', '.join(VALID_COLLECTIONS)}"
        )

    for dim, values in entry.tags.items():
        if dim not in VALID_TAG_DIMENSIONS:
            raise ValidationError(
                f"Invalid tag dimension '{dim}'. "
                f"Must be one of: {', '.join(VALID_TAG_DIMENSIONS)}"
            )
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValidationError(
                f"Tag dimension '{dim}' must be a list of strings."
            )

    if entry.priority not in VALID_PRIORITIES:
        raise ValidationError(
            f"Invalid priority '{entry.priority}'. "
            f"Must be one of: {', '.join(VALID_PRIORITIES)}"
        )

    rules = COLLECTION_RULES.get(entry.collection, {})

    if rules.get("recommend_mandatory") and not entry.mandatory:
        warnings.append(
            f"Collection '{entry.collection}' recommends mandatory=true."
        )

    required_meta = rules.get("required_metadata")
    if isinstance(required_meta, list):
        for key in required_meta:
            if key not in entry.metadata:
                warnings.append(
                    f"Collection '{entry.collection}' recommends "
                    f"metadata key '{key}'."
                )

    # Validar vocabulario controlado de metadados (F1)
    for meta_key, rule_key in (
        ("artifact_type", "valid_artifact_types"),
        ("compliance_type", "valid_compliance_types"),
    ):
        allowed = rules.get(rule_key)
        value = entry.metadata.get(meta_key)
        if isinstance(allowed, list) and value is not None and value not in allowed:
            warnings.append(
                f"Valor '{value}' nao previsto para '{meta_key}' na colecao "
                f"'{entry.collection}'. Valores conhecidos: {', '.join(allowed)}."
            )

    # Computar content_hash se vazio
    if not entry.content_hash:
        entry.content_hash = compute_content_hash(entry.content)

    # Detectar injection
    is_suspect, injection_reasons = detect_injection(entry.content)
    if is_suspect:
        entry.injection_risk = True
        for reason in injection_reasons:
            warnings.append(f"[injection] {reason}")

    # Guardrail D9: agente nao pode criar regra/instrucao mandatoria
    if (
        entry.source == "agent"
        and entry.collection in ("rule", "instruction")
        and entry.mandatory
    ):
        raise ValidationError(
            "Agente nao pode criar regra ou instrucao mandatoria. "
            "Apenas source='human' pode definir regras criticas."
        )

    return warnings
