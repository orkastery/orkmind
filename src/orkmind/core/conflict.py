"""Deteccao de conflito entre regras mandatorias (heuristica estrutural v1)."""

from __future__ import annotations

from dataclasses import dataclass

from orkmind.core.dag import DAGEdge, DAGNode, RuleDAG
from orkmind.core.models import MemoryEntry


@dataclass
class ConflictPair:
    """Par de entries em conflito potencial."""

    entry_a_id: str
    entry_b_id: str
    dimension: str
    shared_tags: list[str]
    reason: str


def detect_conflicts(entries: list[MemoryEntry]) -> list[ConflictPair]:
    """Detecta conflitos estruturais entre entries mandatorias.

    Heuristica v1: mesma collection (rule/instruction), mesma dimensao,
    mesmas tags, ambas mandatory = conflito potencial.
    """
    # Filtrar apenas entries mandatory em collections de regra
    candidates = [
        e for e in entries
        if e.mandatory and e.collection in ("rule", "instruction")
    ]

    conflicts: list[ConflictPair] = []
    seen_pairs: set[tuple[str, str]] = set()

    for i, entry_a in enumerate(candidates):
        for entry_b in candidates[i + 1:]:
            if entry_a.id == entry_b.id:
                continue
            # Normalizar par para evitar duplicatas
            pair_key = tuple(sorted([entry_a.id, entry_b.id]))
            if pair_key in seen_pairs:
                continue

            # Verificar se compartilham collection
            if entry_a.collection != entry_b.collection:
                continue

            # Verificar sobreposicao de tags por dimensao
            for dim in entry_a.tags:
                if dim not in entry_b.tags:
                    continue
                shared = set(entry_a.tags[dim]) & set(entry_b.tags[dim])
                if shared:
                    conflicts.append(ConflictPair(
                        entry_a_id=entry_a.id,
                        entry_b_id=entry_b.id,
                        dimension=dim,
                        shared_tags=sorted(shared),
                        reason=(
                            f"Conflito estrutural: duas entries mandatory "
                            f"({entry_a.collection}) com tags sobrepostas "
                            f"na dimensao '{dim}': {sorted(shared)}"
                        ),
                    ))
                    seen_pairs.add(pair_key)
                    break  # Um conflito por par e suficiente

    return conflicts


def build_rule_dag(entries: list[MemoryEntry]) -> RuleDAG:
    """Constroi um DAG a partir de entries mandatorias com arestas de conflito.

    Cada entry mandatory vira um no. Pares em conflito recebem aresta
    bidirecional 'conflicts_with'. Ciclos sao detectados automaticamente.
    """
    dag = RuleDAG()

    # Adicionar nos para entries mandatory
    mandatory = [e for e in entries if e.mandatory]
    for entry in mandatory:
        dag.add_node(DAGNode(
            id=entry.id,
            entry_id=entry.id,
            node_type=entry.collection,
            metadata={"content": entry.content[:100]},
        ))

    # Detectar conflitos e adicionar arestas
    conflicts = detect_conflicts(entries)
    for conflict in conflicts:
        dag.add_edge(DAGEdge(
            source=conflict.entry_a_id,
            target=conflict.entry_b_id,
            edge_type="conflicts_with",
        ))
        dag.add_edge(DAGEdge(
            source=conflict.entry_b_id,
            target=conflict.entry_a_id,
            edge_type="conflicts_with",
        ))

    return dag
