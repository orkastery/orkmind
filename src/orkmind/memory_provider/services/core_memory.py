"""Tier 1 - Core Memory: blocos pinados no prompt.

`persona`, `system_invariants` e `user_profile` entram em TODO turno. Nao
expiram, nao sao podados e nao competem por orcamento com o resto do contexto.

"Imutavel" aqui tem dois sentidos, ambos garantidos pelo banco:

- o AGENTE nao tem ferramenta para alterar estes blocos; `set_block` e uma
  API de operador e exige dizer quem esta alterando;
- nem o operador edita uma linha: trocar o texto aposenta a versao vigente e
  insere a proxima. O que o agente leu na terca continua consultavel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orkmind.memory_provider.errors import CoreMemoryMissingError
from orkmind.memory_provider.models.schemas import (
    CORE_BLOCK_ORDER,
    REQUIRED_CORE_BLOCKS,
    CoreBlockLabel,
    CoreMemoryBlock,
    SystemContext,
    UserContext,
    content_sha256,
)
from orkmind.memory_provider.tokens import estimate_tokens

if TYPE_CHECKING:  # pragma: no cover - so para anotacao
    import asyncpg

_COLUMNS = "id, agent_id, label, scope_key, content, content_hash, version, created_by, created_at"

# user_profile: o do usuario vence o generico ('') do agente.
_ACTIVE_SQL = f"""
SELECT DISTINCT ON (label) {_COLUMNS}
FROM core_blocks
WHERE agent_id = $1 AND is_active AND scope_key IN ('', $2)
ORDER BY label, (scope_key = $2) DESC
"""


class CoreMemoryService:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def set_block(
        self,
        agent_id: str,
        label: CoreBlockLabel,
        content: str,
        *,
        created_by: str,
        scope_key: str = "",
    ) -> CoreMemoryBlock:
        """Publica uma versao do bloco. Conteudo identico ao vigente e no-op."""
        if not content.strip():
            raise ValueError("bloco de core memory nao pode ser vazio")
        if label is not CoreBlockLabel.USER_PROFILE and scope_key:
            raise ValueError(f"{label.value} e global do agente; scope_key so vale em user_profile")
        digest = content_sha256(content)

        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"core:{agent_id}:{label.value}:{scope_key}",
            )
            current = await conn.fetchrow(
                f"""
                SELECT {_COLUMNS} FROM core_blocks
                WHERE agent_id = $1 AND label = $2 AND scope_key = $3 AND is_active
                """,
                agent_id,
                label.value,
                scope_key,
            )
            if current is not None and current["content_hash"] == digest:
                return CoreMemoryBlock(**dict(current))
            if current is not None:
                await conn.execute(
                    "UPDATE core_blocks SET is_active = FALSE, superseded_at = now() WHERE id = $1",
                    current["id"],
                )
            row = await conn.fetchrow(
                f"""
                INSERT INTO core_blocks
                    (agent_id, label, scope_key, content, content_hash, version, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING {_COLUMNS}
                """,
                agent_id,
                label.value,
                scope_key,
                content,
                digest,
                1 if current is None else current["version"] + 1,
                created_by,
            )
        return CoreMemoryBlock(**dict(row))

    async def get_system_context(
        self, agent_id: str, user: UserContext, *, strict: bool = True
    ) -> SystemContext:
        rows = await self._pool.fetch(_ACTIVE_SQL, agent_id, user.user_id)
        by_label = {CoreBlockLabel(row["label"]): CoreMemoryBlock(**dict(row)) for row in rows}

        missing = [label.value for label in REQUIRED_CORE_BLOCKS if label not in by_label]
        if missing and strict:
            raise CoreMemoryMissingError(
                f"agente {agent_id!r} sem bloco(s) obrigatorio(s): {', '.join(missing)}. "
                f"Publique com set_core_block antes de atender."
            )

        blocks = [by_label[label] for label in CORE_BLOCK_ORDER if label in by_label]
        context = SystemContext(agent_id=agent_id, user=user, blocks=blocks, token_estimate=0)
        return context.model_copy(update={"token_estimate": estimate_tokens(context.render())})

    async def history(
        self, agent_id: str, label: CoreBlockLabel, *, scope_key: str = ""
    ) -> list[CoreMemoryBlock]:
        """Todas as versoes do bloco, da mais nova para a mais antiga."""
        rows = await self._pool.fetch(
            f"""
            SELECT {_COLUMNS} FROM core_blocks
            WHERE agent_id = $1 AND label = $2 AND scope_key = $3
            ORDER BY version DESC
            """,
            agent_id,
            label.value,
            scope_key,
        )
        return [CoreMemoryBlock(**dict(row)) for row in rows]
