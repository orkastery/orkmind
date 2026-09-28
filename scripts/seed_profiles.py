"""Seed dos perfis iniciais do OrkMind (F2/B12).

Cria os perfis de pessoa, agentes, fiscal de conformidade e o grupo
publico usados pelo controle de acesso. O seed e idempotente: perfis
existentes sao atualizados, nunca duplicados.

Uso:
    ORKMIND_DATABASE_URL=postgresql://... python scripts/seed_profiles.py
"""

from __future__ import annotations

import asyncio
import sys

from orkmind.core.config import load_config
from orkmind.core.models import Profile
from orkmind.store.factory import create_store

PROFILES: list[Profile] = [
    Profile(
        id="tomas",
        display_name="Tomas",
        profile_type="person",
        permissions={
            "can_create_rules": True,
            "can_view_encrypted": True,
            "is_admin": True,
        },
    ),
    Profile(id="hermes-copilot", display_name="Hermes Copilot", profile_type="agent"),
    Profile(
        id="hermes-briefing",
        display_name="Hermes Briefing Agent",
        profile_type="agent",
    ),
    Profile(
        id="hermes-docs", display_name="Hermes Docs Manager", profile_type="agent"
    ),
    Profile(
        id="hermes-redator",
        display_name="Hermes Redator Profissional",
        profile_type="agent",
    ),
    Profile(
        id="hermes-researcher",
        display_name="Hermes Web Researcher",
        profile_type="agent",
    ),
    Profile(
        id="hermes-memoria",
        display_name="Hermes Gestor de Memoria",
        profile_type="agent",
    ),
    Profile(
        id="fiscal-conformidade",
        display_name="Fiscal de Conformidade",
        profile_type="agent",
        permissions={
            "can_block_response": True,
            "can_read_all_rules": True,
            "can_create_compliance_review": True,
            "can_create_rules": False,
        },
    ),
    Profile(
        id="public",
        display_name="Publico",
        profile_type="group",
        metadata={"members": []},
    ),
]


async def seed_profiles(verbose: bool = True) -> dict[str, int]:
    """Cria ou atualiza os perfis iniciais. Retorna contagens."""
    cfg = load_config()
    if not cfg.has_store:
        raise RuntimeError(
            "Nenhum backend de storage configurado. Defina [store].backend "
            "ou ORKMIND_STORE_BACKEND."
        )

    store = create_store(cfg)
    await store.initialize()

    # F3.7: os perfis passam pelo ProfileStore do backend em uso. Em
    # pgvector isso e a tabela `profiles` de sempre; nos demais backends
    # sao entries da colecao `users`. O script nao precisa saber qual.
    perfis = store.profiles

    created = 0
    updated = 0
    try:
        for profile in PROFILES:
            existente = await perfis.get(profile.id)
            if existente is None:
                await perfis.create(profile)
                created += 1
                if verbose:
                    print(f"[ok] perfil criado: {profile.id}")
            else:
                # preserva created_at e membros ja cadastrados no grupo
                atualizado = profile.model_copy(
                    update={
                        "created_at": existente.created_at,
                        "metadata": {**profile.metadata, **existente.metadata},
                    }
                )
                await perfis.update(profile.id, atualizado)
                updated += 1
                if verbose:
                    print(f"[skip] perfil ja existe, atualizado: {profile.id}")
    finally:
        await store.close()

    return {"created": created, "updated": updated}


def main() -> int:
    result = asyncio.run(seed_profiles())
    print(
        f"\nSeed de perfis concluido: "
        f"{result['created']} criado(s), {result['updated']} atualizado(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
