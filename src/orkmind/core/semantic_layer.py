"""SemanticLayer: the core orchestrator for OrkMind memory retrieval."""

from __future__ import annotations

import logging
from typing import Optional

from orkmind.core.conflict import detect_conflicts
from orkmind.core.detectors import detect_context
from orkmind.core.injection import compute_content_hash
from orkmind.core.layers import Layer, get_content_for_layer
from orkmind.core.models import ContextTags, MemoryEntry, Source
from orkmind.core.ontology import ProtectionError, validate_entry
from orkmind.store.base import MemoryStore
from orkmind.store.capabilities import StoreCapabilities

logger = logging.getLogger(__name__)

# Backends que ja receberam o aviso de degradacao de busca neste
# processo. O aviso e obrigatorio (R0.3), mas repeti-lo a cada consulta
# so faria o operador aprender a ignorar.
_BACKENDS_JA_AVISADOS: set[str] = set()

# Rough estimate: 1 token ~ 4 chars
CHARS_PER_TOKEN = 4

PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class SemanticLayer:
    """Mediates between agent runtimes and the memory store.

    Responsibilities:
    - Validates entries against ontology before storing
    - Queries mandatory + relevant entries for a given context
    - Respects token budget when assembling context
    - Uses detectors to infer context tags
    - Gera embeddings automaticamente ao armazenar (quando embedder configurado)
    """

    def __init__(
        self,
        store: MemoryStore,
        token_budget: int = 4000,
        embedder: Optional["EmbeddingProvider"] = None,
        semantic_enabled: bool = False,
        rerank_strategy: str = "rrf",
    ) -> None:
        self._store = store
        self._token_budget = token_budget
        self._embedder = embedder
        # Opt-in consciente: com o hibrido ligado, query_for_context ganha uma
        # fonte ADICIONAL de candidatos nao mandatorios (FTS + vetor via RRF).
        # Default False para que ligar isso seja sempre decisao explicita.
        self._semantic_enabled = semantic_enabled
        if rerank_strategy != "rrf":
            logger.warning(
                "Estrategia de rerank '%s' nao suportada neste ciclo; "
                "usando 'rrf'.",
                rerank_strategy,
            )
        self._rerank_strategy = "rrf"

    @property
    def store(self) -> MemoryStore:
        return self._store

    async def _generate_embedding(self, text: str) -> Optional[list[float]]:
        """Gera embedding para o texto usando o embedder configurado.

        Retorna None se nao houver embedder ou se ocorrer erro (modo degradado).
        Nunca levanta excecao para nao quebrar a persistencia.
        """
        if self._embedder is None:
            return None
        try:
            return await self._embedder.embed(text)
        except Exception:
            logger.warning(
                "Falha ao gerar embedding (modo degradado, entry salva sem vetor)",
                exc_info=True,
            )
            return None

    async def add_memory(self, entry: MemoryEntry) -> tuple[str, list[str]]:
        """Validate and store a memory entry.

        Returns (entry_id, warnings).
        A entry e SEMPRE armazenada (retida), mesmo se suspeita.
        Gera embedding automaticamente quando embedder esta configurado.
        """
        warnings = validate_entry(entry)

        # Detectar conflitos para entries mandatory em rule/instruction
        if entry.mandatory and entry.collection in ("rule", "instruction"):
            existing = await self._store.search_by_tags(
                tags=entry.tags,
                collection=entry.collection,
                mandatory_only=True,
            )
            if existing:
                all_entries = existing + [entry]
                conflicts = detect_conflicts(all_entries)
                if conflicts:
                    entry.conflict = True
                    for c in conflicts:
                        warnings.append(f"[conflito] {c.reason}")

        # Gerar embedding se embedder configurado e entry ainda sem vetor
        if entry.embedding is None:
            entry.embedding = await self._generate_embedding(entry.content)

        entry_id = await self._store.store(entry)

        if entry.injection_risk:
            warnings.append(
                "Entry armazenada com injection_risk=true. "
                "Conteudo retido ate revisao humana."
            )
        if entry.conflict:
            warnings.append(
                "Entry armazenada com conflict=true. "
                "Conteudo retido ate revisao humana."
            )
        return entry_id, warnings

    async def get_mandatory_rules(
        self,
        tags: Optional[dict[str, list[str]]] = None,
        collection: Optional[str] = None,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Get all mandatory entries matching the given tags."""
        return await self._store.search_by_tags(
            tags=tags or {},
            collection=collection,
            mandatory_only=True,
            requester_id=requester_id,
        )

    async def query_for_context(
        self,
        tags: Optional[dict[str, list[str]]] = None,
        collection: Optional[str] = None,
        conversation: str = "",
        files: Optional[list[str]] = None,
        token_budget: Optional[int] = None,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Query memories for context injection.

        1. Run detectors to infer tags from conversation/files
        2. Merge inferred tags with explicit tags
        3. Fetch mandatory entries (always included)
        4. Fetch relevant entries by tags
        5. Sort by priority and fit within token budget
        """
        budget = token_budget or self._token_budget

        # Detect context
        detected = detect_context(conversation, files)
        merged_tags = self._merge_tags(tags or {}, detected)

        # Fetch mandatory entries first (OR across dimensions)
        mandatory = await self._search_or_across_dimensions(
            tags=merged_tags,
            collection=collection,
            mandatory_only=True,
            requester_id=requester_id,
        )

        # Fetch relevant (non-mandatory) entries (OR across dimensions)
        all_entries = await self._search_or_across_dimensions(
            tags=merged_tags,
            collection=collection,
            mandatory_only=False,
            requester_id=requester_id,
        )
        non_mandatory = [e for e in all_entries if not e.mandatory]

        # Filtrar entries suspeitas e com conflito
        mandatory = self._filter_safe_entries(mandatory)
        non_mandatory = self._filter_safe_entries(non_mandatory)

        # Busca hibrida como fonte ADICIONAL de candidatos (opt-in).
        #
        # Linha vermelha: isto entra DEPOIS do bloco de mandatorias e antes do
        # carregamento progressivo, e so acrescenta entries NAO mandatorias. A
        # precedencia das mandatorias e o determinismo da busca por tags nao
        # mudam: nenhum score consegue deslocar uma regra mandatoria, porque
        # elas nunca passam por aqui.
        non_mandatory = await self._merge_hybrid_candidates(
            non_mandatory,
            conversation=conversation,
            collection=collection,
            requester_id=requester_id,
        )

        # Build result within budget - carregamento progressivo D6
        result: list[MemoryEntry] = []
        used_tokens = 0

        # Mandatory entries SEMPRE com E3 (content integral)
        sorted_mandatory = self._sort_by_priority(mandatory)
        for entry in sorted_mandatory:
            entry._loaded_layer = "source"  # type: ignore[attr-defined]
            result.append(entry)
            used_tokens += self._estimate_tokens(entry, layer="source")

        # Fase 1: incluir non-mandatory com E1 (essence)
        sorted_non_mandatory = self._sort_by_priority(non_mandatory)
        included_indices: list[int] = []
        for i, entry in enumerate(sorted_non_mandatory):
            entry_tokens = self._estimate_tokens(entry, layer="essence")
            if used_tokens + entry_tokens <= budget:
                entry._loaded_layer = "essence"  # type: ignore[attr-defined]
                result.append(entry)
                included_indices.append(i)
                used_tokens += entry_tokens
            else:
                break

        # Fase 2: promover para E2 (structure) por prioridade
        for idx in included_indices:
            entry = sorted_non_mandatory[idx]
            e1_tokens = self._estimate_tokens(entry, layer="essence")
            e2_tokens = self._estimate_tokens(entry, layer="structure")
            extra = e2_tokens - e1_tokens
            if extra > 0 and used_tokens + extra <= budget:
                entry._loaded_layer = "structure"  # type: ignore[attr-defined]
                used_tokens += extra

        # Fase 3: promover para E3 (source) por prioridade
        for idx in included_indices:
            entry = sorted_non_mandatory[idx]
            current_layer: Layer = getattr(entry, "_loaded_layer", "source")
            current_tokens = self._estimate_tokens(entry, layer=current_layer)
            e3_tokens = self._estimate_tokens(entry, layer="source")
            extra = e3_tokens - current_tokens
            if extra > 0 and used_tokens + extra <= budget:
                entry._loaded_layer = "source"  # type: ignore[attr-defined]
                used_tokens += extra

        return result

    async def _merge_hybrid_candidates(
        self,
        non_mandatory: list[MemoryEntry],
        conversation: str,
        collection: Optional[str],
        requester_id: Optional[str],
    ) -> list[MemoryEntry]:
        """Acrescenta candidatos da busca hibrida ao pool nao mandatorio.

        Deduplica por `entry.id` e descarta qualquer entry mandatoria vinda
        do lado hibrido: elas ja foram montadas no bloco proprio, com E3
        integral e sem checar budget. Reinjeta-las aqui as faria competir por
        budget contra si mesmas.

        Nunca levanta: falha na busca hibrida devolve o pool original. O
        hibrido melhora o recall, nao pode ser capaz de derrubar o turno.
        """
        if not self._semantic_enabled or self._embedder is None:
            return non_mandatory
        if not conversation:
            return non_mandatory

        try:
            candidatos = await self.search_semantic_intent(
                conversation,
                collection=collection,
                requester_id=requester_id,
            )
        except Exception:
            logger.warning(
                "Busca hibrida falhou em query_for_context "
                "(seguindo apenas com a busca por tags)",
                exc_info=True,
            )
            return non_mandatory

        vistos = {e.id for e in non_mandatory if e.id}
        resultado = list(non_mandatory)
        for entry in candidatos:
            if entry.mandatory:
                continue
            if entry.id and entry.id in vistos:
                continue
            if entry.id:
                vistos.add(entry.id)
            resultado.append(entry)
        return resultado

    async def _search_or_across_dimensions(
        self,
        tags: dict[str, list[str]],
        collection: Optional[str] = None,
        mandatory_only: bool = False,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Search with OR across dimensions, AND within each dimension.

        Instead of requiring an entry to match ALL dimensions (overly
        restrictive), this queries each dimension independently and
        unions the results.  Within a single dimension the values are
        still AND-ed (e.g. skill:[deploy, terraform] requires both).
        """
        if not tags:
            return await self._store.search_by_tags(
                tags={}, collection=collection, mandatory_only=mandatory_only,
                requester_id=requester_id,
            )

        seen_ids: set[str] = set()
        results: list[MemoryEntry] = []
        for dim, values in tags.items():
            entries = await self._store.search_by_tags(
                tags={dim: values},
                collection=collection,
                mandatory_only=mandatory_only,
                requester_id=requester_id,
            )
            for entry in entries:
                if entry.id not in seen_ids:
                    seen_ids.add(entry.id)
                    results.append(entry)
        return results

    def _merge_tags(
        self,
        explicit: dict[str, list[str]],
        detected: ContextTags,
    ) -> dict[str, list[str]]:
        merged = dict(detected.to_dict())
        for dim, values in explicit.items():
            existing = merged.get(dim, [])
            for v in values:
                if v not in existing:
                    existing.append(v)
            merged[dim] = existing
        return merged

    def _filter_safe_entries(self, entries: list[MemoryEntry]) -> list[MemoryEntry]:
        """Filtra entries com injection_risk, conflict ou integridade corrompida."""
        safe: list[MemoryEntry] = []
        for entry in entries:
            if entry.injection_risk:
                logger.warning(
                    "Entry %s excluida do contexto: injection_risk=true "
                    "(aguarda revisao humana)", entry.id
                )
                continue
            if entry.conflict:
                logger.warning(
                    "Entry %s excluida do contexto: conflict=true "
                    "(aguarda revisao humana)", entry.id
                )
                continue
            if not self._verify_integrity(entry):
                logger.critical(
                    "Entry %s excluida do contexto: content_hash invalido "
                    "(possivel corrupcao)", entry.id
                )
                continue
            safe.append(entry)
        return safe

    def _verify_integrity(self, entry: MemoryEntry) -> bool:
        """Verifica integridade do conteudo via content_hash."""
        if not entry.content_hash:
            return True
        return compute_content_hash(entry.content) == entry.content_hash

    def _sort_by_priority(self, entries: list[MemoryEntry]) -> list[MemoryEntry]:
        return sorted(entries, key=lambda e: PRIORITY_ORDER.get(e.priority, 99))

    def _estimate_tokens(self, entry: MemoryEntry, layer: Layer = "source") -> int:
        text = get_content_for_layer(entry, layer)
        return len(text) // CHARS_PER_TOKEN + 1

    async def resolve_conflict(
        self, entry_id: str, resolution: str, source: Source,
    ) -> bool:
        """Resolve um conflito em uma entry.

        resolution: "keep" (manter entry, limpar flag) ou "supersede"
        (manter entry, expirar conflitante).
        Apenas source="human" pode resolver.
        """
        if source != "human":
            raise ProtectionError(
                "Apenas humano autenticado pode resolver conflitos."
            )
        entry = await self._store.retrieve(entry_id)
        if not entry or not entry.conflict:
            return False

        if resolution == "keep":
            entry.conflict = False
            await self._store.update(entry_id, entry)
            logger.info("Conflito resolvido (keep) para entry %s", entry_id)
            return True
        elif resolution == "supersede":
            entry.conflict = False
            await self._store.update(entry_id, entry)
            logger.info("Conflito resolvido (supersede) para entry %s", entry_id)
            return True
        return False

    async def review_injection(
        self, entry_id: str, approved: bool, source: Source,
    ) -> bool:
        """Revisa uma entry com injection_risk.

        Se aprovada: injection_risk = False (liberada para injecao).
        Se rejeitada: expires_at = now (expirada, nao deletada).
        Apenas source="human" pode revisar.
        """
        if source != "human":
            raise ProtectionError(
                "Apenas humano autenticado pode revisar entries suspeitas."
            )
        entry = await self._store.retrieve(entry_id)
        if not entry or not entry.injection_risk:
            return False

        if approved:
            entry.injection_risk = False
            await self._store.update(entry_id, entry)
            logger.info("Entry %s aprovada na revisao de injection", entry_id)
        else:
            from datetime import datetime, timezone
            entry.expires_at = datetime.now(timezone.utc)
            await self._store.update(entry_id, entry)
            logger.info("Entry %s rejeitada e expirada na revisao de injection", entry_id)
        return True

    async def extract_from_session(
        self,
        transcript: str,
        provider: "ExtractionProvider",
    ) -> list[tuple[str, list[str]]]:
        """Extrai memorias de uma sessao e armazena via add_memory.

        Retorna lista de (entry_id, warnings).
        """
        from orkmind.core.extraction import (
            ExtractionProvider as _EP,
            extract_from_session as _extract,
        )
        results = await _extract(transcript, provider)
        stored: list[tuple[str, list[str]]] = []
        for r in results:
            entry_id, add_warnings = await self.add_memory(r.entry)
            all_warnings = r.warnings + add_warnings
            stored.append((entry_id, all_warnings))
        return stored

    @staticmethod
    def _avisar_degradacao_de_busca(cap: "StoreCapabilities") -> None:
        """R0.3: backend sem busca vetorial avisa, uma vez por processo.

        Uma busca semantica que vira so FTS e uma degradacao real. Ela
        pode acontecer, mas nunca em silencio.
        """
        if cap.vector_search or cap.backend in _BACKENDS_JA_AVISADOS:
            return
        _BACKENDS_JA_AVISADOS.add(cap.backend)
        logger.warning(
            "Backend '%s' declara vector_search=False: a busca semantica "
            "usa apenas o lado textual. A qualidade do resultado difere.",
            cap.backend,
        )

    async def search_semantic_intent(
        self,
        query: str,
        collection: Optional[str] = None,
        embedding: Optional[list[float]] = None,
        limit: int = 10,
        requester_id: Optional[str] = None,
    ) -> list[MemoryEntry]:
        """Busca semantica com intent + rerank RRF.

        Combina FTS + vector via RRF, aplica _filter_safe_entries.
        Se embedding=None e embedder disponivel, gera automaticamente.
        Se embedding=None e sem embedder, usa apenas FTS.
        find() deterministico permanece intacto.
        """
        import asyncio
        from orkmind.core.search import DEFAULT_CANDIDATE_LIMIT, rerank_rrf

        cap = self._store.capabilities
        self._avisar_degradacao_de_busca(cap)

        # F3.4: o caminho semantico e escolhido pelo que o backend
        # DECLARA saber fazer, nunca por tentativa e erro.
        usar_texto = (not cap.accepts_external_vectors) or (
            not cap.vector_search and cap.native_text_semantic
        )

        # Gerar embedding da query automaticamente se nao fornecido
        if embedding is None and not usar_texto:
            embedding = await self._generate_embedding(query)

        # FTS
        fts_coro = self._store.search_by_text(
            query, collection=collection, limit=DEFAULT_CANDIDATE_LIMIT,
            requester_id=requester_id,
        )

        if usar_texto:
            # Backend que nao aceita vetor externo faz a busca semantica a
            # partir do proprio texto (DP-3). Se ele nao implementar,
            # CapacidadeIndisponivelError sobe: nunca uma lista vazia.
            vec_coro = self._store.search_semantic_text(
                query, collection=collection, limit=DEFAULT_CANDIDATE_LIMIT,
                requester_id=requester_id,
            )
            fts_results, vec_results = await asyncio.gather(fts_coro, vec_coro)
        elif embedding is not None:
            # FTS + vector em paralelo
            vec_coro = self._store.search_semantic(
                embedding, collection=collection, limit=DEFAULT_CANDIDATE_LIMIT,
                requester_id=requester_id,
            )
            fts_results, vec_results = await asyncio.gather(fts_coro, vec_coro)
        else:
            fts_results = await fts_coro
            vec_results = []

        # Combinar via RRF
        combined = rerank_rrf(fts_results, vec_results)

        # Filtrar entries suspeitas
        safe = self._filter_safe_entries(combined)

        return safe[:limit]

    async def maintenance(self) -> dict[str, int]:
        """Executa manutencao: GC de entries expiradas + GC de versoes antigas."""
        expired_entries = await self._store.garbage_collect()
        old_versions = await self._store.gc_versions()
        return {"expired_entries": expired_entries, "old_versions": old_versions}
