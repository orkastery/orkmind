# OrkMind

**English** · [Português](README.pt-BR.md)

[![PyPI](https://img.shields.io/pypi/v/orkmind)](https://pypi.org/project/orkmind/) [![MIT license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

> **In one sentence:** OrkMind is typed, governed memory for AI agents: the rules that matter
> always reach the model, retrieval is deterministic, and history is never rewritten.

- **Status:** 0.4.0, alpha · on PyPI as [`orkmind`](https://pypi.org/project/orkmind/) ·
  [changes per version](CHANGELOG.md)
- **Proof:** CI on every pull request runs the test suite without a database on Python 3.11 and
  3.12, checks the sdist and the wheel, and tests the OpenClaw plugin.
- **Project:** open source under the MIT license, maintained by [Orkastery](https://github.com/orkastery),
  contributions by pull request ([CONTRIBUTING](CONTRIBUTING.md)).
- **Language:** this page is the canonical entry point, mirrored in [Portuguese](README.pt-BR.md);
  part of the docs and the code comments are in Brazilian Portuguese today.

```bash
pip install "orkmind>=0.3.0"
bash scripts/setup_postgres.sh        # PostgreSQL + pgvector in Docker (or bring your own)
export ORKMIND_DATABASE_URL="postgresql://orkmind:password@localhost:5432/orkmind"
orkmind add --collection rule --content "Never run rm -rf in production" \
  --tags '{"skill": ["deploy"]}' --mandatory
orkmind search --tags '{"skill": ["deploy"]}'
```

## Why it exists

- Agents keep their memory in instruction files (`AGENTS.md`, `CLAUDE.md`, `MEMORY.md`) that
  grow until the important rule gets lost in the middle.
- Embedding search is probabilistic: the safety rule you need on this turn may simply not rank.
- **OrkMind moves that memory into a typed store** with deterministic, tag-based retrieval, and
  rules marked `mandatory` are always returned when their tags match the context.

## How it works

```mermaid
flowchart TD
    subgraph Runtimes
        CC[Claude Code]
        HM[Hermes Agent]
        OC[OpenClaw]
        CL[orkmind CLI]
    end

    CC -->|MCP stdio| MCP[MCP server<br/>10 tools]
    HM -->|MemoryProvider plugin| SL
    OC -->|memory plugin| SL
    CL --> SL
    MCP --> SL[SemanticLayer<br/>23 collections &#x2022; 11 tag dimensions]

    SL -->|exact tag search &#43; optional semantic| GS[GovernedStore<br/>protection, ACL, versioning]
    GS --> MS[(PostgreSQL &#43; pgvector<br/>default backend)]
```

## Key concepts

- **23 typed collections:** rule, instruction, fact, learning, preference, decision, content,
  agenda, contacts, handoff, roadmap, files, docs, dags, tools, users, artifact, compliance,
  semantic_log, session, product, project, initiative.
- **11 tag dimensions:** skill, agent, domain, project, situation, person, audience, editors, prod,
  proj, init. `project` stays the physical workspace; `prod`, `proj` and `init` model the business
  portfolio without widening access.
- **Deterministic retrieval:** exact match on tags. Semantic search (FTS + vector, fused with RRF)
  is optional and additive, never a replacement.
- **Mandatory injection:** entries with `mandatory: true` are always returned when their tags match.
- **Token budget:** the semantic layer respects a configurable budget and loads critical and
  mandatory entries first.
- **Protection:** `protected` and `priority: critical` entries reject agent edits and deletes;
  only human-authenticated sources can change them.
- **Append-only history:** full version history in `memory_versions`.
- **Anti-injection:** suspicious content is kept but excluded from automatic injection, with
  SHA-256 content integrity.
- **Conflict detection:** mandatory entries with overlapping tags are held for human review.
- **Context layers:** progressive loading by fidelity (essence, structure, source).
- **Snapshots:** commit, log, diff and restore of the whole memory tree.
- **Encryption at rest:** optional AES-256-GCM for sensitive collections.

## Quick start

```bash
pip install "orkmind>=0.3.0"

# PostgreSQL + pgvector with the helper script (requires Docker)
bash scripts/setup_postgres.sh
# or on an existing server
createdb orkmind
psql orkmind -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Point OrkMind at the database with `ORKMIND_DATABASE_URL`, or with `~/.orkmind/config.toml`:

```toml
[store]
backend = "pgvector"                                     # default
database_url = "postgresql://orkmind:password@localhost:5432/orkmind"

[server]
log_level = "INFO"
token_budget = 4000
```

```bash
orkmind add --collection rule --content "Never run rm -rf in production" \
  --tags '{"skill": ["deploy"], "domain": ["infra"]}' --mandatory
orkmind list --collection rule
orkmind search --tags '{"skill": ["deploy"]}'
orkmind detect --text "Let's deploy the terraform changes"
orkmind stats
orkmind add --collection content --content "weekly report" --dedupe --json   # idempotent
```

## Integrations

| Integration | How it connects | Rules on every turn |
| --- | --- | --- |
| Hermes | MemoryProvider plugin ([setup](docs/hermes-setup.md)) | yes, unconditional injection |
| OpenClaw | memory plugin in `integrations/openclaw` | yes, unconditional injection |
| Claude Code, Codex and any MCP client | `python -m orkmind.mcp` ([setup](docs/mcp-setup.md)) | on demand, through the tools |
| CLI | `orkmind` | on demand |

The MCP server and the CLI never build a model prompt, so they expose the same rules on demand
instead of injecting them. Only the two prompt-building plugins guarantee that governance reaches
the model on every turn, and both are covered by the benchmark below.

MCP configuration for Claude Code:

```json
{
  "mcpServers": {
    "orkmind": {
      "command": "python",
      "args": ["-m", "orkmind.mcp"],
      "env": { "ORKMIND_DATABASE_URL": "postgresql://orkmind:password@localhost:5432/orkmind" }
    }
  }
}
```

## Storage backends

OrkMind separates **persisting** from **governing**. A backend stores and returns bytes;
protection, ACL, versioning and ordering run in one layer above it, identical for every backend.

| Backend | When to use | Trade-off |
| --- | --- | --- |
| `pgvector` (default) | Production | None. It is the reference |
| `memory` | Tests, local development, CI | Volatile: data dies with the process |
| `qdrant` | You already run Qdrant | Experimental; idempotency is best-effort and declared |

Every degradation is declared in `StoreCapabilities` and printed by `orkmind store info`, never
silent. See the [backend guide](docs/storage-backends/GUIA-BACKENDS.md) and the
[capability matrix](docs/storage-backends/MATRIZ-BACKENDS.md).

## Guaranteed writes

Content written by agents and cron jobs is staged in a durable on-disk queue first and then
reconciled, so it survives a missing tool or a database that is briefly down. Idempotency is
keyed on the SHA-256 `content_hash` and enforced by a unique index, so replaying the queue never
duplicates memory.

```bash
export ORKMIND_API_TOKEN="<token>"
orkmind api --port 8077                           # local idempotent write API, loopback only
python scripts/orkmind_drain.py --once            # drain the queue once (cron)
python scripts/orkmind_drain.py --status          # inspect it without writing
```

Details in [docs/sempre-gravar.md](docs/sempre-gravar.md).

## Native Memory Provider

`orkmind.memory_provider` is a separate async package for stacks that need a full memory rather
than the rule layer above: three tiers (Core, Recall, Wiki), hybrid dense and lexical retrieval
fused with RRF in SQL, deterministic RBAC pre-filtering, and idempotent ingestion of whole
documents (Markdown with wikilinks and frontmatter, text, CSV, PDF, DOCX). The database rejects
`UPDATE` and `DELETE` on the history tables, and an MCP server
(`python -m orkmind.memory_provider.mcp`) exposes `memory_search` and `memory_get`.

```bash
pip install "orkmind[memory-provider]>=0.3.0"
export ORKMIND_PROVIDER_DATABASE_URL=postgresql://user:password@localhost:5433/memory_provider
python examples/memory_provider.py
```

See [docs/memory-provider/README.md](docs/memory-provider/README.md).

## Development

```bash
pip install -e ".[dev]"
pytest -m "not integration"      # what CI runs, no database needed
pytest                           # the full suite; needs ORKMIND_TEST_DATABASE_URL with "test" in the name
```

The integration tests erase the database they point at, so the test guard only accepts a
database whose name marks it as a test database.

Setup, tests and what needs a database, style, pull requests and releases have one guide each,
indexed in [CONTRIBUTING](CONTRIBUTING.md). The guides are in Brazilian Portuguese today.

## Design decisions

| Decision | Rationale |
| --- | --- |
| **PostgreSQL + pgvector by default** | One dependency that does exact tag search (GIN), full-text search and vector similarity. |
| **Governance above the adapter** | Protection, ACL, versioning and ordering live in `GovernedStore`, so a new backend cannot quietly weaken them. |
| **Deterministic tags over embeddings** | Retrieval of rules must be predictable. Semantic search is additive, never primary. |
| **23 typed collections** | Each has its own validation and lifecycle (`rule` is not `learning` is not `handoff`). |
| **11 tag dimensions** | The context axes of multi-agent systems, AND-matched within a dimension for precise scoping. |
| **Mandatory injection and a token budget** | Safety rules bypass ranking, and the model's context window is respected. |

## Documentation

- [Ontology reference](docs/ontologia.md): collections, tag dimensions, validation
- [Integration guide](docs/integration-guide.md): Claude Code and Hermes
- [MCP setup](docs/mcp-setup.md) · [Hermes setup](docs/hermes-setup.md)
- [Federated memory](docs/federation.md): per-project stores, shared recall and profile ACL
- [Guaranteed writes](docs/sempre-gravar.md): spool, drainer, idempotent API
- [Native Memory Provider](docs/memory-provider/README.md)
- [Benchmark](bench/README.md): method, metrics, and what it does not prove
- [Changelog](CHANGELOG.md)

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

## Project

OrkMind is open source under the [MIT license](LICENSE), maintained by
[Orkastery](https://github.com/orkastery). Contributions come in by pull request, with the
evidence described in [CONTRIBUTING](CONTRIBUTING.md). Report vulnerabilities privately, as
described in [SECURITY](SECURITY.md). The [code of conduct](CODE_OF_CONDUCT.md) applies to every
space of the project.

## License

[MIT](LICENSE)
