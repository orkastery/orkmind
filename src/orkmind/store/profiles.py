"""ProfileStore: identidades do OrkMind, independentes de backend (DD-6).

A ACL precisa resolver quem e o requester e a quais grupos ele pertence.
Ate F3.3 isso vivia em metodos `profile_*` que so o `PostgresAdapter`
tinha, o que amarrava a ACL a um backend. Aqui o acesso a perfis vira um
protocolo com duas implementacoes:

- `PostgresProfileStore`: delega aos metodos que ja existem no
  `PostgresAdapter`, sobre a tabela `profiles`. Producao nao migra nada
  nesta leva e o comportamento e identico ao de hoje.
- `EntryProfileStore`: generico, persiste `Profile` como `MemoryEntry` na
  colecao `users`, pelo mesmo caminho de qualquer memoria. Funciona em
  qualquer backend.

Mapeamento `Profile` -> `MemoryEntry` do `EntryProfileStore`:

| Profile          | MemoryEntry                              |
|------------------|------------------------------------------|
| `id`             | `id`                                     |
| `display_name`   | `content`                                |
| `profile_type`   | `metadata["profile_type"]`               |
| `parent_id`      | `metadata["profile_parent_id"]`          |
| `permissions`    | `metadata["permissions"]`                |
| `metadata`       | `metadata["profile_metadata"]`           |
| (membros)        | `metadata["members"]`, espelho de leitura|
| -                | `metadata["orkmind_profile"] = True`     |
| `created_at`     | `created_at`                             |

DP-A, aceita e declarada: em backends que usam o `EntryProfileStore`, os
perfis passam a aparecer nas listagens da colecao `users`. A marca
`metadata["orkmind_profile"]` existe para que um filtro de CLI possa ser
adicionado depois sem quebrar nada. A divergencia esta na matriz de
backends, nao no silencio.

O `EntryProfileStore` sempre escreve e le pelo adapter CRU, nunca pelo
`GovernedStore`: identidade e infraestrutura da ACL, e faze-la passar
pela ACL seria recursao.
"""

from __future__ import annotations

import builtins
import logging
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from orkmind.core.models import MemoryEntry, Profile
from orkmind.store.base import MemoryStore

logger = logging.getLogger(__name__)

COLECAO_PERFIS: Literal["users"] = "users"
MARCA_PERFIL = "orkmind_profile"

# Teto de varredura da colecao de perfis. A tabela de identidades e
# pequena por natureza (pessoas, agentes, sistemas e grupos de uma
# instalacao); se algum dia estourar, o log avisa em vez de truncar em
# silencio.
LIMITE_VARREDURA = 5000


@runtime_checkable
class ProfileStore(Protocol):
    """Acesso a identidades, independente de backend."""

    async def get(self, profile_id: str) -> Optional[Profile]:
        ...

    async def create(self, profile: Profile) -> str:
        ...

    async def update(self, profile_id: str, profile: Profile) -> bool:
        ...

    async def delete(self, profile_id: str) -> bool:
        ...

    async def list(
        self, profile_type: Optional[str] = None
    ) -> builtins.list[Profile]:
        ...

    async def get_members(self, group_id: str) -> builtins.list[str]:
        ...

    async def resolve_identities(self, requester_id: str) -> builtins.list[str]:
        ...


async def _resolver_identidades(
    profiles: ProfileStore, requester_id: str
) -> builtins.list[str]:
    """Identidades efetivas: o proprio requester e os grupos dele.

    Um nivel, sem recursao, exatamente como o comportamento atual do
    `PostgresAdapter.resolve_identities`.
    """
    identidades: builtins.list[str] = [requester_id]
    try:
        grupos = await profiles.list(profile_type="group")
    except Exception as e:
        logger.debug("Falha ao resolver grupos de '%s': %s", requester_id, e)
        return identidades
    for grupo in grupos:
        membros = grupo.metadata.get("members", [])
        if isinstance(membros, list) and requester_id in membros:
            identidades.append(grupo.id)
    return identidades


class PostgresProfileStore:
    """Delega aos metodos `profile_*` do `PostgresAdapter` (DD-6).

    Zero migracao de dados de producao nesta leva: a tabela `profiles`
    continua sendo a fonte da verdade no backend pgvector (divida D-3).
    """

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def get(self, profile_id: str) -> Optional[Profile]:
        resultado: Optional[Profile] = await self._adapter.profile_get(profile_id)
        return resultado

    async def create(self, profile: Profile) -> str:
        resultado: str = await self._adapter.profile_create(profile)
        return resultado

    async def update(self, profile_id: str, profile: Profile) -> bool:
        resultado: bool = await self._adapter.profile_update(profile_id, profile)
        return resultado

    async def delete(self, profile_id: str) -> bool:
        resultado: bool = await self._adapter.profile_delete(profile_id)
        return resultado

    async def list(
        self, profile_type: Optional[str] = None
    ) -> builtins.list[Profile]:
        resultado: builtins.list[Profile] = await self._adapter.profile_list(profile_type)
        return resultado

    async def get_members(self, group_id: str) -> builtins.list[str]:
        resultado: builtins.list[str] = await self._adapter.profile_get_members(group_id)
        return resultado

    async def resolve_identities(self, requester_id: str) -> builtins.list[str]:
        # O adapter ja tem a implementacao historica; usar a dele mantem
        # o caminho quente de ACL do pgvector inalterado (D-7).
        resultado: builtins.list[str] = await self._adapter.resolve_identities(
            requester_id
        )
        return resultado


class EntryProfileStore:
    """Perfis persistidos como entries da colecao `users`.

    Funciona em qualquer `MemoryStore`, inclusive nos backends que nunca
    ouviram falar de perfil. E o que faz a ACL valer em `memory` e em
    `qdrant` sem re-engenharia (DP-10).
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # --- conversao ---

    @staticmethod
    def _para_entry(profile: Profile) -> MemoryEntry:
        membros = profile.metadata.get("members", [])
        return MemoryEntry(
            id=profile.id,
            content=profile.display_name,
            collection=COLECAO_PERFIS,
            source="system",
            visibility="public",
            created_at=profile.created_at,
            updated_at=profile.created_at,
            metadata={
                MARCA_PERFIL: True,
                "profile_type": profile.profile_type,
                "profile_parent_id": profile.parent_id,
                "permissions": dict(profile.permissions),
                "profile_metadata": dict(profile.metadata),
                "members": list(membros) if isinstance(membros, list) else [],
            },
        )

    @staticmethod
    def _e_perfil(entry: MemoryEntry) -> bool:
        return bool(entry.metadata.get(MARCA_PERFIL))

    @staticmethod
    def _para_profile(entry: MemoryEntry) -> Profile:
        meta = entry.metadata
        return Profile(
            id=entry.id,
            display_name=entry.content,
            profile_type=meta.get("profile_type", "person"),
            parent_id=meta.get("profile_parent_id"),
            permissions=dict(meta.get("permissions") or {}),
            metadata=dict(meta.get("profile_metadata") or {}),
            created_at=entry.created_at,
        )

    # --- CRUD ---

    async def get(self, profile_id: str) -> Optional[Profile]:
        entry = await self._store.retrieve(profile_id)
        if entry is None or not self._e_perfil(entry):
            return None
        return self._para_profile(entry)

    async def create(self, profile: Profile) -> str:
        """Cria ou substitui, espelhando o `ON CONFLICT DO UPDATE` do SQL."""
        existente = await self._store.retrieve(profile.id)
        entry = self._para_entry(profile)
        if existente is None:
            await self._store.store(entry)
        else:
            await self._store.update(profile.id, entry)
        return profile.id

    async def update(self, profile_id: str, profile: Profile) -> bool:
        existente = await self._store.retrieve(profile_id)
        if existente is None or not self._e_perfil(existente):
            return False
        entry = self._para_entry(profile)
        entry.id = profile_id
        return await self._store.update(profile_id, entry)

    async def delete(self, profile_id: str) -> bool:
        existente = await self._store.retrieve(profile_id)
        if existente is None or not self._e_perfil(existente):
            return False
        return await self._store.delete(profile_id, source="system")

    async def list(
        self, profile_type: Optional[str] = None
    ) -> builtins.list[Profile]:
        entries = await self._store.search_by_tags(
            tags={}, collection=COLECAO_PERFIS, limit=LIMITE_VARREDURA
        )
        if len(entries) >= LIMITE_VARREDURA:
            logger.warning(
                "Varredura de perfis atingiu o teto de %d entries na colecao "
                "'%s'; a lista pode estar truncada.",
                LIMITE_VARREDURA,
                COLECAO_PERFIS,
            )
        perfis = [self._para_profile(e) for e in entries if self._e_perfil(e)]
        if profile_type:
            perfis = [p for p in perfis if p.profile_type == profile_type]
        return sorted(perfis, key=lambda p: p.id)

    async def get_members(self, group_id: str) -> builtins.list[str]:
        perfil = await self.get(group_id)
        if not perfil or perfil.profile_type != "group":
            return []
        membros = perfil.metadata.get("members", [])
        if not isinstance(membros, list):
            return []
        return [str(m) for m in membros]

    async def resolve_identities(self, requester_id: str) -> builtins.list[str]:
        return await _resolver_identidades(self, requester_id)
