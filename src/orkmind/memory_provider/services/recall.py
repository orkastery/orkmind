"""Tier 2 - Recall Memory: historico append-only com janela deslizante.

O problema que este modulo elimina e o "memoria em 99%, vou compactar". A
causa e tratar o historico como algo que mora DENTRO do contexto e, quando
enche, precisa ser resumido - trocando turnos reais por uma sintese que
ninguem audita. Aqui o historico mora no banco, inteiro e para sempre; o que
vai para o orquestrador e uma VISAO:

- janela: os ultimos N turnos (default 6), na integra;
- paginacao: mensagem grande demais (um tool_result de 50 mil tokens) entra
  na janela so com a primeira pagina e um ponteiro; o resto se le sob demanda
  com `read_message`. Nada e cortado no banco;
- busca: turno antigo se RECUPERA por full-text, nao se resume.

Nao existe UPDATE nem DELETE neste modulo, e o banco recusaria se existisse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from orkmind.memory_provider.config import MemoryProviderSettings
from orkmind.memory_provider.models.schemas import ChatRole, HistoryMessage, HistoryPage
from orkmind.memory_provider.tokens import estimate_tokens, head_by_tokens

if TYPE_CHECKING:  # pragma: no cover - so para anotacao
    import asyncpg

_COLUMNS = (
    "id, seq, session_id, agent_id, user_id, turn_index, role, content, tool_calls, "
    "token_estimate, metadata, created_at"
)

_WINDOW_SQL = f"""
SELECT {_COLUMNS}
FROM chat_history
WHERE session_id = $1
  AND turn_index > (
        SELECT COALESCE(max(turn_index), 0) FROM chat_history WHERE session_id = $1
      ) - $2
ORDER BY seq
"""

_SEARCH_SQL = f"""
WITH query AS (
    SELECT CASE WHEN numnode(tsq) = 0 THEN NULL
                ELSE replace(tsq::text, ' & ', ' | ')::tsquery END AS tsq
    FROM (SELECT plainto_tsquery($1::text::regconfig, $2) AS tsq) parsed
)
SELECT {_COLUMNS}
FROM chat_history
CROSS JOIN query
WHERE query.tsq IS NOT NULL
  AND tsv @@ query.tsq
  AND ($3::text IS NULL OR session_id = $3)
  AND ($4::text IS NULL OR user_id = $4)
ORDER BY ts_rank_cd(tsv, query.tsq, 1) DESC, seq DESC
LIMIT $5
"""

_TOOL_CALL_ROLES = (ChatRole.ASSISTANT, ChatRole.TOOL_CALL)


class RecallService:
    def __init__(self, pool: asyncpg.Pool, settings: MemoryProviderSettings) -> None:
        self._pool = pool
        self._settings = settings

    async def append(
        self,
        session_id: str,
        role: ChatRole,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        *,
        agent_id: str,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> HistoryMessage:
        if tool_calls and role not in _TOOL_CALL_ROLES:
            raise ValueError(f"tool_calls nao se aplica ao papel {role.value}")
        token_estimate = estimate_tokens(content)

        async with self._pool.acquire() as conn, conn.transaction():
            # Serializa a sessao: dois appends simultaneos nao podem calcular
            # o mesmo turn_index a partir do mesmo max().
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"chat:{session_id}"
            )
            last_turn = await conn.fetchval(
                "SELECT COALESCE(max(turn_index), 0) FROM chat_history WHERE session_id = $1",
                session_id,
            )
            opens_turn = role is ChatRole.USER or last_turn == 0
            row = await conn.fetchrow(
                f"""
                INSERT INTO chat_history
                    (session_id, agent_id, user_id, turn_index, role, content, tool_calls,
                     token_estimate, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING {_COLUMNS}
                """,
                session_id,
                agent_id,
                user_id,
                last_turn + 1 if opens_turn else last_turn,
                role.value,
                content,
                tool_calls,
                token_estimate,
                metadata or {},
            )
        return HistoryMessage(**dict(row))

    async def window(
        self,
        session_id: str,
        turns: int | None = None,
        *,
        max_message_tokens: int | None = None,
    ) -> list[HistoryMessage]:
        """Ultimos `turns` turnos, em ordem, com mensagens grandes paginadas."""
        turns = turns or self._settings.recall_window_turns
        rows = await self._pool.fetch(_WINDOW_SQL, session_id, turns)
        return [self._view(row, max_message_tokens) for row in rows]

    async def read_message(
        self, message_id: UUID, *, offset: int = 0, max_tokens: int | None = None
    ) -> HistoryPage:
        """Pagina de uma mensagem, a partir do caractere `offset`."""
        row = await self._pool.fetchrow(
            "SELECT content, token_estimate FROM chat_history WHERE id = $1", message_id
        )
        if row is None:
            raise LookupError(f"mensagem inexistente: {message_id}")
        content: str = row["content"]
        budget = max_tokens or self._settings.recall_max_message_tokens
        page, end = head_by_tokens(content, budget, offset=max(0, offset))
        return HistoryPage(
            message_id=message_id,
            content=page,
            offset=offset,
            next_offset=end if end < len(content) else None,
            total_chars=len(content),
            total_tokens=row["token_estimate"],
        )

    async def full_history(
        self, session_id: str, *, after_seq: int = 0, limit: int = 200
    ) -> list[HistoryMessage]:
        """Trilha integra para auditoria, paginada por `seq`. Sem truncamento."""
        rows = await self._pool.fetch(
            f"""
            SELECT {_COLUMNS} FROM chat_history
            WHERE session_id = $1 AND seq > $2
            ORDER BY seq
            LIMIT $3
            """,
            session_id,
            after_seq,
            limit,
        )
        return [HistoryMessage(**dict(row)) for row in rows]

    async def search(
        self,
        query: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 5,
    ) -> list[HistoryMessage]:
        """Turnos antigos por full-text. Exige sessao ou usuario: sem isso a
        busca atravessaria conversas de outras pessoas."""
        if session_id is None and user_id is None:
            raise ValueError("search exige session_id ou user_id")
        if not query.strip():
            return []
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            # Filtros opcionais (`$n IS NULL OR ...`) so viram acesso por
            # indice no plano customizado; ver services/retrieval.py.
            await conn.execute("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
            rows = await conn.fetch(
                _SEARCH_SQL, self._settings.fts_config, query, session_id, user_id, limit
            )
        return [self._view(row, None) for row in rows]

    def _view(self, row: asyncpg.Record, max_message_tokens: int | None) -> HistoryMessage:
        message = HistoryMessage(**dict(row))
        budget = max_message_tokens or self._settings.recall_max_message_tokens
        if message.token_estimate <= budget:
            return message
        page, end = head_by_tokens(message.content, budget)
        pointer = (
            f"\n[... paginado: {budget} de {message.token_estimate} tokens. "
            f'Continue com read_history_message(message_id="{message.id}", offset={end}).]'
        )
        return message.model_copy(
            update={"content": page + pointer, "truncated": True, "next_offset": end}
        )
