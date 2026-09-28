"""Servidor MCP do OrkMind - transporte stdio para Claude Code."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from orkmind.core.config import load_config
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.mcp.tools import (
    orkmind_add,
    orkmind_delete,
    orkmind_get_rules,
    orkmind_guardrail_check,
    orkmind_handoff,
    orkmind_list,
    orkmind_query,
    orkmind_stats,
    orkmind_update,
)
from orkmind.store.factory import create_embedder, create_store

logger = logging.getLogger(__name__)


def _build_tools() -> list[Tool]:
    """Retorna a lista das 9 tools expostas pelo servidor."""
    return [
        Tool(name='orkmind_brain', description='Versioned exact Company Brain API; authenticated human context required for interactive reading.',
             inputSchema={'type':'object','required':['request'],'additionalProperties':False,'properties':{'request':{'type':'object'}}}),
        Tool(
            name="orkmind_query",
            description=(
                "Query OrkMind memories for the current context. "
                "Returns relevant memories sorted by priority, "
                "including all mandatory rules."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "object",
                        "description": "Tag filters: {dimension: [values]}",
                    },
                    "collection": {"type": "string"},
                    "conversation": {
                        "type": "string",
                        "description": "Current conversation text for context detection",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths touched for context detection",
                    },
                    "token_budget": {"type": "integer"},
                },
            },
        ),
        Tool(
            name="orkmind_get_rules",
            description="Get all mandatory rules matching the given tags.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tags": {"type": "object"},
                    "collection": {"type": "string"},
                },
            },
        ),
        Tool(
            name="orkmind_add",
            description="Add a new memory entry to OrkMind.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Memory content"},
                    "collection": {
                        "type": "string",
                        "enum": [
                            "rule", "instruction", "fact", "learning",
                            "preference", "decision", "content", "agenda",
                            "contacts", "handoff", "roadmap", "files",
                            "docs", "dags", "tools", "users",
                        ],
                    },
                    "tags": {"type": "object"},
                    "priority": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "mandatory": {"type": "boolean"},
                    "scope": {
                        "type": "string",
                        "enum": ["global", "project", "session"],
                    },
                    "source": {
                        "type": "string",
                        "enum": ["human", "agent", "system", "bootstrap"],
                    },
                    "metadata": {"type": "object"},
                },
                "required": ["content", "collection"],
            },
        ),
        Tool(
            name="orkmind_update",
            description="Update an existing memory entry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string"},
                    "content": {"type": "string"},
                    "collection": {"type": "string"},
                    "tags": {"type": "object"},
                    "priority": {"type": "string"},
                    "mandatory": {"type": "boolean"},
                    "metadata": {"type": "object"},
                },
                "required": ["entry_id"],
            },
        ),
        Tool(
            name="orkmind_delete",
            description="Delete a memory entry by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string"},
                },
                "required": ["entry_id"],
            },
        ),
        Tool(
            name="orkmind_list",
            description="List memory entries, optionally filtered by collection.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        Tool(
            name="orkmind_stats",
            description="Get statistics about the OrkMind memory store.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="orkmind_guardrail_check",
            description=(
                "Audit governed rule presence for a session (read-only). "
                "Takes a session snapshot (observed rules hash, context "
                "usage, turn count) and returns a GuardrailReport: rule "
                "status by type (critical/important/soft), ready-to-use "
                "reinjection block, window advisory with declared usage "
                "source, and fail-safe flag. OrkMind signals; the runtime "
                "decides and executes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Identifier of the audited session",
                    },
                    "requester_id": {
                        "type": "string",
                        "description": "Requester identity for read ACL (F2)",
                    },
                    "session_tags": {
                        "type": "object",
                        "description": "Session tags: {dimension: [values]}",
                    },
                    "observed_rules_hash": {
                        "type": "string",
                        "description": (
                            "D-MA10 hash of the rules block currently "
                            "present in the session context"
                        ),
                    },
                    "context_usage_pct": {
                        "type": "number",
                        "description": (
                            "Context window usage fraction (0.0-1.0) as "
                            "measured by the runtime; takes precedence "
                            "over the internal heuristic"
                        ),
                    },
                    "turn_count": {"type": "integer"},
                    "estimated_tokens": {"type": "integer"},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="orkmind_handoff",
            description=(
                "Validate and store a session content handoff package. "
                "OrkMind never triggers rotation: call this when the "
                "runtime decides to rotate. The payload must cover the "
                "governed handoff-rules sections (default: progresso, "
                "decisoes, referencias_criticas, proximos_passos), each "
                "with minimum content. Invalid payloads return "
                "status='refazer' listing what is missing (up to 2 "
                "retries, then accepted with warnings). Accepted packages "
                "are stored as a handoff entry (summary, "
                "origin/destination) plus the full package in "
                "semantic_log (package_id), both chained to the session "
                "entry; the next session receives the summary and "
                "package_id on its first prefetch."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": (
                            "Handoff package: {section: content}. Extra "
                            "sections are accepted and preserved."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Origin session identifier",
                    },
                    "origin": {"type": "string"},
                    "destination": {
                        "type": "string",
                        "description": (
                            "Destination session, if already known; empty "
                            "means the next session to start consumes it"
                        ),
                    },
                    "refacao": {
                        "type": "integer",
                        "description": "Retry attempts already used",
                    },
                    "requester_id": {"type": "string"},
                },
                "required": ["payload", "session_id"],
            },
        ),
    ]


async def _handle_tool(
    layer: SemanticLayer,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """Despacha a chamada para a funcao de tool correspondente."""
    dispatch = {
        "orkmind_query": lambda: orkmind_query(layer, **arguments),
        "orkmind_get_rules": lambda: orkmind_get_rules(layer, **arguments),
        "orkmind_add": lambda: orkmind_add(layer, **arguments),
        "orkmind_update": lambda: orkmind_update(layer, **arguments),
        "orkmind_delete": lambda: orkmind_delete(layer, **arguments),
        "orkmind_list": lambda: orkmind_list(layer, **arguments),
        "orkmind_stats": lambda: orkmind_stats(layer),
        "orkmind_guardrail_check": lambda: orkmind_guardrail_check(
            layer, **arguments
        ),
        "orkmind_handoff": lambda: orkmind_handoff(layer, **arguments),
    }
    fn = dispatch.get(name)
    if fn is None:
        raise ValueError(f"Tool desconhecida: {name}")
    return await fn()


async def _list_tools_handler(
    _ctx: Any,
    _params: PaginatedRequestParams | None,
) -> ListToolsResult:
    """Handler para tools/list - retorna as 9 tools registradas."""
    return ListToolsResult(tools=_build_tools())


# Referencia ao SemanticLayer compartilhada entre handlers e run_server.
_layer: SemanticLayer | None = None


async def _call_tool_handler(
    _ctx: Any,
    params: CallToolRequestParams,
) -> CallToolResult:
    """Handler para tools/call - despacha a tool solicitada."""
    if params.name == 'orkmind_brain':
        from orkmind.mcp.tools import orkmind_brain
        args=params.arguments or {}
        result = await orkmind_brain(args.get('request')) if set(args)=={'request'} else {'state':'conflict','error':'brain.api.invalid'}
        return CallToolResult(content=[TextContent(type='text',text=json.dumps(result))])
    assert _layer is not None, "SemanticLayer nao inicializado"
    result = await _handle_tool(_layer, params.name, params.arguments or {})
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, default=str))]
    )


async def run_server() -> None:
    """Inicia o servidor MCP do OrkMind via stdio."""
    global _layer  # noqa: PLW0603

    config = load_config()
    store = create_store(config)
    await store.initialize()
    _layer = SemanticLayer(
        store,
        token_budget=config.token_budget,
        embedder=create_embedder(config),
        semantic_enabled=config.search_semantic_enabled,
        rerank_strategy=config.search_rerank_strategy,
    )

    server = Server(
        "orkmind",
        on_list_tools=_list_tools_handler,
        on_call_tool=_call_tool_handler,
    )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )

    await store.close()
