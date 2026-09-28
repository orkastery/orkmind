"""MCP tool definitions for OrkMind."""

from __future__ import annotations


async def orkmind_brain(request):
    """DB-authenticated stdio. A tool argument never establishes a principal."""
    from orkmind.cli.company_brain import service_request
    return service_request(request)

from typing import Any, Optional

from orkmind.core.models import MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer


async def orkmind_query(
    layer: SemanticLayer,
    tags: Optional[dict[str, list[str]]] = None,
    collection: Optional[str] = None,
    conversation: str = "",
    files: Optional[list[str]] = None,
    token_budget: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Query memories for the current context."""
    entries = await layer.query_for_context(
        tags=tags,
        collection=collection,
        conversation=conversation,
        files=files,
        token_budget=token_budget,
    )
    return [_entry_to_dict(e) for e in entries]


async def orkmind_get_rules(
    layer: SemanticLayer,
    tags: Optional[dict[str, list[str]]] = None,
    collection: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Get all mandatory rules matching the given tags."""
    entries = await layer.get_mandatory_rules(tags=tags, collection=collection)
    return [_entry_to_dict(e) for e in entries]


async def orkmind_add(
    layer: SemanticLayer,
    content: str,
    collection: str,
    tags: Optional[dict[str, list[str]]] = None,
    priority: str = "medium",
    mandatory: bool = False,
    scope: str = "global",
    source: str = "human",
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Add a new memory entry."""
    entry = MemoryEntry(
        content=content,
        collection=collection,  # type: ignore[arg-type]
        tags=tags or {},
        priority=priority,  # type: ignore[arg-type]
        mandatory=mandatory,
        scope=scope,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        metadata=metadata or {},
    )
    entry_id, warnings = await layer.add_memory(entry)
    return {"id": entry_id, "warnings": warnings}


async def orkmind_update(
    layer: SemanticLayer,
    entry_id: str,
    content: Optional[str] = None,
    collection: Optional[str] = None,
    tags: Optional[dict[str, list[str]]] = None,
    priority: Optional[str] = None,
    mandatory: Optional[bool] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Update an existing memory entry."""
    existing = await layer.store.retrieve(entry_id)
    if not existing:
        return {"success": False, "error": "Entry not found"}

    if content is not None:
        existing.content = content
    if collection is not None:
        existing.collection = collection  # type: ignore[assignment]
    if tags is not None:
        existing.tags = tags
    if priority is not None:
        existing.priority = priority  # type: ignore[assignment]
    if mandatory is not None:
        existing.mandatory = mandatory
    if metadata is not None:
        existing.metadata = metadata

    success = await layer.store.update(entry_id, existing)
    return {"success": success}


async def orkmind_delete(
    layer: SemanticLayer,
    entry_id: str,
) -> dict[str, Any]:
    """Delete a memory entry."""
    success = await layer.store.delete(entry_id)
    return {"success": success}


async def orkmind_list(
    layer: SemanticLayer,
    collection: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List memory entries, optionally filtered by collection."""
    entries = await layer.store.search_by_tags(
        tags={}, collection=collection, limit=limit
    )
    return [_entry_to_dict(e) for e in entries]


async def orkmind_stats(
    layer: SemanticLayer,
) -> dict[str, Any]:
    """Get statistics about the memory store."""
    collections = await layer.store.list_collections()
    total = await layer.store.count()
    per_collection: dict[str, int] = {}
    for coll in collections:
        per_collection[coll] = await layer.store.count(coll)
    return {
        "total_entries": total,
        "collections": collections,
        "per_collection": per_collection,
    }


async def orkmind_guardrail_check(
    layer: SemanticLayer,
    session_id: str,
    requester_id: str = "",
    session_tags: Optional[dict[str, list[str]]] = None,
    observed_rules_hash: Optional[str] = None,
    context_usage_pct: Optional[float] = None,
    turn_count: Optional[int] = None,
    estimated_tokens: Optional[int] = None,
) -> dict[str, Any]:
    """Audita a presenca das regras governadas numa sessao (G1).

    Read-only: devolve o GuardrailReport (regras por tipo, sinal de
    janela, fail-safe). Quem aplica reinjecao ou rotaciona e o runtime.
    """
    from orkmind.core.config import load_config
    from orkmind.guardrails import GuardrailSettings, SessionSnapshot, check

    snapshot = SessionSnapshot(
        session_id=session_id,
        requester_id=requester_id,
        session_tags=session_tags or {},
        observed_rules_hash=observed_rules_hash,
        context_usage_pct=context_usage_pct,
        turn_count=turn_count,
        estimated_tokens=estimated_tokens,
    )
    report = await check(
        snapshot, layer=layer, settings=GuardrailSettings.from_config(load_config())
    )
    return report.model_dump()


async def orkmind_handoff(
    layer: SemanticLayer,
    payload: dict[str, Any],
    session_id: str,
    origin: Optional[str] = None,
    destination: Optional[str] = None,
    refacao: int = 0,
    requester_id: Optional[str] = None,
) -> dict[str, Any]:
    """Valida e armazena um pacote de handoff de conteudo (G3).

    O OrkMind nunca dispara o handoff: o runtime decide rotacionar e
    submete o payload. Invalido, volta status='refazer' com o que falta
    (ate 2 refacoes); aceito, grava handoff + semantic_log encadeados a
    entry da sessao.
    """
    from orkmind.guardrails import submeter_handoff

    return await submeter_handoff(
        layer,
        session_id=session_id,
        payload=payload,
        requester_id=requester_id,
        origin=origin,
        destination=destination,
        refacao=refacao,
    )


def _entry_to_dict(entry: MemoryEntry) -> dict[str, Any]:
    data = entry.model_dump(exclude={"embedding"})
    data["created_at"] = entry.created_at.isoformat()
    data["updated_at"] = entry.updated_at.isoformat()
    if entry.expires_at:
        data["expires_at"] = entry.expires_at.isoformat()
    return data
