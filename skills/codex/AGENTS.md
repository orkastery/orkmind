# OrkMind Memory

This project uses OrkMind as its semantic memory layer. Before taking actions,
query OrkMind for mandatory rules via the MCP server:

- Use `orkmind_get_rules` to check for mandatory rules before acting
- Use `orkmind_query` for context-aware memory retrieval
- Use `orkmind_add` to store new learnings or decisions

OrkMind organizes memories into 16 typed collections with 5 semantic tag
dimensions (skill, agent, domain, project, situation). Mandatory rules
are always returned when their tags match the query context.
