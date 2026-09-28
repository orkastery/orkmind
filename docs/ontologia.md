# OrkMind Ontology Reference

## 23 Typed Collections

| # | Collection | Purpose | Example |
|---|-----------|---------|---------|
| 1 | `rule` | Mandatory rules that MUST be followed | "Never use rm -rf in production" |
| 2 | `instruction` | Procedural instructions (how to do something) | "Use CI pipeline for deploys" |
| 3 | `fact` | Verifiable facts about project/user | "Production DB is PostgreSQL 16" |
| 4 | `learning` | Lessons learned from experience | "Auth tests failed with mocks; use real DB" |
| 5 | `preference` | Style/approach preferences | "Prefer atomic commits and small PRs" |
| 6 | `decision` | Decisions and their context | "Chose React for frontend familiarity" |
| 7 | `content` | Reference content/articles/documents | "API v2 spec in content/reference" |
| 8 | `agenda` | Schedules/appointments/date alerts | "Weekly standup Monday 2pm" |
| 9 | `contacts` | People, roles, contexts | "Alice - DevOps team lead" |
| 10 | `handoff` | Context handoff between sessions/agents | "Module X handoff to agent Y" |
| 11 | `roadmap` | Plans/phases/milestones | "MVP phase 2: DAG + multi-target" |
| 12 | `files` | File/path references | "src/orkmind/core/models.py" |
| 13 | `docs` | Documentation references | "docs/ontologia.md defines the schema" |
| 14 | `dags` | DAG activation definitions (phase 2) | "deploy DAG: ci -> test -> deploy" |
| 15 | `tools` | Tool/API knowledge | "CLI `orkmind add` adds a memory" |
| 16 | `users` | Users/identities/roles | "tomas - admin of orkmind workspace" |
| 17 | `session` | Session state (requires `session_id`) | "session 42: refactor of the store layer" |
| 18 | `artifact` | Produced artifacts (requires `artifact_type`) | "release v0.3.0 notes" |
| 19 | `compliance` | Reviews and violations (requires `compliance_type`) | "compliance_violation: rule D5 breached" |
| 20 | `semantic_log` | Semantic log packages (requires `package_id`) | "package 2026-08-31: cycle F3 run" |
| 21 | `product` | Durable product identity and roadmap root | "Orkastery product" |
| 22 | `project` | Product demand that may span workspaces | "Maestro Workspace delivery" |
| 23 | `initiative` | Bounded delivery inside a project | "Portfolio ontology" |

## 11 Semantic Tag Dimensions

Tags are the central primitive of OrkMind's ontology. Each memory entry can have tags
across 11 dimensions. The access dimensions remain `audience` and `editors`:
`audience` says who may READ
the entry and `editors` says who may CHANGE it.

| Dimension | Key | Example Values |
|-----------|-----|---------------|
| Skill | `skill` | `["git", "deploy", "docker", "auth", "testing"]` |
| Agent | `agent` | `["claude-code", "hermes", "codex", "openclaw"]` |
| Domain | `domain` | `["backend", "frontend", "infra", "database", "security"]` |
| Project | `project` | `["orkmind", "my-app", "api-gateway"]` |
| Situation | `situation` | `["code-review", "debugging", "refactor", "deploy"]` |
| Person | `person` | `["tomas", "alice"]` -- who is mentioned or relevant |
| Audience | `audience` | `["owner", "team", "public"]` -- who may READ the entry |
| Editors | `editors` | `["owner", "hermes"]` -- who may CHANGE the entry |
| Product | `prod` | `["prod-orkastery"]` -- business product identity |
| Product project | `proj` | `["proj-maestro-workspace"]` -- demand identity |
| Initiative | `init` | `["init-portfolio-ontology"]` -- bounded delivery identity |

The legacy `project` tag still identifies the physical tenant or repository used by
federation and access control. The `prod`/`proj`/`init` hierarchy never grants access.

## Product Portfolio

The canonical hierarchy is `product -> project -> initiative`. A project belongs to one
product and may reference several workspace IDs. An initiative belongs to one project;
dependencies are allowed only between initiatives of that same project. Use
`orkmind portfolio create` and `orkmind portfolio list` to operate the typed catalog.

### Tag Search is EXACT (Deterministic)

Tag-based search uses exact containment matching, not probabilistic embedding search.
This guarantees that mandatory rules are always returned when their tags match the query.

## Priority Levels

| Priority | Weight | Description |
|----------|--------|-------------|
| `critical` | 0 (highest) | Must be injected first, never dropped |
| `high` | 1 | Important, dropped only under extreme budget pressure |
| `medium` | 2 | Default priority |
| `low` | 3 | Dropped first when budget is tight |

## Scopes

| Scope | Description |
|-------|-------------|
| `global` | Applies everywhere |
| `project` | Applies only within a specific project |
| `session` | Applies only within the current session |

## Sources

| Source | Description |
|--------|-------------|
| `human` | Created by a human user |
| `agent` | Created by an AI agent |
| `system` | Created by the system automatically |

## Validation Rules

Some collections have additional validation:

- **`rule`**: Recommends `mandatory: true` (warning if false)
- **`handoff`**: Recommends metadata keys `origin` and `destination`
- **`files`**: Recommends metadata key `path`

## MemoryEntry Schema

```
MemoryEntry:
  id: str (UUID)
  content: str
  collection: str           # one of 23 collections
  tags: Dict[str, List[str]] # 11 tag dimensions
  priority: str             # critical | high | medium | low
  mandatory: bool           # if true, ALWAYS injected when tags match
  scope: str                # global | project | session
  source: str               # human | agent | system
  created_at: datetime
  updated_at: datetime
  expires_at: Optional[datetime]
  version: int
  embedding: Optional[vector]
  metadata: Dict[str, Any]
```
