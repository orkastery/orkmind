# Claude Code + OrkMind Setup

## Step 1: Install OrkMind

```bash
pip install -e /path/to/OrkMind
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

## Step 4: Add MCP Server to Claude Code

In your Claude Code settings:

```json
{
  "mcpServers": {
    "orkmind": {
      "command": "python",
      "args": ["-m", "orkmind.mcp"],
      "env": {
        "ORKMIND_DATABASE_URL": "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind"
      }
    }
  }
}
```

## Step 5: Install the Skill

```bash
mkdir -p ~/.claude/skills/orkmind
cp skills/claude-code/SKILL.md ~/.claude/skills/orkmind/SKILL.md
```

## Step 6: Verify

Open Claude Code and run:

```
Use orkmind_stats to check the memory store
```

You should see the seeded entries across 16 collections.
