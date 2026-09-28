"""Company Brain v1 / D4-D7. Deterministic contracts, no storage or authority grant."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

Identifier = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Time = Annotated[str, Field(pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$")]
Version = Annotated[int, Field(ge=1, le=9007199254740991)]
Ids = Annotated[list[Identifier], Field(max_length=1000)]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(Closed):
    authority: Literal["ork"]
    instance: Identifier
    source_ref: Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[a-zA-Z0-9._/#:-]+$")]
    source_hash: Digest
    source_version: Version
    location: Annotated[str, Field(min_length=1, max_length=256)]


class Alias(Closed):
    system: Identifier
    instance: Identifier
    id: Identifier


class Owner(Closed):
    raw: Annotated[str, Field(max_length=160)] | None
    state: Literal["legacy-label", "unknown"]
    principal: None


class Entity(Closed):
    schema_: Literal["orkmind.company-brain-entity/v1"] = Field(alias="schema")
    tenant_id: Identifier
    id: Identifier
    kind: Literal["prod", "proj", "init"]
    version: Version
    parent_id: Identifier | None
    workspace_ids: Ids
    depends_on: Ids
    aliases: Annotated[list[Alias], Field(max_length=1000)]
    title: Annotated[str, Field(min_length=1, max_length=160)]
    description: Annotated[str, Field(max_length=20000)]
    status: Literal["idea", "planned", "ready", "in_progress", "validating", "delivered", "blocked", "cancelled"]
    acceptance_criteria: Annotated[list[Annotated[str, Field(max_length=2000)]], Field(max_length=100)]
    owner: Owner
    source: Source
    acl_ref: Identifier
    observed_at: Time | None
    recorded_at: Time | None


class Assertion(Closed):
    schema_: Literal["orkmind.company-brain-assertion/v1"] = Field(alias="schema")
    tenant_id: Identifier
    id: Identifier
    version: Version
    subject: Identifier
    predicate: Identifier
    object_id: Identifier | None
    value: str | int | bool | None
    source: Source
    producer_id: Identifier
    actor_id: Identifier | None
    method: Literal["deterministic"]
    observed_at: Time
    recorded_at: Time | None
    occurred_at: Time | None
    valid_from: Time | None
    valid_to: Time | None
    precision: Literal["instant", "unknown"]
    state: Literal["candidate", "accepted", "contested", "superseded", "retracted", "archived"]
    acl_ref: Identifier


class Cycle(Closed):
    thread_id: Identifier
    objective_id: Identifier | None
    objective_hash: Digest | None
    project_id: Identifier | None
    initiative_ids: Ids
    phase: Literal["GOAL", "PLAN", "GO", "CHECK", "SHIP", "MASTER"] | None
    session_id: Identifier | None
    context_hash: Digest | None


class Event(Closed):
    schema_: Literal["orkmind.company-brain-event/v1"] = Field(alias="schema")
    tenant_id: Identifier
    id: Identifier
    producer_id: Identifier
    aggregate_id: Identifier
    sequence: Version
    source_event_id: Identifier
    source: Source
    operation: Literal["upsert", "assert", "tombstone"]
    payload: Entity | Assertion | None
    payload_hash: Digest
    cycle: Cycle | None
    evidence_refs: Annotated[list[Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[a-zA-Z0-9._/#:-]+$")]], Field(max_length=1000)]
    occurred_at: Time | None
    observed_at: Time | None
    acl_ref: Identifier


class Receipt(Closed):
    schema_: Literal["orkmind.company-brain-receipt/v1"] = Field(alias="schema")
    tenant_id: Identifier
    id: Identifier
    event_id: Identifier
    stage: Literal["observed", "received", "validated", "materialized", "indexed", "retrievable"]
    result: Literal["ok", "conflict", "forbidden", "unavailable", "withheld"]
    attempt: Version
    materialized_version: Version | None
    recorded_at: Time
    previous_receipt_id: Identifier | None
    error: Identifier | None


class Facets(Closed):
    ids: Ids
    kinds: list[Literal["prod", "proj", "init", "assertion"]]
    workspace_ids: Ids
    source_instances: Ids


class Selection(Closed):
    schema_: Literal["orkmind.company-brain-selection/v1"] = Field(alias="schema")
    tenant_id: Identifier
    facets: Facets
    mode: Literal["selection", "context"]
    limit: Annotated[int, Field(ge=1, le=1000)]
    offset: Annotated[int, Field(ge=0, le=100000)]


class MigrationOperation(Closed):
    id: Identifier
    operation: Literal["insert", "update", "unchanged", "conflict"]
    expected_version: Version | None
    before_hash: Digest | None
    after: Entity
    gaps: Ids


class Migration(Closed):
    schema_: Literal["orkmind.company-brain-migration/v1"] = Field(alias="schema")
    tenant_id: Identifier
    batch_id: Identifier
    source_hash: Digest
    operations: Annotated[list[MigrationOperation], Field(max_length=1000)]
    observed_at: Time


Contract = Union[Entity, Assertion, Event, Receipt, Selection, Migration]
ADAPTER = TypeAdapter(Contract)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate(value: object) -> dict:
    try:
        parsed = ADAPTER.validate_python(value).model_dump(mode="json", by_alias=True)
        semantic(parsed)
        return parsed
    except Exception:
        # No untrusted content (including possible secrets) in diagnostics.
        raise ValueError("brain.contract.invalid") from None


def semantic(value: dict) -> None:
    for key, item in value.items():
        if key.endswith("_at") or key in ("valid_from", "valid_to"):
            if item is not None:
                datetime.fromisoformat(item.replace("Z", "+00:00"))
    if value["schema"].endswith("entity/v1"):
        kind, parent = value["kind"], value["parent_id"]
        if not value["id"].startswith(kind + "-"):
            raise ValueError()
        if kind == "prod" and parent is not None or kind != "prod" and (parent is None or not parent.startswith({"proj": "prod-", "init": "proj-"}[kind])):
            raise ValueError()
        if kind != "init" and value["depends_on"] or kind != "proj" and value["workspace_ids"]:
            raise ValueError()
        if any(not d.startswith("init-") or d == value["id"] for d in value["depends_on"]):
            raise ValueError()
        if (value["owner"]["raw"] is None) != (value["owner"]["state"] == "unknown"):
            raise ValueError()
    if value["schema"].endswith("event/v1"):
        payload = value["payload"]
        if digest(payload) != value["payload_hash"]:
            raise ValueError()
        if (value["operation"] == "tombstone") != (payload is None):
            raise ValueError()
        if payload is not None:
            validate(payload)
            if payload["tenant_id"] != value["tenant_id"] or payload["id"] != value["aggregate_id"] or payload["acl_ref"] != value["acl_ref"]:
                raise ValueError()
            if payload["source"] != value["source"]:
                raise ValueError()
            if value["operation"] != ("upsert" if payload["schema"].endswith("entity/v1") else "assert"):
                raise ValueError()
    if value["schema"].endswith("assertion/v1"):
        if value["object_id"] is not None and value["value"] is not None:
            raise ValueError()
        if value["valid_from"] and value["valid_to"] and value["valid_from"] >= value["valid_to"]:
            raise ValueError()
    if value["schema"].endswith("migration/v1"):
        for operation in value["operations"]:
            validate(operation["after"])
            if operation["id"] != operation["after"]["id"] or operation["after"]["tenant_id"] != value["tenant_id"]:
                raise ValueError()


def validate_catalog(entities: list[dict], scope: dict | None = None) -> list[dict]:
    values = [validate(e) for e in entities]
    ids = {e["id"]: e for e in values}
    if len(ids) != len(values) or len({e["tenant_id"] for e in values}) > 1:
        raise ValueError("brain.catalog.conflict")
    for entity in values:
        parent = entity["parent_id"]
        if parent is not None and parent not in ids:
            raise ValueError("brain.catalog.parent-missing")
        for dependency in entity["depends_on"]:
            if dependency not in ids or ids[dependency]["parent_id"] != parent:
                raise ValueError("brain.catalog.dependency-invalid")
    visiting, done = set(), set()
    def visit(id: str) -> None:
        if id in visiting:
            raise ValueError("brain.catalog.cycle")
        if id in done:
            return
        visiting.add(id)
        for dependency in ids[id]["depends_on"]:
            visit(dependency)
        visiting.remove(id)
        done.add(id)
    for id in ids:
        visit(id)
    if scope is not None:
        project = ids.get(scope.get("project_id"))
        selected = scope.get("initiative_ids")
        delivery = scope.get("delivery")
        if not project or project["kind"] != "proj" or not isinstance(selected, list) or delivery not in ("project", "initiatives"):
            raise ValueError("brain.scope.invalid")
        if (delivery == "project" and selected) or (delivery == "initiatives" and not selected):
            raise ValueError("brain.scope.invalid")
        if any(i not in ids or ids[i]["parent_id"] != project["id"] for i in selected):
            raise ValueError("brain.scope.invalid")
    return values
