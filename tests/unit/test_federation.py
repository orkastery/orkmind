"""I-14: one memory per project, cross-channel recall and profile ACL."""

from __future__ import annotations

import json

import pytest

from orkmind.core.models import MemoryEntry, Profile
from orkmind.federation import (
    FederatedMemory,
    FederatedSource,
    FederationManifest,
    federation_access,
    load_federation_manifest,
    set_federation_profile,
)
from orkmind.hermes.provider import OrkMindMemoryProvider
from orkmind.store.governed import GovernedStore
from orkmind.store.memory_adapter import MemoryAdapter
from orkmind.store.profiles import EntryProfileStore


async def source(name: str, project: str, producer: str) -> FederatedSource:
    adapter = MemoryAdapter()
    profiles = EntryProfileStore(adapter)
    store = GovernedStore(adapter, profiles)
    await store.initialize()
    return FederatedSource(name, project, producer, store)


def profile(identity: str, role: str, projects: list[str], current: str | None = None) -> Profile:
    federation = {"role": role, "projects": projects, "sources": ["*"]}
    if current:
        federation["current_project"] = current
    profile_type = (
        "agent" if role == "agent" else "system" if role == "system_owner" else "person"
    )
    return Profile(
        id=identity,
        display_name=identity,
        profile_type=profile_type,
        permissions={"federation": federation},
    )


@pytest.mark.asyncio
async def test_hermes_reads_ork_decision_with_both_acl_layers() -> None:
    ork = await source("orkastery", "orkastery", "ork")
    mind = await source("orkmind", "orkmind", "ork")
    private = await source("finance", "finance", "shared")
    all_sources = [ork, mind, private]
    try:
        tomas = profile("tomas", "builder", ["orkastery", "orkmind"])
        agent = profile("agent-plan", "agent", ["orkastery", "orkmind"], "orkastery")
        owner = profile("owner", "system_owner", [])
        for item in all_sources:
            assert isinstance(item.store, GovernedStore)
            for identity in (tomas, agent, owner):
                await item.store.profiles.create(identity)
        await ork.store.store(MemoryEntry(
            id="decision-ork-1",
            content="Usar CHECK cruzado antes do SHIP",
            collection="decision",
            tags={"skill": ["roadmap"], "audience": ["tomas"]},
            visibility="restricted",
            author_id="tomas",
            source="agent",
        ))
        await mind.store.store(MemoryEntry(
            id="decision-mind-1",
            content="Manter uma memória por projeto",
            collection="decision",
            tags={"skill": ["roadmap"]},
            visibility="public",
            source="agent",
        ))
        await private.store.store(MemoryEntry(
            id="decision-finance-1",
            content="Segredo financeiro",
            collection="decision",
            tags={"skill": ["roadmap"]},
            visibility="public",
        ))
        federation = FederatedMemory(all_sources, ork.store.profiles)

        recalled = await federation.recall(
            tags={"skill": ["roadmap"]},
            requester_id="tomas",
            caller="hermes",
            collection="decision",
        )
        assert [item.entry.id for item in recalled.entries] == [
            "decision-ork-1",
            "decision-mind-1",
        ]
        assert recalled.caller == "hermes"
        assert recalled.entries[0].producer == "ork"
        assert recalled.entries[0].project == "orkastery"
        assert recalled.denied_sources == ["finance"]

        via_provider = await OrkMindMemoryProvider(federation=federation).federated_recall(
            tags={"skill": ["roadmap"]},
            requester_id="tomas",
            collection="decision",
        )
        assert via_provider["caller"] == "hermes"
        assert via_provider["entries"][0]["entry"]["id"] == "decision-ork-1"

        # O agente só acessa seu projeto atual e ainda perde a entry restrita pela ACL interna.
        agent_recall = await federation.recall(
            tags={"skill": ["roadmap"]}, requester_id="agent-plan", caller="openclaw"
        )
        assert agent_recall.entries == []
        assert agent_recall.denied_sources == ["orkmind", "finance"]

        owner_recall = await federation.recall(
            tags={"skill": ["roadmap"]}, requester_id="owner", caller="ork"
        )
        assert {entry.project for entry in owner_recall.entries} == {"orkmind", "finance"}
        with pytest.raises(PermissionError, match="profile not found"):
            await federation.recall(tags={}, requester_id="ghost", caller="hermes")
    finally:
        for item in all_sources:
            await item.store.close()


def test_roles_fail_closed_and_manifest_never_contains_dsn(tmp_path) -> None:
    builder = profile("tomas", "builder", ["orkastery"])
    agent = profile("agent", "agent", ["orkastery"], "orkastery")
    owner = profile("owner", "system_owner", [])
    assert federation_access(builder, "orkastery", "source") is True
    assert federation_access(builder, "finance", "source") is False
    assert federation_access(agent, "orkastery", "source") is True
    assert federation_access(agent, "other", "source") is False
    assert federation_access(owner, "anything", "source") is True
    malformed = profile("bad", "builder", ["orkastery"])
    malformed.permissions = {"federation": {"role": "unknown", "projects": ["*"]}}
    assert federation_access(malformed, "orkastery", "source") is False

    manifest = {
        "schema": "orkmind.federation/v1",
        "identity_source": "orkastery",
        "sources": [{
            "name": "orkastery",
            "project": "orkastery",
            "producer": "ork",
            "backend": "pgvector",
            "database_url_env": "ORKASTERY_ORKMIND_DATABASE_URL",
        }],
    }
    file = tmp_path / "federation.json"
    file.write_text(json.dumps(manifest), encoding="utf-8")
    loaded = load_federation_manifest(file)
    assert loaded.sources[0].database_url_env == "ORKASTERY_ORKMIND_DATABASE_URL"
    assert "postgresql://" not in file.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="identity_source"):
        FederationManifest.model_validate({**manifest, "identity_source": "missing"})


@pytest.mark.asyncio
async def test_profile_command_model_creates_and_updates_explicit_roles() -> None:
    adapter = MemoryAdapter()
    profiles = EntryProfileStore(adapter)
    await adapter.initialize()
    try:
        created = await set_federation_profile(
            profiles,
            profile_id="tomas",
            display_name="Tomas",
            role="builder",
            projects=["orkastery", "orkmind", "orkastery"],
        )
        assert created.profile_type == "person"
        assert created.permissions["federation"]["projects"] == ["orkastery", "orkmind"]
        updated = await set_federation_profile(
            profiles,
            profile_id="tomas",
            display_name="Tomas Maestro",
            role="system_owner",
            projects=[],
        )
        assert updated.created_at == created.created_at
        assert updated.profile_type == "system"
        with pytest.raises(ValueError, match="current_project"):
            await set_federation_profile(
                profiles,
                profile_id="agent",
                display_name="Agente",
                role="agent",
                projects=["orkastery"],
                current_project="orkmind",
            )
    finally:
        await adapter.close()
