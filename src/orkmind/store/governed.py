"""GovernedStore: a camada onde a governanca do OrkMind e aplicada (F3.4).

Rota C do plano: um `MemoryStore` que implementa o contrato inteiro e
compoe OUTRO `MemoryStore` (o adapter). Nao existe contrato novo. Tudo
que esta acima (SemanticLayer, SnapshotManager, CLI, API, MCP, Hermes)
continua vendo exatamente o mesmo tipo.

O ponto da camada e existir UM lugar onde a politica e aplicada, e
`create_store()` garantir que todo mundo passa por ele. O adapter vira
persistencia; a decisao mora aqui e nas funcoes puras de
`store/governance.py`.

Invariantes garantidos (secao 2.4 do plano):

I1  protegida nunca e alterada nem apagada por agente
I2  a protecao D2 e avaliada ANTES da ACL de escrita e prevalece
I3  com requester_id so retorna o visivel; sem ele, comportamento antigo
I4  entry expirada nunca aparece em busca
I5  injection_risk nunca aparece em search_by_tags
I6  mandatory que casa com a consulta vem antes, e o corte avisa
I7  update arquiva versao e incrementa version em exatamente 1
I8  snapshot_restore cria auto-backup antes de escrever
I9  a ordenacao de search_by_tags e total e identica entre backends
I10 nenhuma degradacao ocorre sem estar declarada e logada
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from orkmind.core.models import MemoryEntry, Profile, Source
from orkmind.store.base import MemoryStore
from orkmind.store.capabilities import StoreCapabilities
from orkmind.store.governance import (
    assert_nao_protegida,
    assert_pode_escrever,
    filtrar_ativas,
    filtrar_sem_injection,
    filtrar_visiveis,
    limite_de_candidatos,
    ordenar_constitucional,
    particionar_mandatory_primeiro,
)
from orkmind.store.profiles import (
    EntryProfileStore,
    PostgresProfileStore,
    ProfileStore,
)

logger = logging.getLogger(__name__)

TETO_OVERFETCH_PADRAO = 500


def montar_profile_store(nome_backend: str, adapter: MemoryStore) -> ProfileStore:
    """Escolhe a implementacao de perfis adequada ao backend (DD-6).

    `pgvector` continua na tabela `profiles` (zero migracao de producao
    nesta leva). Os demais persistem perfis como entries da colecao
    `users`, o que faz a ACL valer em qualquer backend.
    """
    if nome_backend in ("pgvector", "postgres") and hasattr(adapter, "profile_get"):
        return PostgresProfileStore(adapter)
    return EntryProfileStore(adapter)


class GovernedStore(MemoryStore):
    """Aplica a governanca do OrkMind sobre qualquer `MemoryStore`."""

    def __init__(
        self,
        inner: MemoryStore,
        profiles: ProfileStore,
        candidate_overfetch: int = TETO_OVERFETCH_PADRAO,
    ) -> None:
        self._inner = inner
        self._profiles = profiles
        self._teto_overfetch = max(int(candidate_overfetch), 1)

    # --- Acesso a composicao ---

    @property
    def inner(self) -> MemoryStore:
        """O adapter embrulhado. Uso legitimo: manutencao e testes."""
        return self._inner

    @property
    def profiles(self) -> ProfileStore:
        """Acesso a identidades, ja resolvido para o backend em uso."""
        return self._profiles

    @property
    def capabilities(self) -> StoreCapabilities:
        """Repasse: quem degrada e o backend, nao a camada."""
        return self._inner.capabilities

    # --- Identidades ---

    async def _identidades(self, requester_id: Optional[str]) -> list[str]:
        if not requester_id:
            return []
        try:
            return await self._profiles.resolve_identities(requester_id)
        except Exception as e:
            # Falhar a resolucao NAO pode virar "vejo tudo". Sem grupos,
            # o requester vale por si mesmo e a degradacao fica no log.
            logger.warning(
                "Nao foi possivel resolver os grupos de '%s': %s. "
                "A ACL vale apenas para a identidade direta.",
                requester_id,
                e,
            )
            return [requester_id]

    # --- CRUD ---

    async def store(self, entry: MemoryEntry) -> str:
        """DP-6: sem indice unico, a idempotencia vira best-effort declarado."""
        if not self.capabilities.unique_content_hash and entry.content_hash:
            existente = await self._inner.find_by_content_hash(
                entry.content_hash, entry.collection
            )
            if existente is not None:
                logger.debug(
                    "Backend '%s' nao tem indice unico; entry com content_hash "
                    "'%s' ja existe (id '%s') e o id existente foi reaproveitado.",
                    self.capabilities.backend,
                    entry.content_hash,
                    existente.id,
                )
                return existente.id
        return await self._inner.store(entry)

    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        return await self._inner.retrieve(entry_id)

    async def find_by_content_hash(
        self,
        content_hash: str,
        collection: Optional[str] = None,
    ) -> Optional[MemoryEntry]:
        return await self._inner.find_by_content_hash(content_hash, collection)

    async def update(
        self,
        entry_id: str,
        entry: MemoryEntry,
        requester_id: Optional[str] = None,
    ) -> bool:
        atual = await self._inner.retrieve(entry_id)
        if atual is not None:
            # I2: a protecao D2 vem ANTES da ACL de escrita e prevalece.
            assert_nao_protegida(atual, entry.source, "update")
            assert_pode_escrever(
                atual, requester_id, await self._identidades(requester_id)
            )
        versao_anterior = atual.version if atual else None

        atualizou = await self._inner.update(entry_id, entry, requester_id)

        if atualizou and versao_anterior is not None:
            await self._garantir_versionamento(entry_id, versao_anterior)
        return atualizou

    async def _garantir_versionamento(
        self, entry_id: str, versao_anterior: int
    ) -> None:
        """I7: `version` sobe exatamente 1 e a versao anterior fica guardada."""
        if not self.capabilities.versioning:
            logger.warning(
                "Backend '%s' declara versioning=False: a versao anterior da "
                "entry '%s' NAO foi arquivada.",
                self.capabilities.backend,
                entry_id,
            )
            return
        nova = await self._inner.retrieve(entry_id)
        if nova is None:
            return
        if nova.version != versao_anterior + 1:
            logger.warning(
                "Backend '%s' devolveu version=%s para a entry '%s' quando o "
                "esperado era %s. Invariante I7 violado pelo adapter.",
                self.capabilities.backend,
                nova.version,
                entry_id,
                versao_anterior + 1,
            )

    async def delete(
        self,
        entry_id: str,
        source: Source = "human",
        requester_id: Optional[str] = None,
    ) -> bool:
        atual = await self._inner.retrieve(entry_id)
        if atual is not None:
            assert_nao_protegida(atual, source, "delete")
            assert_pode_escrever(
                atual, requester_id, await self._identidades(requester_id)
            )
        return await self._inner.delete(entry_id, source, requester_id)

    # --- Busca ---

    def _limite_candidatos(self, limit: int, tem_requester: bool) -> int:
        cap = self.capabilities
        return limite_de_candidatos(
            limit,
            ordena_nativamente=cap.native_constitutional_order,
            filtra_acl_nativamente=cap.native_acl_filter,
            tem_requester=tem_requester,
            teto=self._teto_overfetch,
        )

    def _avisar_saturacao(
        self, operacao: str, pedidos: int, recebidos: int, limit: int
    ) -> None:
        """I6: a janela de candidatos encheu, entao a garantia pode ter sido tocada."""
        if pedidos > limit and recebidos >= pedidos:
            logger.warning(
                "Backend '%s' saturou a janela de candidatos em %s (%d de %d). "
                "Uma entry mandatory pode ter ficado de fora. Aumente "
                "[store.options].candidate_overfetch se isso se repetir.",
                self.capabilities.backend,
                operacao,
                recebidos,
                pedidos,
            )

    async def search_by_tags(
        self,
        tags: dict[str, list[str]],
        collection: Optional[str] = None,
        mandatory_only: bool = False,
        limit: int = 50,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        candidatos = self._limite_candidatos(limit, bool(requester_id))
        brutas = await self._inner.search_by_tags(
            tags, collection, mandatory_only, candidatos, requester_id
        )
        self._avisar_saturacao("search_by_tags", candidatos, len(brutas), limit)
        brutas = await self._garantir_mandatory(
            brutas, tags, collection, mandatory_only, candidatos, requester_id
        )

        entries = filtrar_ativas(brutas)
        entries = filtrar_sem_injection(entries)
        entries = filtrar_visiveis(
            entries, requester_id, await self._identidades(requester_id)
        )
        if mandatory_only:
            entries = [e for e in entries if e.mandatory]
        return ordenar_constitucional(entries)[:limit]


    async def _garantir_mandatory(
        self,
        brutas: list[MemoryEntry],
        tags: dict[str, list[str]],
        collection: Optional[str],
        mandatory_only: bool,
        candidatos: int,
        requester_id: Optional[str],
    ) -> list[MemoryEntry]:
        """I6: a janela de candidatos nao pode engolir uma entry `mandatory`.

        Backend que nao ordena `mandatory` primeiro devolve os candidatos
        pela ordem NEUTRA dele (`updated_at DESC, id ASC`). Se houver mais
        entries casando a consulta do que cabe na janela, uma regra
        obrigatoria antiga fica de fora e o contrato de
        `search_by_tags` ("entries mandatory que casam as tags sao SEMPRE
        incluidas", `store/base.py`) e violado.

        A correcao e uma segunda consulta com `mandatory_only=True`,
        exatamente a mesma dos demais parametros. Ela so acontece nos
        backends sem ordenacao nativa: no `pgvector` o SQL ja resolve e
        nada muda (nao-regressao do default, DP-9).
        """
        if mandatory_only or self.capabilities.native_constitutional_order:
            return brutas
        obrigatorias = await self._inner.search_by_tags(
            tags, collection, True, candidatos, requester_id
        )
        if len(obrigatorias) >= candidatos:
            logger.warning(
                "Backend '%s' saturou a janela de candidatos so com entries "
                "mandatory (%d). Alguma pode ter ficado de fora; aumente "
                "[store.options].candidate_overfetch.",
                self.capabilities.backend,
                len(obrigatorias),
            )
        vistos = {e.id for e in brutas}
        faltantes = [e for e in obrigatorias if e.id not in vistos]
        if faltantes:
            logger.debug(
                "Backend '%s': %d entries mandatory estavam fora da janela de "
                "candidatos e foram recuperadas por consulta dedicada.",
                self.capabilities.backend,
                len(faltantes),
            )
        return list(brutas) + faltantes

    async def _pos_busca_por_relevancia(
        self,
        brutas: list[MemoryEntry],
        limit: int,
        requester_id: Optional[str],
    ) -> list[MemoryEntry]:
        """Filtra e particiona preservando a ordem de relevancia (DD-4)."""
        entries = filtrar_ativas(brutas)
        entries = filtrar_visiveis(
            entries, requester_id, await self._identidades(requester_id)
        )
        return particionar_mandatory_primeiro(entries)[:limit]

    async def search_by_text(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        candidatos = self._limite_candidatos(limit, bool(requester_id))
        brutas = await self._inner.search_by_text(
            query, collection, candidatos, requester_id
        )
        self._avisar_saturacao("search_by_text", candidatos, len(brutas), limit)
        return await self._pos_busca_por_relevancia(brutas, limit, requester_id)

    async def search_semantic(
        self,
        embedding: list[float],
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        candidatos = self._limite_candidatos(limit, bool(requester_id))
        brutas = await self._inner.search_semantic(
            embedding, collection, candidatos, requester_id
        )
        self._avisar_saturacao("search_semantic", candidatos, len(brutas), limit)
        return await self._pos_busca_por_relevancia(brutas, limit, requester_id)

    async def search_semantic_text(
        self,
        query: str,
        collection: Optional[str] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        candidatos = self._limite_candidatos(limit, bool(requester_id))
        brutas = await self._inner.search_semantic_text(
            query, collection, candidatos, requester_id
        )
        self._avisar_saturacao("search_semantic_text", candidatos, len(brutas), limit)
        return await self._pos_busca_por_relevancia(brutas, limit, requester_id)

    # --- Collections ---

    async def list_collections(self) -> list[str]:
        return await self._inner.list_collections()

    async def count(self, collection: Optional[str] = None) -> int:
        return await self._inner.count(collection)

    # --- Hierarquia ---

    async def get_children(self, parent_id: str) -> list[MemoryEntry]:
        return await self._inner.get_children(parent_id)

    # --- Backfill (sem governanca por desenho, secao 4.3) ---

    async def list_entries_without_embedding(
        self,
        collection: Optional[str] = None,
        limit: int = 1000,
    ) -> list[MemoryEntry]:
        return await self._inner.list_entries_without_embedding(collection, limit)

    async def set_embedding(self, entry_id: str, embedding: list[float]) -> bool:
        return await self._inner.set_embedding(entry_id, embedding)

    # --- Manutencao ---

    async def get_history(self, entry_id: str) -> list[MemoryEntry]:
        return await self._inner.get_history(entry_id)

    async def gc_versions(self, max_age_days: int = 365) -> int:
        return await self._inner.gc_versions(max_age_days)

    async def garbage_collect(self) -> int:
        return await self._inner.garbage_collect()

    # --- Snapshots ---

    async def snapshot_commit(self, label: str, message: str = "") -> str:
        return await self._inner.snapshot_commit(label, message)

    async def snapshot_log(self, limit: int = 20) -> list[dict]:
        return await self._inner.snapshot_log(limit)

    async def snapshot_show(self, snapshot_id: str) -> dict:
        return await self._inner.snapshot_show(snapshot_id)

    async def snapshot_diff(self, snap_a: str, snap_b: str) -> dict:
        return await self._inner.snapshot_diff(snap_a, snap_b)

    async def snapshot_restore(self, snapshot_id: str) -> int:
        return await self._inner.snapshot_restore(snapshot_id)

    # --- Perfis (compatibilidade com os consumidores existentes) ---

    async def profile_get(self, profile_id: str) -> Optional[Profile]:
        return await self._profiles.get(profile_id)

    async def profile_create(self, profile: Profile) -> str:
        return await self._profiles.create(profile)

    async def profile_update(self, profile_id: str, profile: Profile) -> bool:
        return await self._profiles.update(profile_id, profile)

    async def profile_delete(self, profile_id: str) -> bool:
        return await self._profiles.delete(profile_id)

    async def profile_list(
        self, profile_type: Optional[str] = None
    ) -> list[Profile]:
        return await self._profiles.list(profile_type)

    async def profile_get_members(self, group_id: str) -> list[str]:
        return await self._profiles.get_members(group_id)

    async def resolve_identities(self, requester_id: str) -> list[str]:
        return await self._profiles.resolve_identities(requester_id)

    # --- Init / Teardown ---

    async def initialize(self) -> None:
        await self._inner.initialize()
        for aviso in self.capabilities.avisos():
            logger.info("Capacidade declarada: %s", aviso)

    async def close(self) -> None:
        await self._inner.close()

    def __repr__(self) -> str:
        return (
            f"GovernedStore(backend={self.capabilities.backend!r}, "
            f"inner={type(self._inner).__name__})"
        )

    # Espelho de leitura para quem ainda usa a assinatura antiga do
    # adapter. A implementacao unica vive em store/governance.py.
    def can_read(
        self,
        entry: MemoryEntry,
        requester_id: Optional[str],
        identities: Optional[list[str]] = None,
    ) -> bool:
        from orkmind.store.governance import pode_ler

        return pode_ler(entry, requester_id, identities)

    def __getattr__(self, nome: str) -> Any:
        raise AttributeError(
            f"'GovernedStore' nao expoe '{nome}'. Se e detalhe do backend, "
            f"use `store.inner`; se e governanca, ela vive nesta camada."
        )
