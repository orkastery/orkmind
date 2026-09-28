"""DAG engine minimo para OrkMind - grafo dirigido, topological sort, deteccao de ciclos."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class CycleError(Exception):
    """Levantada quando o grafo contem ciclos."""

    def __init__(self, cycles: list[list[str]]) -> None:
        self.cycles = cycles
        cycle_strs = [" -> ".join(c) for c in cycles]
        super().__init__(f"Ciclos detectados: {'; '.join(cycle_strs)}")


@dataclass
class DAGNode:
    """No do grafo dirigido."""

    id: str
    entry_id: Optional[str] = None
    node_type: str = "rule"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class DAGEdge:
    """Aresta do grafo dirigido."""

    source: str
    target: str
    edge_type: str = "depends_on"


class RuleDAG:
    """Grafo dirigido aciclico em memoria com topological sort e deteccao de ciclos."""

    def __init__(self) -> None:
        self.nodes: dict[str, DAGNode] = {}
        self.edges: list[DAGEdge] = []
        self._adjacency: dict[str, list[str]] = {}
        self._reverse: dict[str, list[str]] = {}

    def add_node(self, node: DAGNode) -> None:
        self.nodes[node.id] = node
        if node.id not in self._adjacency:
            self._adjacency[node.id] = []
        if node.id not in self._reverse:
            self._reverse[node.id] = []

    def add_edge(self, edge: DAGEdge) -> None:
        self.edges.append(edge)
        if edge.source not in self._adjacency:
            self._adjacency[edge.source] = []
        self._adjacency[edge.source].append(edge.target)
        if edge.target not in self._reverse:
            self._reverse[edge.target] = []
        self._reverse[edge.target].append(edge.source)

    def get_neighbors(self, node_id: str) -> list[str]:
        """Retorna vizinhos (successores) de um no."""
        return list(self._adjacency.get(node_id, []))

    def get_predecessors(self, node_id: str) -> list[str]:
        """Retorna predecessores de um no."""
        return list(self._reverse.get(node_id, []))

    def topological_sort(self) -> list[str]:
        """Retorna nos em ordem topologica (algoritmo de Kahn).

        Levanta CycleError se o grafo contiver ciclos.
        """
        # Calcular in-degree
        in_degree: dict[str, int] = {n: 0 for n in self.nodes}
        for edge in self.edges:
            if edge.target in in_degree:
                in_degree[edge.target] += 1

        # Fila com nos sem predecessores
        queue = [n for n, d in in_degree.items() if d == 0]
        queue.sort()  # Ordenacao estavel para determinismo
        result: list[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)
            for neighbor in sorted(self.get_neighbors(node)):
                if neighbor in in_degree:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)
                        queue.sort()

        if len(result) != len(self.nodes):
            cycles = self.detect_cycles()
            raise CycleError(cycles)

        return result

    def detect_cycles(self) -> list[list[str]]:
        """Detecta ciclos no grafo via DFS com coloracao.

        Retorna lista de ciclos encontrados (cada ciclo e uma lista de node_ids).
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {n: WHITE for n in self.nodes}
        parent: dict[str, Optional[str]] = {n: None for n in self.nodes}
        cycles: list[list[str]] = []

        def dfs(node: str) -> None:
            color[node] = GRAY
            for neighbor in sorted(self.get_neighbors(node)):
                if neighbor not in color:
                    continue
                if color[neighbor] == GRAY:
                    # Ciclo encontrado - reconstruir
                    cycle = [neighbor]
                    current = node
                    while current != neighbor:
                        cycle.append(current)
                        current = parent.get(current, neighbor)  # type: ignore[assignment]
                    cycle.append(neighbor)
                    cycle.reverse()
                    cycles.append(cycle)
                elif color[neighbor] == WHITE:
                    parent[neighbor] = node
                    dfs(neighbor)
            color[node] = BLACK

        for node in sorted(self.nodes.keys()):
            if color[node] == WHITE:
                dfs(node)

        return cycles
