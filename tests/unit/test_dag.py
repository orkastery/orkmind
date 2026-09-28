"""Testes para o DAG engine minimo."""

import pytest

from orkmind.core.dag import CycleError, DAGEdge, DAGNode, RuleDAG


class TestRuleDAG:
    def test_add_node_and_edge(self) -> None:
        dag = RuleDAG()
        dag.add_node(DAGNode(id="a"))
        dag.add_node(DAGNode(id="b"))
        dag.add_edge(DAGEdge(source="a", target="b"))
        assert "a" in dag.nodes
        assert "b" in dag.nodes
        assert len(dag.edges) == 1

    def test_get_neighbors(self) -> None:
        dag = RuleDAG()
        dag.add_node(DAGNode(id="a"))
        dag.add_node(DAGNode(id="b"))
        dag.add_node(DAGNode(id="c"))
        dag.add_edge(DAGEdge(source="a", target="b"))
        dag.add_edge(DAGEdge(source="a", target="c"))
        neighbors = dag.get_neighbors("a")
        assert set(neighbors) == {"b", "c"}
        assert dag.get_neighbors("b") == []

    def test_get_predecessors(self) -> None:
        dag = RuleDAG()
        dag.add_node(DAGNode(id="a"))
        dag.add_node(DAGNode(id="b"))
        dag.add_node(DAGNode(id="c"))
        dag.add_edge(DAGEdge(source="a", target="c"))
        dag.add_edge(DAGEdge(source="b", target="c"))
        preds = dag.get_predecessors("c")
        assert set(preds) == {"a", "b"}

    def test_topological_sort_linear(self) -> None:
        dag = RuleDAG()
        dag.add_node(DAGNode(id="a"))
        dag.add_node(DAGNode(id="b"))
        dag.add_node(DAGNode(id="c"))
        dag.add_edge(DAGEdge(source="a", target="b"))
        dag.add_edge(DAGEdge(source="b", target="c"))
        order = dag.topological_sort()
        assert order.index("a") < order.index("b")
        assert order.index("b") < order.index("c")

    def test_topological_sort_diamond(self) -> None:
        dag = RuleDAG()
        dag.add_node(DAGNode(id="a"))
        dag.add_node(DAGNode(id="b"))
        dag.add_node(DAGNode(id="c"))
        dag.add_node(DAGNode(id="d"))
        dag.add_edge(DAGEdge(source="a", target="b"))
        dag.add_edge(DAGEdge(source="a", target="c"))
        dag.add_edge(DAGEdge(source="b", target="d"))
        dag.add_edge(DAGEdge(source="c", target="d"))
        order = dag.topological_sort()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_cycle_detection_no_cycle(self) -> None:
        dag = RuleDAG()
        dag.add_node(DAGNode(id="a"))
        dag.add_node(DAGNode(id="b"))
        dag.add_edge(DAGEdge(source="a", target="b"))
        cycles = dag.detect_cycles()
        assert cycles == []

    def test_cycle_detection_simple(self) -> None:
        dag = RuleDAG()
        dag.add_node(DAGNode(id="a"))
        dag.add_node(DAGNode(id="b"))
        dag.add_edge(DAGEdge(source="a", target="b"))
        dag.add_edge(DAGEdge(source="b", target="a"))
        cycles = dag.detect_cycles()
        assert len(cycles) >= 1
        # Topological sort deve levantar CycleError
        with pytest.raises(CycleError):
            dag.topological_sort()

    def test_cycle_detection_complex(self) -> None:
        dag = RuleDAG()
        dag.add_node(DAGNode(id="a"))
        dag.add_node(DAGNode(id="b"))
        dag.add_node(DAGNode(id="c"))
        dag.add_edge(DAGEdge(source="a", target="b"))
        dag.add_edge(DAGEdge(source="b", target="c"))
        dag.add_edge(DAGEdge(source="c", target="a"))
        cycles = dag.detect_cycles()
        assert len(cycles) >= 1
        # Verifica que os 3 nos participam de algum ciclo
        all_nodes_in_cycles = {n for cycle in cycles for n in cycle}
        assert {"a", "b", "c"}.issubset(all_nodes_in_cycles)
