"""Testes para deteccao de conflito entre regras."""

from orkmind.core.conflict import build_rule_dag, detect_conflicts
from orkmind.core.models import MemoryEntry


def _make_rule(content: str, tags: dict, mandatory: bool = True,
               collection: str = "rule") -> MemoryEntry:
    return MemoryEntry(
        content=content,
        collection=collection,  # type: ignore[arg-type]
        tags=tags,
        mandatory=mandatory,
    )


class TestDetectConflicts:
    def test_no_conflict_different_tags(self) -> None:
        entries = [
            _make_rule("regra A", {"skill": ["deploy"]}),
            _make_rule("regra B", {"skill": ["testing"]}),
        ]
        conflicts = detect_conflicts(entries)
        assert conflicts == []

    def test_no_conflict_non_mandatory(self) -> None:
        entries = [
            _make_rule("regra A", {"skill": ["deploy"]}, mandatory=False),
            _make_rule("regra B", {"skill": ["deploy"]}, mandatory=False),
        ]
        conflicts = detect_conflicts(entries)
        assert conflicts == []

    def test_conflict_same_tags_mandatory(self) -> None:
        entries = [
            _make_rule("use blue deployments", {"skill": ["deploy"]}),
            _make_rule("use green deployments", {"skill": ["deploy"]}),
        ]
        conflicts = detect_conflicts(entries)
        assert len(conflicts) == 1
        assert "deploy" in conflicts[0].shared_tags

    def test_conflict_same_dimension(self) -> None:
        entries = [
            _make_rule("regra A", {"domain": ["infra", "cloud"]}),
            _make_rule("regra B", {"domain": ["infra"]}),
        ]
        conflicts = detect_conflicts(entries)
        assert len(conflicts) == 1
        assert conflicts[0].dimension == "domain"
        assert "infra" in conflicts[0].shared_tags

    def test_conflict_marks_both_entries(self) -> None:
        a = _make_rule("regra A", {"skill": ["deploy"]})
        b = _make_rule("regra B", {"skill": ["deploy"]})
        conflicts = detect_conflicts([a, b])
        assert len(conflicts) == 1
        ids = {conflicts[0].entry_a_id, conflicts[0].entry_b_id}
        assert a.id in ids
        assert b.id in ids

    def test_conflict_different_collections_ok(self) -> None:
        entries = [
            _make_rule("regra", {"skill": ["deploy"]}, collection="rule"),
            _make_rule("fato", {"skill": ["deploy"]}, collection="fact"),
        ]
        conflicts = detect_conflicts(entries)
        assert conflicts == []

    def test_conflict_instruction_vs_instruction(self) -> None:
        entries = [
            _make_rule("instrucao A", {"skill": ["ci"]}, collection="instruction"),
            _make_rule("instrucao B", {"skill": ["ci"]}, collection="instruction"),
        ]
        conflicts = detect_conflicts(entries)
        assert len(conflicts) == 1


class TestBuildRuleDAG:
    def test_build_rule_dag(self) -> None:
        entries = [
            _make_rule("regra A", {"skill": ["deploy"]}),
            _make_rule("regra B", {"skill": ["testing"]}),
        ]
        dag = build_rule_dag(entries)
        assert len(dag.nodes) == 2
        assert len(dag.edges) == 0  # Sem conflito

    def test_build_rule_dag_with_conflicts(self) -> None:
        entries = [
            _make_rule("regra A", {"skill": ["deploy"]}),
            _make_rule("regra B", {"skill": ["deploy"]}),
        ]
        dag = build_rule_dag(entries)
        assert len(dag.nodes) == 2
        # Arestas bidirecionais de conflito
        assert len(dag.edges) == 2
        cycles = dag.detect_cycles()
        assert len(cycles) >= 1
