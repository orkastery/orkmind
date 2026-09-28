# Hermes + OrkMind Setup

## Step 1: Install OrkMind with Hermes Support

```bash
pip install -e "/path/to/OrkMind[hermes]"
```

## Step 2: Start PostgreSQL + pgvector

```bash
bash scripts/setup_postgres.sh
export ORKMIND_DATABASE_URL="postgresql://orkmind:senha-de-teste@localhost:5432/orkmind"
```

## Step 3: Seed Example Data (Optional)

```bash
python scripts/seed_example.py
```

## Step 4: Use in Hermes

```python
from orkmind.hermes.provider import OrkMindMemoryProvider

provider = OrkMindMemoryProvider()

# At session start, recall mandatory rules
rules = await provider.get_rules(tags={"skill": ["deploy"]})
for rule in rules:
    print(f"[{rule['priority']}] {rule['content']}")

# During session, store learnings
await provider.store_memory(
    content="Deploy to staging requires VPN connection",
    collection="learning",
    tags={"skill": ["deploy"], "domain": ["infra"]},
    source="agent",
)
```
