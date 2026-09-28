# OrkMind Integration Guide

OrkMind integrates with AI agent runtimes as a semantic memory layer. The MVP supports
two integrators:

## 1. Claude Code (via MCP Server)

OrkMind runs as an MCP server over stdio, exposing 7 tools that Claude Code can call.

### Setup

See [MCP Setup](mcp-setup.md) for detailed instructions.

### Available Tools

| Tool | Description |
|------|-------------|
| `orkmind_query` | Query memories for current context (with detection) |
| `orkmind_get_rules` | Get all mandatory rules matching tags |
| `orkmind_add` | Add a new memory entry |
| `orkmind_update` | Update an existing entry |
| `orkmind_delete` | Delete an entry |
| `orkmind_list` | List entries by collection |
| `orkmind_stats` | Get store statistics |

### How Claude Code Uses OrkMind

1. Install the SKILL.md (see `skills/claude-code/SKILL.md`)
2. Before taking actions, Claude queries OrkMind for mandatory rules
3. Context detection infers relevant tags from conversation and files
4. Mandatory rules are always injected into the response context

## 2. Hermes Agent (via MemoryProvider)

OrkMind provides a MemoryProvider that Hermes can use as its memory backend.

### Setup

See [Hermes Setup](hermes-setup.md) for detailed instructions.

### Provider Interface

```python
from orkmind.hermes.provider import OrkMindMemoryProvider

provider = OrkMindMemoryProvider()

# Recall memories for current context
memories = await provider.recall(
    context="deploying the app",
    tags={"skill": ["deploy"]},
)

# Store a new memory
await provider.store_memory(
    content="Deploy requires approval from team lead",
    collection="rule",
    tags={"skill": ["deploy"]},
    mandatory=True,
)

# Get mandatory rules
rules = await provider.get_rules(tags={"skill": ["deploy"]})
```

## Architecture

Both integrators share the same core:

```
Claude Code -> MCP Server -> SemanticLayer -> MemoryStore -> PostgreSQL
Hermes      -> Provider   -> SemanticLayer -> MemoryStore -> PostgreSQL
```

The SemanticLayer handles:
- Ontology validation (23 collections, 11 tag dimensions)
- Context detection (keyword + file path detectors)
- Mandatory rule injection
- Token budget management
- Priority-based ordering
