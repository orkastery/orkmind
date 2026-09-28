"""Ontologia de portfólio: produto -> projeto -> iniciativa.

As entidades são memórias governadas, mas não reutilizam a dimensão legada
`project`, que continua representando isolamento físico/federação.
"""

from __future__ import annotations

import re
import uuid
from typing import Literal, Optional, cast

from pydantic import BaseModel, Field, field_validator

from orkmind.core.models import MemoryEntry
from orkmind.core.ontology import validate_entry
from orkmind.store.base import MemoryStore

PortfolioStatus = Literal[
    "idea", "planned", "ready", "in_progress", "validating",
    "delivered", "blocked", "cancelled",
]
EntityKind = Literal["product", "project", "initiative"]
_ENTITY_ID = re.compile(r"^(prod|proj|init)-[a-z0-9][a-z0-9-]{2,47}$")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class PortfolioEntity(BaseModel):
    id: str
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=20_000)
    status: PortfolioStatus = "idea"
    owner_id: Optional[str] = None
    priority: Literal["critical", "high", "medium", "low"] = "medium"
    acceptance_criteria: list[str] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ENTITY_ID.fullmatch(value):
            raise ValueError("id deve usar prod-, proj- ou init- e um slug estável")
        return value


class Product(PortfolioEntity):
    id: str = Field(default_factory=lambda: _id("prod"))
    kind: Literal["product"] = "product"


class Project(PortfolioEntity):
    id: str = Field(default_factory=lambda: _id("proj"))
    kind: Literal["project"] = "project"
    product_id: str
    workspace_ids: list[str] = Field(default_factory=list)

    @field_validator("product_id")
    @classmethod
    def product_prefix(cls, value: str) -> str:
        if not value.startswith("prod-"):
            raise ValueError("product_id deve referenciar um produto")
        return value


class Initiative(PortfolioEntity):
    id: str = Field(default_factory=lambda: _id("init"))
    kind: Literal["initiative"] = "initiative"
    project_id: str
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("project_id")
    @classmethod
    def project_prefix(cls, value: str) -> str:
        if not value.startswith("proj-"):
            raise ValueError("project_id deve referenciar um projeto")
        return value

    @field_validator("depends_on")
    @classmethod
    def dependency_prefixes(cls, values: list[str]) -> list[str]:
        if any(not value.startswith("init-") for value in values):
            raise ValueError("depends_on contém id inválido")
        return list(dict.fromkeys(values))


class CycleScope(BaseModel):
    """Escopo atômico de um ciclo do Ork."""

    project_id: str
    delivery: Literal["project", "initiatives"]
    initiative_ids: list[str] = Field(default_factory=list)

    @field_validator("initiative_ids")
    @classmethod
    def initiative_prefixes(cls, values: list[str]) -> list[str]:
        if any(not value.startswith("init-") for value in values):
            raise ValueError("initiative_ids contém id inválido")
        return list(dict.fromkeys(values))

    def model_post_init(self, __context: object) -> None:
        if self.delivery == "project" and self.initiative_ids:
            raise ValueError("ciclo de projeto não aceita initiative_ids")
        if self.delivery == "initiatives" and not self.initiative_ids:
            raise ValueError("ciclo de iniciativas exige ao menos uma iniciativa")


class PortfolioCatalog:
    """Catálogo sobre qualquer MemoryStore, com integridade referencial."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    @staticmethod
    def _entry(entity: Product | Project | Initiative) -> MemoryEntry:
        data = entity.model_dump(mode="json")
        tags: dict[str, list[str]] = {
            "prod": [entity.id if isinstance(entity, Product) else data.get("product_id", "")],
        }
        parent_id: str | None = None
        if isinstance(entity, Project):
            tags["proj"] = [entity.id]
            parent_id = entity.product_id
        elif isinstance(entity, Initiative):
            tags.pop("prod", None)
            tags["proj"] = [entity.project_id]
            tags["init"] = [entity.id]
            parent_id = entity.project_id
        tags = {key: values for key, values in tags.items() if all(values)}
        entry = MemoryEntry(
            id=entity.id,
            content=entity.description or entity.title,
            collection=entity.kind,
            tags=tags,
            priority=entity.priority,
            scope="project",
            source="human",
            author_id=entity.owner_id,
            parent_id=parent_id,
            metadata={"entity_schema": "orkmind.portfolio/v1", **data},
        )
        validate_entry(entry)
        return entry

    async def add_product(self, product: Product) -> str:
        return cast(str, await self.store.store(self._entry(product)))

    async def add_project(self, project: Project) -> str:
        parent = await self.store.retrieve(project.product_id)
        if parent is None or parent.collection != "product":
            raise ValueError(f"produto {project.product_id} não encontrado")
        return cast(str, await self.store.store(self._entry(project)))

    async def add_initiative(self, initiative: Initiative) -> str:
        parent = await self.store.retrieve(initiative.project_id)
        if parent is None or parent.collection != "project":
            raise ValueError(f"projeto {initiative.project_id} não encontrado")
        for dependency in initiative.depends_on:
            found = await self.store.retrieve(dependency)
            if found is None or found.collection != "initiative":
                raise ValueError(f"dependência {dependency} não encontrada")
            if found.metadata.get("project_id") != initiative.project_id:
                raise ValueError("dependências devem pertencer ao mesmo projeto")
        return cast(str, await self.store.store(self._entry(initiative)))

    async def list(self, kind: EntityKind, parent_id: str | None = None) -> list[MemoryEntry]:
        tags: dict[str, list[str]] = {}
        if kind == "project" and parent_id:
            tags["prod"] = [parent_id]
        if kind == "initiative" and parent_id:
            tags["proj"] = [parent_id]
        return cast(
            list[MemoryEntry],
            await self.store.search_by_tags(tags, collection=kind, limit=1000),
        )
