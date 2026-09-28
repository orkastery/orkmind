"""Seed the OrkMind store with example memories across all 16 collections."""

import asyncio

from orkmind.core.config import load_config
from orkmind.core.models import MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.store.factory import create_store


def _e(content: str, collection: str, tags: dict, **kw) -> MemoryEntry:  # type: ignore[type-arg]
    return MemoryEntry(content=content, collection=collection, tags=tags, **kw)  # type: ignore[arg-type]


SEED_DATA = [
    _e("Never use rm -rf in production", "rule",
       {"skill": ["deploy"], "domain": ["infra"]}, priority="critical", mandatory=True),
    _e("Always run tests before merging to main", "rule",
       {"skill": ["git", "testing"]}, priority="critical", mandatory=True),
    _e("Do not commit .env files", "rule",
       {"skill": ["git"], "domain": ["security"]}, priority="high", mandatory=True),
    _e("Use the CI pipeline for all deployments", "instruction",
       {"skill": ["deploy", "ci-cd"]}, priority="high"),
    _e("Run database migrations with alembic upgrade head", "instruction",
       {"skill": ["database"], "domain": ["database"]}, priority="medium"),
    _e("Production database is PostgreSQL 16", "fact",
       {"domain": ["database", "infra"]}, priority="medium"),
    _e("API runs on port 8080 in production", "fact",
       {"domain": ["backend"]}, priority="medium"),
    _e("Auth tests failed with mocks; use real DB", "learning",
       {"skill": ["testing", "auth"], "domain": ["security"]}, priority="high"),
    _e("Prefer atomic commits and small PRs", "preference",
       {"skill": ["git"]}, priority="medium"),
    _e("Chose React for frontend due to team familiarity", "decision",
       {"domain": ["frontend"]}, priority="medium"),
    _e("API v2 spec available at docs/api-v2.md", "content",
       {"domain": ["backend"]}, priority="low"),
    _e("Weekly standup every Monday at 2pm", "agenda",
       {"project": ["team"]}, priority="medium"),
    _e("Alice - DevOps team lead", "contacts",
       {"domain": ["infra"]}, priority="medium"),
    _e("Auth module handoff from agent-alpha to agent-beta", "handoff",
       {"skill": ["auth"]}, priority="medium",
       metadata={"origin": "agent-alpha", "destination": "agent-beta"}),
    _e("MVP phase 2: DAG engine + multi-target", "roadmap",
       {"project": ["orkmind"]}, priority="medium"),
    _e("Core models at src/orkmind/core/models.py", "files",
       {"project": ["orkmind"]}, priority="low",
       metadata={"path": "src/orkmind/core/models.py"}),
    _e("Ontology reference at docs/ontologia.md", "docs",
       {"project": ["orkmind"]}, priority="low"),
    _e("Deploy DAG: lint -> test -> build -> deploy", "dags",
       {"skill": ["deploy"]}, priority="low"),
    _e("CLI: orkmind add --collection rule --content", "tools",
       {"project": ["orkmind"]}, priority="low"),
    _e("tomas - admin of orkmind workspace", "users",
       {"project": ["orkmind"]}, priority="medium"),
]


async def main() -> None:
    config = load_config()
    store = create_store(config)
    await store.initialize()
    layer = SemanticLayer(store)

    for entry in SEED_DATA:
        entry_id, warnings = await layer.add_memory(entry)
        status = " (warnings: " + ", ".join(warnings) + ")" if warnings else ""
        print(f"  [{entry.collection}] {entry_id[:8]}..{status}: {entry.content[:60]}")

    total = await store.count()
    colls = await store.list_collections()
    print(f"\nSeeded {total} entries across {len(colls)} collections.")
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
