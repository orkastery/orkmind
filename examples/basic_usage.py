"""Basic usage example for OrkMind."""

import asyncio

from orkmind.core.config import load_config
from orkmind.core.detectors import detect_context
from orkmind.core.models import MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.store.factory import create_store


async def main() -> None:
    # Load config and create store
    config = load_config()
    store = create_store(config)
    await store.initialize()
    layer = SemanticLayer(store, token_budget=config.token_budget)

    # Add some memories
    rule_id, _ = await layer.add_memory(MemoryEntry(
        content="Never use rm -rf in production environments",
        collection="rule",
        tags={"skill": ["deploy"], "domain": ["infra"]},
        priority="critical",
        mandatory=True,
    ))
    print(f"Added rule: {rule_id}")

    fact_id, _ = await layer.add_memory(MemoryEntry(
        content="Production database runs PostgreSQL 16 on port 5432",
        collection="fact",
        tags={"domain": ["database", "infra"]},
        priority="medium",
    ))
    print(f"Added fact: {fact_id}")

    # Detect context
    detected = detect_context(
        conversation="Let's deploy the terraform changes",
        files=["infra/main.tf"],
    )
    print(f"\nDetected context: {detected.to_dict()}")

    # Query for context (combines detection + mandatory rules + budget)
    results = await layer.query_for_context(
        conversation="Let's deploy the terraform changes",
        files=["infra/main.tf"],
    )
    print(f"\nContext results ({len(results)} entries):")
    for entry in results:
        flag = " [MANDATORY]" if entry.mandatory else ""
        print(f"  [{entry.collection}] {entry.priority}{flag}: {entry.content}")

    # Get mandatory rules only
    rules = await layer.get_mandatory_rules(tags={"skill": ["deploy"]})
    print(f"\nMandatory rules: {len(rules)}")
    for rule in rules:
        print(f"  {rule.content}")

    # Stats
    total = await store.count()
    collections = await store.list_collections()
    print(f"\nStats: {total} entries in {len(collections)} collections")

    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
