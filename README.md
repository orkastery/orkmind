# OrkMind

**Semantic memory layer for AI agents.** OrkMind offloads memory from constrained agent
instruction files (MEMORY.md, AGENTS.md, CLAUDE.md) into a structured, typed store with
deterministic tag-based retrieval and mandatory rule injection.

**Status: v0.3.0 - Fase 2.5 (Contexto por camadas + recuperacao + autonomia controlada) entregue** -
220 testes, camadas E1/E2/E3 com progressive loading, snapshots globais, encryption at-rest
seletivo (AES-256-GCM), extracao automatica com guardrail de 3 camadas, busca semantica com
rerank RRF. Inclui toda a Fase 2 (governanca, protecao, anti-injection, conflitos, DAG).
Ver [CHANGELOG](CHANGELOG.md).

## Architecture

```mermaid
flowchart TD
    subgraph Runtimes
        CC[Claude Code]
        HM[Hermes Agent]
        CL[orkmind CLI]
    end

    CC -->|MCP stdio| MCP[MCP Server<br/>7 tools]
    HM -->|MemoryProvider| HP[Hermes Provider]
    CL --> SL

    MCP --> SL[SemanticLayer<br/>23 collections &#x2022; 11 tag dimensions]
    HP --> SL

    SL -->|exact tag search &#43; semantic| MS[MemoryStore ABC]
    MS --> PG[(PostgreSQL &#43; pgvector)]

    SL --> DET[Context Detectors<br/>Keyword &#43; File path]
    DET -->|inferred tags| SL

    style PG fill:#336791,stroke:#fff,color:#fff
    style SL fill:#2563eb,stroke:#fff,color:#fff
    style MCP fill:#10b981,stroke:#fff,color:#fff
```

## Key Concepts

- **23 typed collections**: rule, instruction, fact, learning, preference, decision,
  content, agenda, contacts, handoff, roadmap, files, docs, dags, tools, users,
  artifact, compliance, semantic_log, session, product, project, initiative
- **11 semantic tag dimensions**: skill, agent, domain, project, situation, person, audience,
  editors, prod, proj, init. The legacy `project` dimension remains the physical tenant/workspace;
  `prod`, `proj` and `init` model the business portfolio without widening access.
- **Deterministic tag search**: exact match on tags, not probabilistic embedding search
- **Mandatory injection**: entries with `mandatory: true` are ALWAYS returned when their
  tags match the query context
- **Token budget**: the semantic layer respects a configurable token budget, prioritizing
  critical and mandatory entries
- **Rule protection (D2)**: entries `protected`/`priority: critical` reject agent
  edits/deletes - only human-authenticated sources can modify
- **Append-only versioning (D3)**: full history via `memory_versions` table with
  LRU-based garbage collection
- **Anti-injection (D4)**: 5-category detection, suspicious entries retained but
  excluded from automatic injection; SHA-256 content integrity
- **Conflict detection (D5)**: mandatory entries with overlapping tags flagged and
  blocked from injection until human review
- **DAG engine (D1)**: in-memory directed graph with topological sort and cycle
  detection for rule dependency analysis
- **Context layers E1/E2/E3 (D6)**: progressive loading by fidelity - Essence
  (~100 tokens), Structure (~2k tokens), Source (full). Mandatory entries always
  loaded at E3
- **Global snapshots (D7)**: commit/log/show/diff/restore of the entire memory
  tree with embeddings preserved
- **Encryption at-rest (D8)**: optional, selective AES-256-GCM envelope encryption
  for sensitive collections (contacts, files, docs)
- **Session extraction (D9)**: automatic memory extraction into soft collections
  with 3-layer guardrail (prompt + post-parse + ontology validation)
- **Semantic search + RRF rerank (D10)**: optional intent-based search combining
  FTS + vector with Reciprocal Rank Fusion, keeping deterministic `find()` intact

## Quick Start

### Install

```bash
pip install -e .
```

### Configure

Set the database URL:

```bash
export ORKMIND_DATABASE_URL="postgresql://orkmind:senha-de-teste@localhost:5432/orkmind"
```

Or create `~/.orkmind/config.toml`:

```toml
[store]
backend = "pgvector"                                          # default
database_url = "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind"

[server]
log_level = "INFO"
token_budget = 4000
```

### Storage backends

OrkMind separates **persisting** from **governing**. The backend stores and
returns bytes; governance (entry protection, ACL, versioning, constitutional
ordering) runs in a layer above, identical for every backend. Switching
backends changes where data lives, never what the product guarantees.

| Backend | When to use | Trade-off |
|---------|-------------|-----------|
| `pgvector` (default) | Production. Nothing to change on existing installs. | None. It is the reference. |
| `memory` | Tests, local development, CI without external services. | Volatile: data dies with the process. Never a default. |
| `qdrant` | You already run Qdrant and want a dedicated vector engine. | No unique index for `(collection, content_hash)`, so idempotency is best-effort and declared. |

```bash
export ORKMIND_STORE_BACKEND=qdrant
export ORKMIND_STORE_OPTIONS='{"url": "http://localhost:6333"}'
pip install "orkmind[qdrant]"

orkmind store info          # active backend, capabilities and active warnings
```

Every degradation is declared in `StoreCapabilities` and printed by
`orkmind store info` -- never silent. Full matrix in
[`docs/storage-backends/MATRIZ-BACKENDS.md`](docs/storage-backends/MATRIZ-BACKENDS.md),
configuration and migration guide in
[`docs/storage-backends/GUIA-BACKENDS.md`](docs/storage-backends/GUIA-BACKENDS.md).

### Set Up PostgreSQL

```bash
# Using the helper script (requires Docker):
bash scripts/setup_postgres.sh

# Or manually:
createdb orkmind
psql orkmind -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### CLI Usage

```bash
# Add a memory
orkmind add --collection rule --content "Never use rm -rf in production" \
  --tags '{"skill": ["deploy"], "domain": ["infra"]}' --mandatory

# List memories
orkmind list --collection rule

# Search by tags
orkmind search --tags '{"skill": ["deploy"]}'

# Detect context from conversation
orkmind detect --text "Let's deploy the terraform changes"

# Statistics
orkmind stats

# Add idempotently (returns the existing id instead of duplicating)
orkmind add --collection content --content "relatorio semanal" --dedupe --json
```

### Guaranteed Writes (spool + drainer)

Agent and cron content must never be lost, even when the execution tools
are missing from a job toolset or the backend is momentarily down. The
write path stages to a durable on-disk queue first, then reconciles:

```bash
# Serve the local idempotent write API (loopback only, token required)
export ORKMIND_API_TOKEN="<token>"
orkmind api --port 8077

# Drain the queue once (cron), or continuously
.venv/bin/python scripts/orkmind_drain.py --once
.venv/bin/python scripts/orkmind_drain.py --watch --interval 60

# Inspect the queue without writing anything
.venv/bin/python scripts/orkmind_drain.py --status
```

Idempotency is keyed on `content_hash` (SHA-256) and enforced by a
partial unique index on `memories(collection, content_hash)`, so
reprocessing the queue never duplicates memory. See
[docs/sempre-gravar.md](docs/sempre-gravar.md).

### MCP Server (Claude Code)

Add to your Claude Code MCP settings:

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

## Native Memory Provider

`orkmind.memory_provider` is a separate, **async** package (asyncpg) for any person, company
or agent stack that needs a full memory stack rather than the rule-injection layer above: three tiers
(Core / Recall / Wiki), a navigable meta-index, hybrid dense + lexical retrieval fused with
RRF in SQL, deterministic RBAC pre-filtering, and idempotent document ingestion. It runs
directly on PostgreSQL + pgvector and never compacts history: the per-turn context is bounded
by construction, and the database itself rejects `UPDATE`/`DELETE` on the history tables.

```bash
pip install 'orkmind[memory-provider]'
export ORKMIND_PROVIDER_DATABASE_URL=postgresql://user:pass@localhost:5433/memory_provider
python examples/memory_provider.py
```

It ingests **whole documents** -- Markdown with wikilinks and frontmatter, plain text, CSV,
PDF and DOCX -- and keeps the web between them: `[[wikilinks]]`, backlinks with the citing
excerpt, links to notes that do not exist yet (which resolve themselves once the note is
written), and hierarchical tags where `rede` also matches `rede/backbone`. Point it at a
folder with `ingest_vault()` and it walks the whole thing.

It ships an **MCP server** (`python -m orkmind.memory_provider.mcp`), so any MCP-speaking
runtime -- Claude Code, OpenClaw, Codex -- gets `memory_search` / `memory_get` over this store.
Identity is resolved once from the environment, never from a tool argument, so the model
cannot pick its own role.

It shares the `EmbeddingProvider` contract with the rest of OrkMind but not the store: it has
its own schema and does not go through `MemoryStore`/`GovernedStore`.
See [docs/memory-provider/README.md](docs/memory-provider/README.md).

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check .

# Type check
mypy src/
```

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **PostgreSQL + pgvector** as the default backend | Single dependency with proven reliability. Supports exact tag search (GIN indexes), full-text search (tsvector), and vector similarity in one engine. `memory` and `qdrant` are also available; pgvector stays the default so existing installs need no change. |
| **Governance above the adapter, not inside it** | A backend persists; OrkMind governs. Protection, ACL, versioning and constitutional ordering live in one place (`GovernedStore`), so the same guarantees hold on every backend and a new adapter cannot quietly weaken them. |
| **Deterministic tag search over embeddings** | Agent memory retrieval must be predictable. Tag-based exact match ensures rules and mandatory entries are always found. Semantic (vector) search is additive, not primary. |
| **23 typed collections** | Each collection has distinct validation rules and lifecycle semantics (`rule` vs `learning` vs `handoff`). Strong typing prevents the "everything in one bucket" anti-pattern. |
| **8 tag dimensions** (skill, agent, domain, project, situation, person, audience, editors) | Captures the essential context axes for multi-agent systems. Tags are AND-matched within a dimension, enabling precise scoping. |
| **Mandatory injection** | Entries marked `mandatory: true` bypass ranking and are always included when tags match. Critical for safety rules and governance policies. |
| **Token budget** | SemanticLayer respects a configurable token limit, prioritizing critical/mandatory entries, so agents don't exceed their context window. |
| **MCP (stdio) + Hermes MemoryProvider** | Two integration paths cover the two dominant agent runtimes. MCP for Claude Code, MemoryProvider for Hermes -- both are thin adapters over SemanticLayer. |

## Documentation

- [Ontology Reference](docs/ontologia.md) -- 23 collections, 11 tag dimensions, validation
- [Integration Guide](docs/integration-guide.md) -- Claude Code + Hermes setup
- [MCP Setup](docs/mcp-setup.md) -- MCP server configuration for Claude Code
- [Hermes Setup](docs/hermes-setup.md) -- Hermes MemoryProvider configuration
- [Federated memory](docs/federation.md) -- per-project stores, shared recall and profile ACL
- [Gravacao garantida](docs/sempre-gravar.md) -- spool, drainer, idempotent API
- [Native Memory Provider](docs/memory-provider/README.md) -- Core/Recall/Wiki tiers, RRF hybrid search, RBAC, ingestion
- [Benchmark](bench/README.md) -- method, metric definitions, and what it does not prove
- [Changelog](CHANGELOG.md) -- Release history

<!-- BENCH:INICIO (gerado por bench/run.py; nao editar a mao) -->
## Benchmark

Numbers below are generated by `python bench/run.py` and read from
`bench/results/latest.json`. They are never typed by hand. Every
fraction is reported as `hits/total (rate)`, never as a bare
percentage. See `bench/README.md` for the method and for what this
benchmark does **not** prove.

Last run: `2026-09-01T01:41:27.622062+00:00` at commit `31e4e4f`.

| Metric | Hermes | OpenClaw (now) | OpenClaw (pre-port) |
| --- | --- | --- | --- |
| M1 constitutional injection | 20/20 (1.00) | 20/20 (1.00) | 0/20 (0.00) |
| M2 mandatory rules | 20/20 (1.00) | 20/20 (1.00) | 0/20 (0.00) |
| M3 fail-safe scenarios | 6/6 (1.00) | 6/6 (1.00) | 2/6 (0.33) |
| M4 pipeline sanity (fake embedder) | 12/12 (1.00) | 12/12 (1.00) | 0/12 (0.00) |
| M5 injected chars per turn | 2678 | 1557 | 0 |

M4 recall/precision are deliberately omitted from this table: with a
fake embedder they measure nothing about semantic quality. What is
validated here is the pipeline structure. Real-embedder numbers are
scheduled for the next cycle.

M7 (cross-harness fidelity): rules block identical, 0 differing chars, guardrail present in both.

### Support matrix per integration

| Integration | Unconditional rule injection | Fail-safe alert | Auto-recall | Covered by benchmark |
| --- | --- | --- | --- | --- |
| Hermes plugin | yes | yes | yes | yes (M1-M7) |
| OpenClaw plugin | yes | yes | yes | yes (M1-M7) |
| MCP server | n/a (no prompt build) | n/a | on demand via `orkmind_get_rules` / recall tools | no |
| CLI | n/a (no prompt build) | n/a | manual (`orkmind search`) | no |

MCP and CLI never build a model prompt, so unconditional injection
does not apply to them: they expose the same rules on demand. Only
the two prompt-building integrations can guarantee that governance
reaches the model on every turn, and only those are benchmarked.
<!-- BENCH:FIM -->

## License

[Apache 2.0](LICENSE)
