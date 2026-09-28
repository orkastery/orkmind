"""Tests for core models."""


from orkmind.core.models import ContextTags, MemoryEntry


class TestMemoryEntry:
    def test_defaults(self) -> None:
        entry = MemoryEntry(content="test", collection="fact")
        assert entry.content == "test"
        assert entry.collection == "fact"
        assert entry.priority == "medium"
        assert entry.mandatory is False
        assert entry.scope == "global"
        assert entry.source == "human"
        assert entry.version == 1
        assert entry.embedding is None
        assert entry.metadata == {}
        assert entry.tags == {}
        assert entry.expires_at is None
        assert entry.id  # UUID generated
        assert entry.created_at.tzinfo is not None

    def test_all_collections_valid(self) -> None:
        from orkmind.core.models import VALID_COLLECTIONS
        for coll in VALID_COLLECTIONS:
            entry = MemoryEntry(content="x", collection=coll)  # type: ignore[arg-type]
            assert entry.collection == coll

    def test_23_collections(self) -> None:
        from orkmind.core.models import VALID_COLLECTIONS
        assert len(VALID_COLLECTIONS) == 23

    def test_tags_structure(self) -> None:
        entry = MemoryEntry(
            content="test",
            collection="rule",
            tags={"skill": ["git", "deploy"], "domain": ["infra"]},
        )
        assert entry.tags["skill"] == ["git", "deploy"]
        assert entry.tags["domain"] == ["infra"]

    def test_protected_default_false(self) -> None:
        entry = MemoryEntry(content="test", collection="fact")
        assert entry.protected is False

    def test_content_hash_default_none(self) -> None:
        entry = MemoryEntry(content="test", collection="fact")
        assert entry.content_hash is None

    def test_injection_risk_default_false(self) -> None:
        entry = MemoryEntry(content="test", collection="fact")
        assert entry.injection_risk is False

    def test_conflict_default_false(self) -> None:
        entry = MemoryEntry(content="test", collection="fact")
        assert entry.conflict is False

    def test_serialization_roundtrip(self) -> None:
        entry = MemoryEntry(
            content="test rule",
            collection="rule",
            tags={"skill": ["deploy"]},
            priority="critical",
            mandatory=True,
        )
        data = entry.model_dump()
        restored = MemoryEntry(**data)
        assert restored.content == entry.content
        assert restored.collection == entry.collection
        assert restored.mandatory is True


class TestContextTags:
    def test_empty(self) -> None:
        tags = ContextTags()
        assert tags.to_dict() == {}

    def test_to_dict_filters_empty(self) -> None:
        tags = ContextTags(skill=["git"], domain=[])
        result = tags.to_dict()
        assert result == {"skill": ["git"]}
        assert "domain" not in result

    def test_merge(self) -> None:
        a = ContextTags(skill=["git"], domain=["backend"])
        b = ContextTags(skill=["deploy"], situation=["deploy"])
        merged = a.merge(b)
        assert set(merged.skill) == {"git", "deploy"}
        assert merged.domain == ["backend"]
        assert merged.situation == ["deploy"]

    def test_merge_deduplicates(self) -> None:
        a = ContextTags(skill=["git"])
        b = ContextTags(skill=["git"])
        merged = a.merge(b)
        assert merged.skill == ["git"]
