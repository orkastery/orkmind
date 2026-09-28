# MCP Server Setup for Claude Code

## Prerequisites

- Python 3.11+
- PostgreSQL with pgvector extension
- OrkMind installed (`pip install -e .`)

## 1. Install OrkMind

```bash
pip install -e .
```

## 2. Configure Database

```bash
export ORKMIND_DATABASE_URL="postgresql://orkmind:senha-de-teste@localhost:5432/orkmind"
```

Or create `~/.orkmind/config.toml`:

```toml
[store]
database_url = "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind"
```

## 3. Add MCP Server to Claude Code

Add to your Claude Code settings (`.claude/settings.json` or global settings):

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

## 4. Install the Skill (Optional)

Copy `skills/claude-code/SKILL.md` to your Claude Code skills directory:

```bash
cp skills/claude-code/SKILL.md ~/.claude/skills/orkmind/SKILL.md
```

This teaches Claude Code to query OrkMind before taking actions.

## 5. Verify

Start Claude Code and check that `orkmind_query` appears in the available tools:

```
> /mcp
```

## Available MCP Tools

- **orkmind_query**: Query memories with context detection
- **orkmind_get_rules**: Get mandatory rules for current context
- **orkmind_add**: Add a new memory
- **orkmind_update**: Update an existing memory
- **orkmind_delete**: Delete a memory
- **orkmind_list**: List memories by collection
- **orkmind_stats**: Get store statistics
