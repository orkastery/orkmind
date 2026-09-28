# Hermes MemoryProvider Setup

## Prerequisites

- Python 3.11+
- PostgreSQL with pgvector extension
- Hermes Agent installed
- OrkMind installed (`pip install -e ".[hermes]"`)

## 1. Install OrkMind with Hermes Support

```bash
pip install -e ".[hermes]"
```

## 2. Configure Database

```bash
export ORKMIND_DATABASE_URL="postgresql://orkmind:senha-de-teste@localhost:5432/orkmind"
```

## 3. Configure Hermes

In your Hermes configuration, set OrkMind as the memory provider:

```yaml
memory_provider: orkmind
memory_provider_config:
  database_url: postgresql://orkmind:senha-de-teste@localhost:5432/orkmind
  token_budget: 4000
```

## 4. Programmatic Usage

```python
from orkmind.hermes.provider import OrkMindMemoryProvider

provider = OrkMindMemoryProvider()

# Recall memories for context
memories = await provider.recall(
    context="current conversation text",
    tags={"skill": ["deploy"]},
    files=["infra/main.tf"],
    token_budget=4000,
)

# Store a learning
await provider.store_memory(
    content="Tests pass faster with connection pooling",
    collection="learning",
    tags={"skill": ["testing"], "domain": ["database"]},
    source="agent",
)

# Get mandatory rules
rules = await provider.get_rules(
    tags={"skill": ["deploy"], "domain": ["infra"]},
)
```

## How It Works

1. Hermes calls `provider.recall()` at the start of each session
2. OrkMind runs context detectors on the conversation and files
3. Mandatory rules matching the detected context are always included
4. Additional relevant memories fill the remaining token budget
5. Results are sorted by priority (critical > high > medium > low)

This offloads memory from Hermes' constrained MEMORY.md (2,200 chars) into
OrkMind's structured store with 23 typed collections and semantic tags.
