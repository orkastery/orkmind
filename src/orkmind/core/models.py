"""Core data models for OrkMind."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Priority = Literal["critical", "high", "medium", "low"]
Scope = Literal["global", "project", "session"]
Source = Literal["human", "agent", "system", "bootstrap"]

# F1: visibilidade de uma entry para o controle de acesso de leitura.
# public: qualquer requester. private: apenas o autor.
# restricted: autor + quem estiver na dimensao de tag `audience`.
Visibility = Literal["public", "private", "restricted"]

VALID_COLLECTIONS = (
    "rule",
    "instruction",
    "fact",
    "learning",
    "preference",
    "decision",
    "content",
    "agenda",
    "contacts",
    "handoff",
    "roadmap",
    "files",
    "docs",
    "dags",
    "tools",
    "users",
    # F1: ontologia expandida
    "session",
    "artifact",
    "compliance",
    "semantic_log",
    # Portfólio de produto (I-18)
    "product",
    "project",
    "initiative",
)

Collection = Literal[
    "rule",
    "instruction",
    "fact",
    "learning",
    "preference",
    "decision",
    "content",
    "agenda",
    "contacts",
    "handoff",
    "roadmap",
    "files",
    "docs",
    "dags",
    "tools",
    "users",
    # F1: ontologia expandida
    "session",
    "artifact",
    "compliance",
    "semantic_log",
    "product",
    "project",
    "initiative",
]


def _make_id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryEntry(BaseModel):
    """A single memory entry in OrkMind."""

    id: str = Field(default_factory=_make_id)
    content: str
    collection: Collection
    tags: dict[str, list[str]] = Field(default_factory=dict)
    priority: Priority = "medium"
    mandatory: bool = False
    scope: Scope = "global"
    source: Source = "human"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    expires_at: Optional[datetime] = None
    version: int = 1
    embedding: Optional[list[float]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    protected: bool = False
    content_hash: Optional[str] = None
    injection_risk: bool = False
    conflict: bool = False
    essence: Optional[str] = None
    structure: Optional[str] = None
    layer_generated_at: Optional[datetime] = None
    encrypted: bool = False
    encryption_meta: Optional[dict[str, Any]] = None
    # F1: ontologia expandida
    url: Optional[str] = None
    visibility: Visibility = "private"
    author_id: Optional[str] = None
    parent_id: Optional[str] = None


# Dimensoes de tag suportadas pelo ContextTags (espelham
# orkmind.core.ontology.VALID_TAG_DIMENSIONS).
TAG_DIMENSIONS = (
    "skill",
    "agent",
    "domain",
    "project",
    "situation",
    "person",
    "audience",
    "editors",
    # Identidades de negócio. `project` acima permanece legado e representa
    # isolamento/federação; prod/proj/init nunca ampliam ACL por si só.
    "prod",
    "proj",
    "init",
)


# F2: tipos de perfil suportados pela tabela profiles.
ProfileType = Literal["person", "agent", "system", "company", "group"]


class Profile(BaseModel):
    """Perfil de uma identidade que interage com o OrkMind (F2).

    Identifica pessoas, agentes, sistemas, empresas e grupos. Serve de
    base para o controle de acesso: `id` e comparado com author_id da
    entry e com as tags audience/editors.
    """

    id: str
    display_name: str
    profile_type: ProfileType
    parent_id: Optional[str] = None
    permissions: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


class ContextTags(BaseModel):
    """Tags inferred by context detectors."""

    skill: list[str] = Field(default_factory=list)
    agent: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)
    project: list[str] = Field(default_factory=list)
    situation: list[str] = Field(default_factory=list)
    # F1: ontologia expandida
    person: list[str] = Field(default_factory=list)
    audience: list[str] = Field(default_factory=list)
    editors: list[str] = Field(default_factory=list)
    prod: list[str] = Field(default_factory=list)
    proj: list[str] = Field(default_factory=list)
    init: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for dim in TAG_DIMENSIONS:
            values = getattr(self, dim)
            if values:
                result[dim] = values
        return result

    def merge(self, other: ContextTags) -> ContextTags:
        merged: dict[str, list[str]] = {}
        for dim in TAG_DIMENSIONS:
            merged[dim] = list(set(getattr(self, dim) + getattr(other, dim)))
        return ContextTags(**merged)
