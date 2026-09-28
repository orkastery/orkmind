"""API HTTP local do OrkMind: ponto unico de escrita idempotente.

Esta API existe para sustentar a solucao "sempre gravar". O drainer da
spool (scripts/orkmind_drain.py) grava preferencialmente por aqui, e cai
para o CLI apenas se a API estiver fora.

Endpoints:

    GET  /health                              vivacidade
    GET  /entries/by-hash?collection=&hash=   lookup de idempotencia
    POST /entries                             escrita idempotente

Contrato de idempotencia do POST /entries: a resposta sempre traz
``{entry_id, content_hash, status}``, com ``status`` em ``created`` ou
``duplicate``. O cliente pode repetir a mesma requisicao a vontade sem
gerar memoria duplicada, o que e exatamente o que permite ao drainer
reprocessar a fila sem medo.

Postura de seguranca (a API escuta em loopback, mas escrita nunca fica
aberta):

- Autenticacao obrigatoria por token Bearer (env ORKMIND_API_TOKEN).
  Sem token configurado a API sobe em modo somente-leitura-negada: toda
  rota protegida responde 503, nunca "aberta por omissao".
- Comparacao de token em tempo constante (hmac.compare_digest).
- Simetria de hash: se o cliente enviar content_hash, o servidor
  RECOMPUTA o SHA-256 do conteudo e rejeita divergencia com 400. Sem
  isso um cliente poderia registrar conteudo sob o hash de outro e
  envenenar a chave de deduplicacao.
- O lookup por hash devolve apenas metadados (entry_id, colecao,
  timestamps). Nunca o conteudo, para que o endpoint nao vire um oraculo
  de confirmacao de conteudo alheio.
- mandatory=true e priority=critical sao recusados quando source=agent,
  espelhando a regra de que governanca so vem de humano autenticado.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from orkmind.core.config import OrkMindConfig, load_config
from orkmind.core.injection import compute_content_hash
from orkmind.core.models import VALID_COLLECTIONS, MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.store.factory import create_embedder, create_store

logger = logging.getLogger(__name__)

#: Env com o token Bearer exigido nas rotas protegidas.
API_TOKEN_ENV = "ORKMIND_API_TOKEN"

_VALID_PRIORITIES = ("critical", "high", "medium", "low")
_VALID_SOURCES = ("human", "agent", "system", "bootstrap")
_VALID_SCOPES = ("global", "project", "session")


def _error(message: str, status: int, **extra: Any) -> JSONResponse:
    payload: dict[str, Any] = {"error": message}
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


def _expected_token() -> str:
    return os.environ.get(API_TOKEN_ENV, "").strip()


def _check_auth(request: Request) -> Optional[JSONResponse]:
    """Valida o Bearer token. Devolve a resposta de erro, ou None se ok."""
    expected = _expected_token()
    if not expected:
        # Falha fechada de proposito: sem token configurado nao ha
        # escrita nem lookup, em vez de expor a API sem autenticacao.
        return _error(
            f"API sem token configurado. Defina {API_TOKEN_ENV} no ambiente "
            f"do servico para habilitar as rotas protegidas.",
            503,
        )

    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return _error("Credencial ausente. Use 'Authorization: Bearer <token>'.", 401)

    if not hmac.compare_digest(presented.strip(), expected):
        return _error("Credencial invalida.", 401)

    return None


async def health(request: Request) -> JSONResponse:
    """Vivacidade. Nao exige token e nao revela nada do acervo."""
    return JSONResponse({"status": "ok", "service": "orkmind-api"})


async def lookup_by_hash(request: Request) -> JSONResponse:
    """GET /entries/by-hash?collection=&hash= - lookup de idempotencia.

    Responde apenas com metadados. O conteudo nunca e devolvido aqui.
    """
    if (denied := _check_auth(request)) is not None:
        return denied

    content_hash = (request.query_params.get("hash") or "").strip()
    collection = (request.query_params.get("collection") or "").strip() or None

    if not content_hash:
        return _error("Parametro 'hash' e obrigatorio.", 400)
    if collection and collection not in VALID_COLLECTIONS:
        return _error(f"Colecao invalida: '{collection}'.", 400)

    layer: SemanticLayer = request.app.state.layer
    existing = await layer.store.find_by_content_hash(content_hash, collection)

    if existing is None:
        return JSONResponse(
            {"status": "not_found", "content_hash": content_hash, "collection": collection},
            status_code=404,
        )

    return JSONResponse(
        {
            "status": "found",
            "entry_id": existing.id,
            "content_hash": existing.content_hash,
            "collection": existing.collection,
            "created_at": existing.created_at.isoformat(),
            "version": existing.version,
        }
    )


async def create_entry(request: Request) -> JSONResponse:
    """POST /entries - escrita idempotente por content_hash."""
    if (denied := _check_auth(request)) is not None:
        return denied

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _error("Corpo da requisicao nao e JSON valido.", 400)
    if not isinstance(body, dict):
        return _error("Corpo da requisicao deve ser um objeto JSON.", 400)

    content = body.get("content") or ""
    if not isinstance(content, str) or not content.strip():
        return _error("Campo 'content' e obrigatorio e nao pode ser vazio.", 400)

    collection = body.get("collection") or "content"
    if collection not in VALID_COLLECTIONS:
        return _error(
            f"Colecao invalida: '{collection}'. "
            f"Validas: {', '.join(VALID_COLLECTIONS)}",
            400,
        )

    tags = body.get("tags") or {}
    if not isinstance(tags, dict) or not all(
        isinstance(v, list) and all(isinstance(i, str) for i in v) for v in tags.values()
    ):
        return _error("Campo 'tags' deve ser um objeto de listas de strings.", 400)

    priority = body.get("priority") or "medium"
    if priority not in _VALID_PRIORITIES:
        return _error(f"Prioridade invalida: '{priority}'.", 400)

    source = body.get("source") or "agent"
    if source not in _VALID_SOURCES:
        return _error(f"Source invalido: '{source}'.", 400)

    scope = body.get("scope") or "global"
    if scope not in _VALID_SCOPES:
        return _error(f"Scope invalido: '{scope}'.", 400)

    mandatory = bool(body.get("mandatory", False))

    # Governanca so vem de humano autenticado. Um agente (ou o drainer
    # drenando conteudo de agente) nunca cria regra mandatoria/critica.
    if source == "agent" and (mandatory or priority == "critical"):
        return _error(
            "source='agent' nao pode criar memoria mandatory=true nem "
            "priority='critical'. Governanca exige humano autenticado.",
            403,
        )

    # Simetria de hash: o servidor e a autoridade sobre o hash. Se o
    # cliente mandou um, tem que bater com o conteudo enviado.
    computed_hash = compute_content_hash(content)
    declared_hash = (body.get("content_hash") or "").strip()
    if declared_hash and not hmac.compare_digest(declared_hash, computed_hash):
        return _error(
            "content_hash divergente do conteudo enviado.",
            400,
            expected=computed_hash,
            received=declared_hash,
        )

    layer: SemanticLayer = request.app.state.layer

    # 1. Caminho rapido de idempotencia: ja existe com este hash?
    existing = await layer.store.find_by_content_hash(computed_hash, collection)
    if existing is not None:
        return JSONResponse(
            {
                "status": "duplicate",
                "entry_id": existing.id,
                "content_hash": computed_hash,
                "collection": existing.collection,
            }
        )

    entry = MemoryEntry(
        content=content,
        collection=collection,  # type: ignore[arg-type]
        tags=tags,
        priority=priority,  # type: ignore[arg-type]
        mandatory=mandatory,
        scope=scope,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        content_hash=computed_hash,
        metadata=body.get("metadata") or {},
        author_id=body.get("author_id"),
        url=body.get("url"),
    )

    try:
        entry_id, warnings = await layer.add_memory(entry)
    except Exception as exc:  # noqa: BLE001
        # 2. Corrida entre dois gravadores: o indice unico parcial
        #    (collection, content_hash) barra o segundo INSERT. Isso nao
        #    e erro, e a idempotencia dura funcionando - reconsultamos e
        #    devolvemos o entry_id real de quem chegou primeiro.
        duplicate = await layer.store.find_by_content_hash(computed_hash, collection)
        if duplicate is not None:
            return JSONResponse(
                {
                    "status": "duplicate",
                    "entry_id": duplicate.id,
                    "content_hash": computed_hash,
                    "collection": duplicate.collection,
                }
            )
        logger.exception("Falha ao gravar entry na colecao %s", collection)
        return _error(f"Falha ao gravar entry: {exc}", 500)

    return JSONResponse(
        {
            "status": "created",
            "entry_id": entry_id,
            "content_hash": computed_hash,
            "collection": collection,
            "warnings": warnings,
        },
        status_code=201,
    )


def create_app(
    config: Optional[OrkMindConfig] = None,
    layer: Optional[SemanticLayer] = None,
) -> Starlette:
    """Monta a aplicacao Starlette da API local do OrkMind.

    `layer` permite injetar uma SemanticLayer pronta (usado nos testes),
    caso em que nenhuma conexao nova de banco e aberta no startup.
    """
    cfg = config if config is not None else (OrkMindConfig() if layer else load_config())

    @contextlib.asynccontextmanager
    async def _lifespan(app: Starlette) -> AsyncIterator[None]:
        if layer is not None:
            app.state.layer = layer
        else:
            store = create_store(cfg)
            await store.initialize()
            embedder = create_embedder(cfg)
            app.state.layer = SemanticLayer(
                store, token_budget=cfg.token_budget, embedder=embedder
            )
        if not _expected_token():
            logger.warning(
                "API OrkMind iniciada SEM %s: rotas protegidas responderao 503. "
                "Defina o token para habilitar escrita.",
                API_TOKEN_ENV,
            )
        try:
            yield
        finally:
            # Uma layer injetada pertence a quem a criou; nao fechamos aqui.
            if layer is None:
                ativa = getattr(app.state, "layer", None)
                if ativa is not None:
                    await ativa.store.close()

    routes: list[Route] = [
        Route("/health", health, methods=["GET"]),
        Route("/entries/by-hash", lookup_by_hash, methods=["GET"]),
        Route("/entries", create_entry, methods=["POST"]),
    ]

    return Starlette(routes=routes, lifespan=_lifespan)


def serve(host: str = "127.0.0.1", port: int = 8077, log_level: str = "info") -> None:
    """Sobe a API com uvicorn.

    O default de host e 127.0.0.1 de proposito: esta API grava memoria e
    nunca deve ficar exposta fora do loopback.
    """
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level=log_level)


__all__ = ["create_app", "serve"]
