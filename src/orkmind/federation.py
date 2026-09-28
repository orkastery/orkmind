"""Federated, multi-project recall with profile and entry ACLs."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from orkmind.core.config import OrkMindConfig, load_config
from orkmind.core.models import MemoryEntry, Profile
from orkmind.store.base import MemoryStore
from orkmind.store.factory import create_store
from orkmind.store.governed import GovernedStore
from orkmind.store.profiles import ProfileStore

Caller = Literal["ork", "hermes", "openclaw", "codex", "claude-code"]
FederationRole = Literal["system_owner", "builder", "agent"]


class SourceConfig(BaseModel):
    """One independently stored project memory."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")
    project: str = Field(min_length=1, max_length=80)
    producer: Literal["ork", "hermes", "openclaw", "shared"] = "ork"
    backend: str = "pgvector"
    database_url_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,99}$")
    options: dict[str, Any] = Field(default_factory=dict)


class FederationManifest(BaseModel):
    schema_: Literal["orkmind.federation/v1"] = Field(
        default="orkmind.federation/v1", alias="schema", serialization_alias="schema"
    )
    identity_source: str
    sources: list[SourceConfig]

    @field_validator("sources")
    @classmethod
    def sources_are_unique(cls, values: list[SourceConfig]) -> list[SourceConfig]:
        names = [source.name for source in values]
        if not values or len(names) != len(set(names)):
            raise ValueError("federation sources must be non-empty and unique")
        return values

    @model_validator(mode="after")
    def identity_exists(self) -> "FederationManifest":
        if self.identity_source not in {source.name for source in self.sources}:
            raise ValueError("identity_source must name one configured source")
        return self


@dataclass(frozen=True)
class FederatedSource:
    name: str
    project: str
    producer: str
    store: MemoryStore


@dataclass(frozen=True)
class FederatedEntry:
    source: str
    project: str
    producer: str
    entry: MemoryEntry

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "project": self.project,
            "producer": self.producer,
            "entry": self.entry.model_dump(mode="json"),
        }


@dataclass
class FederatedRecall:
    requester_id: str
    caller: Caller
    entries: list[FederatedEntry] = field(default_factory=list)
    denied_sources: list[str] = field(default_factory=list)
    unavailable_sources: dict[str, str] = field(default_factory=dict)
    schema: str = "orkmind.federated-recall/v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "requester_id": self.requester_id,
            "caller": self.caller,
            "entries": [entry.as_dict() for entry in self.entries],
            "denied_sources": self.denied_sources,
            "unavailable_sources": self.unavailable_sources,
        }


def federation_access(profile: Profile, project: str, source: str) -> bool:
    """Fail-closed source ACL. Entry ACL still runs inside each GovernedStore."""
    permissions = profile.permissions.get("federation", {})
    if not isinstance(permissions, dict):
        return False
    role: str = str(permissions.get("role", ""))
    projects = permissions.get("projects", [])
    sources = permissions.get("sources", [])
    if role == "system_owner":
        return True
    if not isinstance(projects, list) or not isinstance(sources, list):
        return False
    project_allowed = "*" in projects or project in projects
    source_allowed = not sources or "*" in sources or source in sources
    if role == "builder":
        return project_allowed and source_allowed
    if role == "agent":
        current = permissions.get("current_project")
        return current == project and project_allowed and source_allowed
    return False


async def set_federation_profile(
    profiles: ProfileStore,
    *,
    profile_id: str,
    display_name: str,
    role: FederationRole,
    projects: list[str],
    sources: list[str] | None = None,
    current_project: str | None = None,
) -> Profile:
    """Create/update the three explicit federation profiles."""
    if not profile_id.strip() or not display_name.strip():
        raise ValueError("profile id and display name are required")
    projects = list(dict.fromkeys(project.strip() for project in projects if project.strip()))
    if role != "system_owner" and not projects:
        raise ValueError("builder and agent profiles require projects")
    if role == "agent" and (not current_project or current_project not in projects):
        raise ValueError("agent current_project must be one of its projects")
    permission: dict[str, Any] = {
        "role": role,
        "projects": projects,
        "sources": list(dict.fromkeys(sources or ["*"])),
    }
    if current_project:
        permission["current_project"] = current_project
    existing = await profiles.get(profile_id)
    profile_data: dict[str, Any] = {
        "id": profile_id,
        "display_name": display_name,
        "profile_type": (
            "agent"
            if role == "agent"
            else "system"
            if role == "system_owner"
            else "person"
        ),
        "parent_id": existing.parent_id if existing else None,
        "permissions": {
            **(existing.permissions if existing else {}),
            "federation": permission,
        },
        "metadata": existing.metadata if existing else {},
    }
    if existing:
        profile_data["created_at"] = existing.created_at
    profile = Profile.model_validate(profile_data)
    if existing:
        await profiles.update(profile_id, profile)
    else:
        await profiles.create(profile)
    return profile


class FederatedMemory:
    """Read-only fan-out across one store per project."""

    def __init__(self, sources: list[FederatedSource], profiles: ProfileStore) -> None:
        if not sources:
            raise ValueError("federation requires sources")
        self.sources = list(sources)
        self.profiles = profiles

    async def recall(
        self,
        *,
        tags: dict[str, list[str]],
        requester_id: str,
        caller: Caller,
        collection: str | None = None,
        limit: int = 50,
    ) -> FederatedRecall:
        if not requester_id.strip():
            raise ValueError("federated recall requires requester_id")
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        profile = await self.profiles.get(requester_id)
        if profile is None:
            raise PermissionError("requester profile not found")
        result = FederatedRecall(requester_id=requester_id, caller=caller)
        allowed = []
        for source in self.sources:
            if federation_access(profile, source.project, source.name):
                allowed.append(source)
            else:
                result.denied_sources.append(source.name)

        async def query(
            source: FederatedSource,
        ) -> tuple[FederatedSource, list[MemoryEntry] | Exception]:
            try:
                entries = await source.store.search_by_tags(
                    tags, collection=collection, limit=limit, requester_id=requester_id
                )
                return source, entries
            except Exception as exc:  # one project must not erase healthy results
                return source, exc

        for source, value in await asyncio.gather(*(query(source) for source in allowed)):
            if isinstance(value, Exception):
                result.unavailable_sources[source.name] = type(value).__name__
                continue
            result.entries.extend(
                FederatedEntry(source.name, source.project, source.producer, entry)
                for entry in value
            )
        priority = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        result.entries.sort(key=lambda item: (
            not item.entry.mandatory,
            priority[item.entry.priority],
            item.source,
            item.entry.id,
        ))
        result.entries = result.entries[:limit]
        return result


def load_federation_manifest(path: Path) -> FederationManifest:
    return FederationManifest.model_validate_json(path.expanduser().read_text(encoding="utf-8"))


async def open_federation(
    manifest: FederationManifest,
    base_config: OrkMindConfig | None = None,
) -> tuple[FederatedMemory, list[MemoryStore]]:
    """Open configured stores without putting DSNs in the manifest."""
    base = base_config or load_config()
    opened: list[MemoryStore] = []
    sources: list[FederatedSource] = []
    try:
        for source in manifest.sources:
            database_url = os.environ.get(source.database_url_env, "")
            if not database_url and source.backend not in {"memory"}:
                raise ValueError(f"missing environment variable {source.database_url_env}")
            config = replace(
                base,
                store_backend=source.backend,
                database_url=database_url,
                store_options=dict(source.options),
            )
            store = create_store(config)
            await store.initialize()
            opened.append(store)
            sources.append(FederatedSource(source.name, source.project, source.producer, store))
        identity_index = next(
            index
            for index, source in enumerate(manifest.sources)
            if source.name == manifest.identity_source
        )
        identity_store = opened[identity_index]
        if not isinstance(identity_store, GovernedStore):
            raise TypeError("identity source must be governed")
        return FederatedMemory(sources, identity_store.profiles), opened
    except Exception:
        await asyncio.gather(*(store.close() for store in opened), return_exceptions=True)
        raise


async def close_federation(stores: list[MemoryStore]) -> None:
    await asyncio.gather(*(store.close() for store in stores), return_exceptions=True)


def default_manifest_path() -> Path:
    return Path(os.environ.get("ORKMIND_FEDERATION_MANIFEST", "~/.orkmind/federation.json"))
