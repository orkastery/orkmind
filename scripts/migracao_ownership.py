"""Migracao retroativa de propriedade das entries (F2, decisao Q4).

Entries criadas antes da F2 nao tem author_id e ficam com o default
visibility='private'. Como todas foram criadas pelo dono da instalacao ou por seus
agentes num contexto single-user, esta migracao adota a opcao (a) do
plano: define author_id e promove visibility para 'public', mantendo o
comportamento das consultas que passam requester_id.

A migracao e idempotente: so altera linhas ainda sem author_id.

Uso:
    ORKMIND_DATABASE_URL=postgresql://... python scripts/migracao_ownership.py
    ... python scripts/migracao_ownership.py --author outro-perfil --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from orkmind.core.config import load_config
from orkmind.store.factory import create_store

DEFAULT_AUTHOR = "owner"


async def migrar_ownership(
    author_id: str = DEFAULT_AUTHOR, dry_run: bool = False
) -> dict[str, int]:
    """Atribui author_id e visibility='public' as entries legadas."""
    cfg = load_config()
    if not cfg.has_database:
        raise RuntimeError(
            "ORKMIND_DATABASE_URL nao configurado. "
            "Defina a variavel de ambiente ou ~/.orkmind/config.toml."
        )

    store = create_store(cfg)
    await store.initialize()
    try:
        # Script especifico de pgvector: fala SQL cru. A partir de F3.4
        # `create_store` devolve um GovernedStore, entao o adapter e
        # acessado por `.inner`, de forma explicita e nomeada.
        adapter = getattr(store, "inner", store)
        conn = await adapter._get_conn()
        cur = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM memories WHERE author_id IS NULL"
        )
        row = await cur.fetchone()
        pendentes = int(row["cnt"]) if row else 0

        if dry_run or pendentes == 0:
            return {"pendentes": pendentes, "migradas": 0}

        cur = await conn.execute(
            """
            UPDATE memories
            SET author_id = %s,
                visibility = 'public'
            WHERE author_id IS NULL
            """,
            (author_id,),
        )
        return {"pendentes": pendentes, "migradas": cur.rowcount}
    finally:
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--author",
        default=DEFAULT_AUTHOR,
        help=f"profile_id atribuido as entries legadas (default: {DEFAULT_AUTHOR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="apenas conta as entries pendentes, sem alterar nada",
    )
    args = parser.parse_args()

    resultado = asyncio.run(migrar_ownership(args.author, args.dry_run))
    if args.dry_run:
        print(f"{resultado['pendentes']} entry(s) sem author_id (dry-run).")
    else:
        print(
            f"Migracao concluida: {resultado['migradas']} de "
            f"{resultado['pendentes']} entry(s) atualizadas para "
            f"author_id='{args.author}' e visibility='public'."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
