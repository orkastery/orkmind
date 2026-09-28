"""Tools do servidor MCP do Memory Provider.

Duas regras atravessam este modulo:

1. **Identidade nao e argumento.** Nenhuma tool aceita papel ou departamento.
   O `UserContext` e montado no start do servidor, a partir do ambiente, e o
   modelo so consegue ESTREITAR o que aquela identidade ja podia ver. Se o
   papel viesse no argumento, bastaria o modelo pedir `EXECUTIVE` e o RBAC
   inteiro viraria enfeite.

2. **Resposta curta por padrao.** O ponto da camada e nao estourar contexto.
   Documento integro sai PAGINADO com ponteiro para a proxima pagina, nunca
   resumido: o mesmo principio anti-compactacao do resto do pacote.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from orkmind.memory_provider.models.schemas import (
    AccessLevel,
    DocumentIngestRequest,
    MemoryScopeFilter,
    SearchResult,
    UserContext,
)
from orkmind.memory_provider.provider import NativeMemoryProvider
from orkmind.memory_provider.tokens import head_by_tokens

# Teto de uma pagina de documento integro. Acima disso o agente pede a proxima.
DEFAULT_PAGE_TOKENS = 1500


def _origem(hit: SearchResult) -> str:
    """De qual perna veio o resultado - o agente merece saber por que confiar."""
    if hit.dense_rank and hit.sparse_rank:
        return "semantica+lexica"
    return "semantica" if hit.dense_rank else "lexica"


def _hit(hit: SearchResult) -> dict[str, Any]:
    return {
        "slug": hit.slug,
        "documento": hit.title,
        "secao": hit.heading_path,
        "conteudo": hit.content,
        "tokens": hit.token_count,
        "origem": _origem(hit),
    }


async def memory_search(
    provider: NativeMemoryProvider,
    scope: MemoryScopeFilter,
    query: str,
    limit: int = 5,
    doc_types: list[str] | None = None,
    tags: dict[str, Any] | None = None,
    index_path: str | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Busca hibrida na Wiki, ja restrita ao escopo de quem roda o servidor."""
    estreito = scope.model_copy(
        update={
            "doc_types": doc_types or scope.doc_types,
            "tags": tags or scope.tags,
            "index_path": index_path or scope.index_path,
        }
    )
    resposta = await provider.search_wiki_detailed(
        query, estreito, limit=limit, max_tokens=max_tokens
    )
    saida: dict[str, Any] = {
        "resultados": [_hit(h) for h in resposta.results],
        "tokens": resposta.token_total,
    }
    if resposta.degraded:
        # Nunca degradar em silencio: o agente precisa saber que a perna
        # semantica caiu e que isto aqui e so busca lexica.
        saida["aviso"] = f"busca degradada para lexica pura ({resposta.degraded})"
    if not resposta.results:
        saida["dica"] = "nada no escopo desta identidade; tente memory_index para ver o mapa"
    return saida


async def memory_get(
    provider: NativeMemoryProvider,
    scope: MemoryScopeFilter,
    slug: str,
    offset: int = 0,
    max_tokens: int = DEFAULT_PAGE_TOKENS,
) -> dict[str, Any]:
    """Documento integro, PAGINADO. Nada e resumido; o resto vem por offset."""
    doc = await provider.get_document(slug, scope)
    pagina, fim = head_by_tokens(doc.raw_content, max_tokens, offset=max(0, offset))
    saida: dict[str, Any] = {
        "slug": doc.slug,
        "titulo": doc.title,
        "tipo": doc.doc_type,
        "versao": doc.version,
        "classificacao": doc.access_level.value,
        "conteudo": pagina,
    }
    if fim < len(doc.raw_content):
        saida["continua_em"] = fim
        saida["dica"] = f'memory_get(slug="{doc.slug}", offset={fim}) traz a proxima pagina'
    return saida


async def memory_index(
    provider: NativeMemoryProvider,
    scope: MemoryScopeFilter,
    path: str | None = None,
    depth: int = 2,
    pendentes: bool = False,
) -> dict[str, Any]:
    """Mapa navegavel (dominio > tema > documento) antes de gastar busca vetorial."""
    arvore = await provider.browse_index(scope, path, depth)

    def render(nos: list[Any]) -> list[dict[str, Any]]:
        saida = []
        for no in nos:
            item: dict[str, Any] = {"caminho": no.path}
            if no.title:
                item["titulo"] = no.title
            if no.document is not None:
                item["slug"] = no.document.slug
                item["documento"] = no.document.title
            else:
                item["documentos_visiveis"] = no.document_count
            if no.children:
                item["filhos"] = render(no.children)
            saida.append(item)
        return saida

    saida: dict[str, Any] = {"indice": render(arvore)}
    if not arvore:
        saida["dica"] = "nada visivel neste escopo"
    if pendentes:
        # O que ja foi citado e nunca escrito: pauta, nao erro.
        saida["falta_escrever"] = [
            {"slug": p["target_slug"], "citado_por": p["citacoes"]}
            for p in await provider.unresolved_links(scope)
        ]
    return saida


async def memory_links(
    provider: NativeMemoryProvider,
    scope: MemoryScopeFilter,
    slug: str,
) -> dict[str, Any]:
    """As duas direcoes da teia de um documento, numa chamada so."""
    saindo = await provider.outgoing_links(slug, scope)
    entrando = await provider.backlinks(slug, scope)
    return {
        "aponta_para": [
            {
                "slug": x["target_slug"],
                "titulo": x["target_title"],
                "tipo": x["kind"],
                **({} if x["resolvido"] else {"ainda_nao_existe": True}),
            }
            for x in saindo
        ],
        "citado_por": [
            {"slug": x["slug"], "titulo": x["title"], "trecho": x["context"]} for x in entrando
        ],
    }


async def memory_tags(
    provider: NativeMemoryProvider,
    scope: MemoryScopeFilter,
    tag: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Sem `tag`: o painel de tags com contagem. Com `tag`: os documentos dela."""
    if tag:
        documentos = await provider.documents_by_tag(tag, scope, limit=limit)
        return {
            "tag": tag,
            # A barra e hierarquia: `rede` traz `rede/backbone` junto.
            "documentos": [
                {"slug": d["slug"], "titulo": d["title"], "tipo": d["doc_type"]} for d in documentos
            ],
        }
    return {
        "tags": [
            {"tag": t["tag"], "documentos": t["documentos"]}
            for t in await provider.list_tags(scope, limit=limit)
        ]
    }


async def memory_remember(
    provider: NativeMemoryProvider,
    user: UserContext,
    slug: str,
    title: str,
    content: str,
    doc_type: str = "note",
    access_level: str | None = None,
    departments: list[str] | None = None,
    tags: list[str] | None = None,
    index_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Grava um documento na Wiki.

    A classificacao default e a do proprio usuario: o modelo nao consegue
    gravar algo mais restrito do que ele proprio pode ler, nem publicar como
    OPERATIONAL um conteudo que so ele veria.
    """
    nivel = AccessLevel(access_level) if access_level else user.role
    if nivel.rank > user.role.rank:
        raise ValueError(
            f"{user.user_id!r} e {user.role.value} e nao pode classificar como {nivel.value}"
        )
    resultado = await provider.ingest_document(
        DocumentIngestRequest(
            slug=slug,
            title=title,
            doc_type=doc_type,
            raw_content=content,
            access_level=nivel,
            department_scope=departments if departments is not None else user.departments,
            tags=tags or [],
            index_paths=index_paths or [],
            ingested_by=f"mcp:{user.user_id}",
        )
    )
    return {
        "situacao": resultado.status.value,
        "slug": resultado.slug,
        "versao": resultado.version,
        "chunks": resultado.chunk_count,
    }


async def memory_recall(
    provider: NativeMemoryProvider,
    user: UserContext,
    query: str,
    session_id: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Recupera turnos antigos por full-text. Turno antigo nao se resume: se busca."""
    mensagens = await provider.search_history(
        query,
        session_id=session_id,
        user_id=None if session_id else user.user_id,
        limit=limit,
    )
    return {
        "turnos": [
            {
                "sessao": m.session_id,
                "turno": m.turn_index,
                "papel": m.role.value,
                "quando": m.created_at.isoformat(),
                "conteudo": m.content,
                **({"continua_em": m.next_offset} if m.truncated else {}),
            }
            for m in mensagens
        ]
    }


async def read_page(
    provider: NativeMemoryProvider, message_id: str, offset: int = 0
) -> dict[str, Any]:
    """Proxima pagina de uma mensagem longa do historico."""
    pagina = await provider.read_history_message(UUID(message_id), offset=offset)
    saida: dict[str, Any] = {"conteudo": pagina.content}
    if pagina.next_offset is not None:
        saida["continua_em"] = pagina.next_offset
    return saida
