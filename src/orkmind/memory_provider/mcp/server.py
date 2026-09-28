"""Servidor MCP do Memory Provider - transporte stdio.

Expoe a Wiki Memory e o historico para qualquer runtime que fale MCP (Claude
Code, OpenClaw, Codex...). Os nomes `memory_search` e `memory_get` seguem o
contrato que o OpenClaw espera de um provedor de memoria: expor nome fora do
contrato ja custou uma investigacao inteira.

IDENTIDADE NAO E ARGUMENTO DE TOOL
----------------------------------
Quem esta falando e resolvido UMA vez, no start, a partir do ambiente:

    ORKMIND_PROVIDER_MCP_USER_ID       quem e (default "mcp")
    ORKMIND_PROVIDER_MCP_ROLE          OPERATIONAL | EXECUTIVE | SYSTEM_ADMIN
    ORKMIND_PROVIDER_MCP_DEPARTMENTS   lista separada por virgula
    ORKMIND_PROVIDER_MCP_READONLY      "1" desliga a tool de escrita

O modelo so consegue ESTREITAR esse escopo (tipo de documento, tags, subarvore
do indice). Se o papel viesse no argumento, bastaria pedir `EXECUTIVE`.

Configure o resto do provider com as variaveis `ORKMIND_PROVIDER_*` de sempre
(`ORKMIND_PROVIDER_DATABASE_URL` e obrigatoria). Rode com:

    python -m orkmind.memory_provider.mcp
"""

from __future__ import annotations

import json
import logging
import os
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

from orkmind.memory_provider.config import ENV_PREFIX, MemoryProviderSettings
from orkmind.memory_provider.mcp import tools
from orkmind.memory_provider.models.schemas import (
    AccessLevel,
    MemoryScopeFilter,
    UserContext,
)
from orkmind.memory_provider.provider import NativeMemoryProvider

logger = logging.getLogger(__name__)

MCP_PREFIX = f"{ENV_PREFIX}MCP_"

_provider: NativeMemoryProvider | None = None
_user: UserContext | None = None
_scope: MemoryScopeFilter | None = None
_readonly = False


def build_user_context() -> UserContext:
    """Identidade do servidor, lida do ambiente. Fail-closed no papel."""
    bruto = os.environ.get(f"{MCP_PREFIX}ROLE", AccessLevel.OPERATIONAL.value).strip().upper()
    try:
        papel = AccessLevel(bruto)
    except ValueError as e:
        validos = ", ".join(n.value for n in AccessLevel)
        raise ValueError(f"{MCP_PREFIX}ROLE invalido: {bruto!r} (use um de: {validos})") from e
    departamentos = [
        d.strip() for d in os.environ.get(f"{MCP_PREFIX}DEPARTMENTS", "").split(",") if d.strip()
    ]
    return UserContext(
        user_id=os.environ.get(f"{MCP_PREFIX}USER_ID", "mcp"),
        role=papel,
        departments=departamentos,
        display_name=os.environ.get(f"{MCP_PREFIX}DISPLAY_NAME") or None,
        channel="mcp",
    )


def build_tools(readonly: bool = False) -> list[Tool]:
    """As tools expostas. `memory_search` e a principal; as outras a apoiam."""
    lista = [
        Tool(
            name="memory_search",
            description=(
                "PRINCIPAL. Busca na memoria documental por significado E por termo exato "
                "(sigla, numero de contrato, codigo) ao mesmo tempo. Use ANTES de responder "
                "qualquer coisa sobre decisoes, politicas, contratos, playbooks ou pesquisas "
                "anteriores. Devolve apenas os trechos relevantes, nao documentos inteiros."
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "description": "A pergunta, em linguagem natural"},
                    "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                    "doc_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filtra por tipo (ex.: playbook, policy, deep_research)",
                    },
                    "index_path": {
                        "type": "string",
                        "description": "Restringe a uma subarvore do indice (DOMINIO/TEMA)",
                    },
                    "max_tokens": {"type": "integer", "description": "Teto de tokens da resposta"},
                },
            },
        ),
        Tool(
            name="memory_get",
            description=(
                "Le um documento INTEIRO, pagina a pagina, quando os trechos do "
                "memory_search nao bastarem. Use `offset` do campo `continua_em` para seguir."
            ),
            input_schema={
                "type": "object",
                "required": ["slug"],
                "additionalProperties": False,
                "properties": {
                    "slug": {"type": "string", "description": "slug ou id do documento"},
                    "offset": {"type": "integer", "default": 0},
                    "max_tokens": {"type": "integer", "default": tools.DEFAULT_PAGE_TOKENS},
                },
            },
        ),
        Tool(
            name="memory_index",
            description=(
                "Mapa da memoria: dominios, temas e documentos visiveis. Barato. "
                "Use para descobrir O QUE existe antes de buscar, ou quando a busca vier vazia."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "description": "Ponto de partida (DOMINIO/TEMA)"},
                    "depth": {"type": "integer", "default": 2, "minimum": 0, "maximum": 2},
                    "pendentes": {
                        "type": "boolean",
                        "default": False,
                        "description": "Inclui o que ja foi citado mas nunca escrito",
                    },
                },
            },
        ),
        Tool(
            name="memory_links",
            description=(
                "A teia de um documento nas duas direcoes: para onde ele aponta e quem "
                "aponta para ele (com o trecho que cita). Use para seguir o raciocinio "
                "entre documentos em vez de buscar de novo."
            ),
            input_schema={
                "type": "object",
                "required": ["slug"],
                "additionalProperties": False,
                "properties": {"slug": {"type": "string"}},
            },
        ),
        Tool(
            name="memory_tags",
            description=(
                "Sem argumento: todas as tags com quantos documentos cada uma tem. "
                "Com `tag`: os documentos dela, incluindo as filhas "
                "(`rede` traz `rede/backbone`)."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tag": {"type": "string"},
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                },
            },
        ),
        Tool(
            name="memory_recall",
            description=(
                "Procura em conversas ANTIGAS desta memoria por texto. Use quando precisar "
                "do que ja foi dito ou decidido antes e que nao esta mais no contexto."
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "session_id": {"type": "string", "description": "Restringe a uma sessao"},
                    "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                },
            },
        ),
        Tool(
            name="memory_read_page",
            description="Continua lendo um turno longo devolvido por memory_recall.",
            input_schema={
                "type": "object",
                "required": ["message_id"],
                "additionalProperties": False,
                "properties": {
                    "message_id": {"type": "string"},
                    "offset": {"type": "integer", "default": 0},
                },
            },
        ),
    ]
    if not readonly:
        lista.append(
            Tool(
                name="memory_remember",
                description=(
                    "Grava um documento na memoria para as proximas sessoes. Use para algo "
                    "durável (decisao, politica, aprendizado consolidado), nao para recado "
                    "de curto prazo. Reingerir o mesmo conteudo nao duplica."
                ),
                input_schema={
                    "type": "object",
                    "required": ["slug", "title", "content"],
                    "additionalProperties": False,
                    "properties": {
                        "slug": {"type": "string", "description": "identificador a-z0-9-_"},
                        "title": {"type": "string"},
                        "content": {"type": "string", "description": "Markdown"},
                        "doc_type": {"type": "string", "default": "note"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "index_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Onde entra no indice (DOMINIO/TEMA)",
                        },
                    },
                },
            )
        )
    return lista


async def dispatch(name: str, arguments: dict[str, Any]) -> Any:
    """Despacha a tool. Nunca recebe identidade pelo argumento - so estreitamento."""
    assert _provider is not None and _user is not None and _scope is not None
    if name == "memory_search":
        return await tools.memory_search(_provider, _scope, **arguments)
    if name == "memory_get":
        return await tools.memory_get(_provider, _scope, **arguments)
    if name == "memory_index":
        return await tools.memory_index(_provider, _scope, **arguments)
    if name == "memory_links":
        return await tools.memory_links(_provider, _scope, **arguments)
    if name == "memory_tags":
        return await tools.memory_tags(_provider, _scope, **arguments)
    if name == "memory_recall":
        return await tools.memory_recall(_provider, _user, **arguments)
    if name == "memory_read_page":
        return await tools.read_page(_provider, **arguments)
    if name == "memory_remember":
        if _readonly:
            raise ValueError("servidor em modo somente leitura")
        return await tools.memory_remember(_provider, _user, **arguments)
    raise ValueError(f"tool desconhecida: {name}")


async def _list_tools_handler(_ctx: Any, _params: PaginatedRequestParams | None) -> ListToolsResult:
    return ListToolsResult(tools=build_tools(_readonly))


async def _call_tool_handler(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
    try:
        resultado = await dispatch(params.name, dict(params.arguments or {}))
    except Exception as e:  # noqa: BLE001 - erro vira resposta, nao derruba o servidor
        logger.exception("tool %s falhou", params.name)
        return CallToolResult(
            content=[TextContent(type="text", text=f"{type(e).__name__}: {e}")],
            is_error=True,
        )
    texto = json.dumps(resultado, ensure_ascii=False, default=str)
    return CallToolResult(content=[TextContent(type="text", text=texto)])


async def run_server() -> None:
    """Sobe o servidor MCP do Memory Provider via stdio."""
    global _provider, _user, _scope, _readonly  # noqa: PLW0603

    _readonly = os.environ.get(f"{MCP_PREFIX}READONLY", "").strip() in ("1", "true", "yes")
    _user = build_user_context()
    # for_user passa pelo ExecutiveScopeGuard: o escopo nasce amarrado a identidade.
    _scope = MemoryScopeFilter.for_user(_user)
    _provider = await NativeMemoryProvider.connect(MemoryProviderSettings.from_env())
    logger.info(
        "memory provider MCP: usuario=%s papel=%s departamentos=%s somente_leitura=%s",
        _user.user_id,
        _user.role.value,
        ",".join(_user.departments) or "-",
        _readonly,
    )

    server = Server(
        "orkmind-memory-provider",
        on_list_tools=_list_tools_handler,
        on_call_tool=_call_tool_handler,
    )
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await _provider.close()


def main() -> None:
    import asyncio

    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
