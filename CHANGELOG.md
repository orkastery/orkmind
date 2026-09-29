# Changelog

All notable changes to OrkMind will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Company Brain, modo `context` da seleção**: a seleção com `mode: context` deixa de responder
  `brain.selection.context-unsupported` e devolve o pacote de contexto citável
  `orkmind.company-brain-context/v1`: os ids pedidos e os pais que a concessão deixa ver, cada um
  com a citação inteira, e lacunas tipadas (`entity.unknown`, `entity.withheld`,
  `citation.incomplete`, `owner.unresolved`, `observed.unknown`, `recorded.unknown`). Cada id
  resolve com a semântica do `get`; roda numa transação `REPEATABLE READ, READ ONLY`; o digest é o
  sha256 do JSON canônico. `capabilities` declara `selection_modes`.
- **Company Brain, histórico**: a operação `history` da API devolve as versões append-only de uma
  entidade em ordem de sequência, com a origem de cada uma (fonte, produtor, evento, thread e fase
  do ciclo, horário de gravação) e a marca `rolled_back`. Exige a ação `history` na concessão.
- **Documentação do Company Brain v1** em `docs/company-brain.md`.

O contrato `orkmind.company-brain/v1` não muda: as duas adições são da API
`orkmind.company-brain-api/v1`.

## [0.4.0] - 2026-09-29

### Added

- **Escrita em nome de uma pessoa no Memory Provider**: `ingest_document(..., writer=escopo)`
  e `ingest_bytes(..., writer=escopo)` só gravam o que quem escreve conseguiria ler.
  Classificação fora do alcance levanta `ClassificationOutOfReachError` antes de fatiar ou gerar
  embedding; slug de documento que a pessoa não lê levanta `SlugUnavailableError`, sem revelar o
  documento que o ocupa. As duas recusas herdam de `WriteOutOfScopeError`. A deduplicação por
  conteúdo não atravessa escopo, e `suggest_documents()` completa `[[link]]` só com o que o
  escopo vê. Ver `docs/memory-provider/README.md`.

## [0.3.0] - 2026-09-28

### Changed

- **Licença MIT**, a mesma do Orkastery, com titular Julio Pessoa, no lugar da Apache 2.0.
- README canônico em inglês, com espelho em `README.pt-BR.md`, e CONTRIBUTING, SECURITY e
  CODE_OF_CONDUCT no padrão do Orkastery.
- URLs do projeto apontam para `github.com/orkastery/orkmind` e para `orkmind.com`.

### Added

- **Memory Provider nativo (`orkmind.memory_provider`, extra
  `orkmind[memory-provider]`)**: pacote assíncrono (asyncpg + Pydantic v2) direto sobre
  PostgreSQL + pgvector, com schema próprio, fora de `MemoryStore`. Ver
  `docs/memory-provider/README.md`.
  - Três camadas: Core (`core_blocks`, pinada e versionada), Recall
    (`chat_history`, append-only, janela de 6 turnos, paginação de mensagem
    grande, busca full-text) e Wiki (`wiki_documents` + `wiki_chunks`, HNSW
    `m=16, ef_construction=64`, GIN em `tsv` PT-BR e em `metadata_tags`).
  - Sem compactação destrutiva: contexto do turno limitado por construção;
    `UPDATE`/`DELETE`/`TRUNCATE` no histórico e nas revisões recusados por trigger.
  - `NativeMemoryProvider`: `get_system_context`, `append_history`,
    `search_wiki`, `browse_index`, `ingest_document`/`submit_document`,
    `assemble_turn_context`.
  - Recuperação híbrida com RRF em SQL; candidatos léxicos por lexema raro com
    teto rígido de custo; desempate a favor da evidência léxica; degradação
    sinalizada para léxica pura quando o embedding falha.
  - RBAC determinístico em pré-filtro único (`services/scope.py`) e
    `ExecutiveScopeGuard`: pedido acima do papel é `ValidationError`, não filtro
    silencioso. Meta-índice de 3 níveis que não vaza taxonomia de outra área.
  - Ingestão idempotente por SHA-256, transacional, com fila assíncrona;
    reclassificar gera revisão auditável sem reembedar.
  - `bench/memory_provider_scale.py`: verificação de escala (30 mil chunks) que
    sai com código != 0 se plano, latência, precisão de identificador ou
    isolamento de escopo regredirem. Pegou três defeitos que a suíte funcional
    não pega: ranking léxico linear no corpus (~600 ms), plano genérico do
    prepared statement a partir da 6ª execução (~650 ms -> ~6,2 s) e empate de
    RRF resolvido contra a evidência léxica.
  - Importa sem o driver: `asyncpg` entra sob demanda (mesmo padrão lazy de
    `orkmind.store.factory`), então `pip install -e ".[dev]"` + `pytest` roda a
    suíte inteira sem o extra; só conectar exige o `orkmind[memory-provider]`.
  - **Vault**: `ingest_file()` e `ingest_vault()` ingerem Markdown, texto, CSV,
    PDF e DOCX — documento inteiro, nunca ponteiro. Frontmatter YAML, `#tags`
    inline e titulo saem do proprio arquivo. CSV vira tabela Markdown para o
    chunker conseguir fatiar repetindo o cabecalho. Um arquivo ruim nao derruba
    o vault.
  - **A teia** (`wiki_links`): `[[wikilinks]]`, transclusao, ancora, apelido e
    link Markdown interno; backlinks com o trecho que cita; e link para nota
    inexistente que se religa sozinho quando ela nasce. A lista de alvos nao
    resolvidos e o mapa do que falta escrever. Backlink filtra o documento de
    ORIGEM pelo escopo de quem pergunta — senao o conteudo ficaria protegido e
    a relacao vazaria.
  - **Tags semanticas** (`wiki_document_tags`): tabela propria, com origem
    (frontmatter/inline/ingest) e hierarquia por barra — `rede` traz
    `rede/backbone` sem ninguem declarar a tag-mae.
  - Tools MCP `memory_links` e `memory_tags`; `memory_index` ganhou
    `pendentes`. `content_format` passou a aceitar `docx_text` (DOCX era
    recusado pelo CHECK da tabela).
  - **Servidor MCP** (`python -m orkmind.memory_provider.mcp`, script
    `orkmind-memory-mcp`): seis tools, com `memory_search` e `memory_get` nos
    nomes que o OpenClaw espera de um provedor de memória. Identidade (papel,
    departamentos) vem do ambiente no start, nunca do argumento da tool; todos
    os schemas são `additionalProperties: false` e um teste falha se alguém
    expuser papel. `ORKMIND_PROVIDER_MCP_READONLY=1` esconde a tool de escrita.
  - `OpenRouterEmbeddingProvider` aceita `request_dimensions`, que envia
    `dimensions` no corpo da chamada. Sem isso o default novo
    (`qwen/qwen3-embedding-8b`, nativamente 4096 dims) seria inutilizável: o
    HNSW do pgvector para em 2000 dims no tipo `vector` e 4000 no `halfvec`
    (medido em 0.8.6). Modelos Matryoshka truncam e renormalizam do lado do
    servidor. Validado contra a API real do OpenRouter em 2026-09-20.
  - `embedding_dim` acima de 2000 agora falha com a explicação do limite e do
    contorno, em vez da mensagem genérica do Pydantic.
  - Não entregue: canal multiusuário (ex.: plugin OpenClaw); validação com embeddings reais
    (a suíte usa embedder determinístico não semântico).
- **Storage plugavel (ciclo "storage plugavel", F3.1-F3.9)**: o
  OrkMind deixa de ter um unico backend. Sao tres nesta leva, e o padrao nao
  muda para quem ja usa. Ver `docs/storage-backends/MATRIZ-BACKENDS.md` e
  `docs/storage-backends/GUIA-BACKENDS.md`.
- `[store].backend` no `config.toml`, `[store.options]` como bag de opcoes por
  backend, e as variaveis `ORKMIND_STORE_BACKEND` e `ORKMIND_STORE_OPTIONS`
  (JSON, merge raso). `OrkMindConfig.has_store` nasce ao lado de
  `has_database`, que fica intacta.
- Registry `STORE_BACKENDS` com builders lazy: `pgvector` (default), o alias
  `postgres`, `memory` e `qdrant`. Importar `orkmind.store.factory` nao arrasta
  mais `psycopg` nem `pgvector`.
- `StoreCapabilities` (`orkmind.store.capabilities`): cada backend declara o
  que sabe fazer. Toda degradacao passa por ai, e nenhuma acontece em silencio.
- `orkmind store info`: imprime o backend ativo, as capabilities e os avisos
  ativos (`--json` para consumo por ferramenta).
- `GovernedStore` (`orkmind.store.governed`) e as funcoes puras de
  `orkmind.store.governance`: a governanca (protecao D2, ACL de leitura e de
  escrita, versionamento, ordenacao constitucional, filtro de expiracao e de
  `injection_risk`) sobe para uma camada unica acima dos adapters.
  `create_store()` passa a devolver sempre um store governado.
- `ProfileStore` (`orkmind.store.profiles`) com duas implementacoes:
  `PostgresProfileStore` (tabela `profiles`, comportamento de hoje) e
  `EntryProfileStore` (perfis como entries da colecao `users`). A ACL por grupo
  passa a funcionar em qualquer backend, sem migrar dado de producao.
- `MemoryAdapter` (backend `memory`): storage volatil em dicts, com cosseno em
  numpy. Da cobertura de contrato em qualquer maquina, sem servico externo.
- `QdrantAdapter` (backend `qdrant`, extra `orkmind[qdrant]`): uma colecao com
  payload discriminado por `kind`, vetor nomeado `content` e distancia cosseno.
- `MemoryStore.capabilities`, `search_semantic_text()`,
  `list_entries_without_embedding()` e `set_embedding()` entram no contrato,
  todos concretos e com falha alta e nomeada quando nao suportados.
- `orkmind store export` e `orkmind store import` (`orkmind.store.transfer`):
  migracao entre backends em JSONL com manifesto. A migracao nunca remove a
  origem, o import nunca sobrescreve, o destino ganha snapshot antes de
  qualquer escrita e a reconciliacao de entries `mandatory` e obrigatoria.
- Suite de contrato parametrizada por backend, com guarda destrutiva propria e
  mais rigida para cada backend novo (`tests/contract/backends.py`,
  `tests/unit/test_guarda_backends.py`).
- Teste de equivalencia de governanca entre backends
  (`tests/contract/test_governanca_equivalencia.py`): o mesmo corpus fixo e
  carregado em todos os backends disponiveis e as mesmas consultas rodam com o
  mesmo `requester_id` nos tres. Verifica ordem total identica em
  `search_by_tags`, conjunto identico em busca textual e vetorial com
  `mandatory` sempre na frente, ACL igual para autor, membro de grupo, estranho
  e requester ausente, `ProtectionError` com a MESMA mensagem em todos, e
  `version + 1` com exatamente uma versao arquivada. Com menos de dois backends
  disponiveis o teste PULA com aviso ruidoso, para nao ser aprovado por
  vacuidade.

### Changed

- `SemanticLayer.search_semantic_intent` escolhe entre `search_semantic` e
  `search_semantic_text` pelo que o backend DECLARA em `capabilities`, e avisa
  uma vez por processo quando o backend nao faz busca vetorial.
- `embeddings/backfill.py` passa a usar o contrato do store
  (`list_entries_without_embedding` e `set_embedding`) em vez de SQL cru e da
  conexao interna do adapter. Era o ultimo vazamento de backend no codigo
  Python.
- `scripts/seed_profiles.py` usa `store.profiles` e perdeu os quatro
  `# type: ignore[attr-defined]`. O gate passou de `has_database` para
  `has_store`.
- **Quebra de texto de erro (nao de tipo):** `create_store()` levantava
  `ValueError("No database URL configured...")` e agora levanta
  `BackendMalConfiguradoError` ou `BackendDesconhecidoError`, com mensagem em
  pt-BR nomeando o backend. As duas herdam de `ValueError`, entao quem captura
  por tipo nao quebra; quem captura pelo texto precisa ajustar.
- A limpeza do backend `pgvector` na suite de contrato passa a incluir a tabela
  `profiles`, porque a suite agora exercita identidades.

### Fixed

Correcoes do F4 (QA do ciclo), cada uma com teste de regressao provado vermelho
sem a correcao.

- **Regra `mandatory` sumindo do contexto sob volume** (`memory` e `qdrant`).
  O contrato promete que entries `mandatory` que casam as tags sao SEMPRE
  incluidas, e isso era falso: com centenas de entries casando a consulta, a
  janela de candidatos era preenchida pela ordem NEUTRA do adapter e a regra
  obrigatoria mais antiga nao aparecia. `GovernedStore._garantir_mandatory`
  passa a fazer uma segunda consulta com `mandatory_only=True` nos backends sem
  ordenacao nativa. O `pgvector` nao paga nada por isso.
- **Ordem de `search_by_tags` nao era total no `pgvector` (I9).** Sem over-fetch
  o `LIMIT` do SQL corta, e o `ORDER BY` sem desempate por `id` escolhia
  arbitrariamente entre empates: com `limit=2` os conjuntos devolvidos por
  `pgvector` e `memory` chegavam a ser disjuntos. Entra `, id ASC` em
  `search_by_tags`, `search_by_text`, `search_semantic` e `get_children`.
- **Migracao perdia a ACL em silencio.** `store/transfer.py` nao carregava
  identidades e nao declarava a perda: uma migracao mudava quem enxerga o que e
  o relatorio dizia que estava tudo certo. Agora o manifesto traz `profile_count`
  e `profiles_as_entries`, a perda de identidades e declarada no import e exige
  `--accept-loss`, e o destino reconcilia a contagem de perfis.

### Changed (publicação no PyPI)

- Publicação por tag `v*` com Trusted Publishing, sem token
  (`.github/workflows/publicar.yml`), e CI em todo PR e na `main`
  (`.github/workflows/ci.yml`: Python 3.11 e 3.12 sem banco, conferência do
  sdist e do wheel, testes do plugin OpenClaw).
- sdist com lista fechada de arquivos: fica de fora o material interno
  (estado do Orkastery, relatórios, planos, auditorias, benchmark) e o plugin do
  OpenClaw. Sai o classificador de licença, porque a licença já vai como
  expressão SPDX (`License-Expression: MIT`).
- Os módulos de teste do Company Brain que exigem banco levam a marca
  `integration`; sem banco eles continuam falhando, de propósito.
- DSN padrão do plugin Hermes sem senha no código. Exemplos, docs e testes
  usam `senha-de-teste`.
- Sai `scripts/migracao_memory.py`: migração pontual, com dados pessoais.

### Fixed (publicação no PyPI)

- **Schema do Company Brain dentro do pacote.** `BrainStore.initialize()` lia
  `migrations/company_brain_v1.sql` do repositório e quebrava numa instalação
  pelo wheel. O arquivo passa a `orkmind/store/company_brain_v1.sql`.
- **Tools do plugin OpenClaw na assinatura real.** O OpenClaw 2026.7.1 chama
  `execute(toolCallId, params, signal, onUpdate)`; as cinco tools esperavam
  `execute(params)` e `params.query.slice` quebrava na primeira busca. Agora
  seguem a assinatura real e devolvem `content` e `details`.

### Notes

- `pgvector` continua sendo o default. Uma instalacao sem `[store].backend` no
  TOML e sem `ORKMIND_STORE_BACKEND` resolve para `pgvector` e funciona sem
  editar nada.
- Para `pgvector` nao ha over-fetch de candidatos: o `limit` enviado ao adapter
  e o `limit` pedido, e o SQL emitido e o mesmo de antes do ciclo. Ha teste com
  espiao provando isso nas tres buscas.
- Dividas registradas neste ciclo: **D-1** `psycopg`/`pgvector` seguem
  obrigatorios em vez de extra; **D-2** o plugin OpenClaw continua falando SQL
  com pgvector; **D-3** perfis de producao permanecem na tabela `profiles`;
  **D-4** Qdrant ainda nao e servico no CI; **D-5** `snapshot_*` continua
  devolvendo `dict`; **D-6** `PostgresAdapter` mantem conexao unica sem pool;
  **D-7** o SQL de ACL e de ordenacao do `PostgresAdapter` fica como
  redundancia por um ciclo.

#### Status de maturidade dos backends

- `pgvector` (default): **apto a producao**. E o unico backend com ACL e
  ordenacao nativas no SQL, com as identidades na tabela `profiles`, e nao e
  atingido por nenhuma das dividas criticas abaixo.
- `memory`: **so para teste e desenvolvimento**. Volatil, nao duravel, nao pode
  virar default e nao deve ser habilitado em producao.
- `qdrant`: **EXPERIMENTAL, nao habilitavel em producao neste ciclo**. Entra na
  `main` para consolidar a arquitetura plugavel, nao para ser ligado. As
  dividas S1 e S4 abaixo sao bloqueantes para produzi-lo, e a evidencia deste
  ciclo veio do Qdrant em modo local, que tem semantica de busca textual
  diferente da do servidor e ignora indices de payload.

#### Dividas conhecidas do F4 (registradas, nao bloqueiam a `main`)

O QA do F4 fechou com CHANGES NEEDED por dois achados criticos que continuam
ABERTOS. Nenhum dos dois atinge o default `pgvector`, e os dois so tem efeito
em backends que nao estao aptos a producao, entao a consolidacao da `main`
segue com as dividas registradas.

- **S1 (D-F4-S1), CRITICO, ABERTO: identidade forjavel pela API de memoria**
  (`memory` e `qdrant`). Em backends que usam `EntryProfileStore` os perfis sao
  entries comuns da colecao `users`, publicas e sem `protected`. Um agente que
  pode gravar memoria pode criar um grupo com ele proprio dentro, se incluir num
  grupo real ou apagar o grupo real, e com isso ler entries `restricted` das
  quais nao era destinatario. **O `pgvector` e imune**, porque le identidade da
  tabela `profiles`, fora do alcance da API de memoria. A correcao e decisao de
  desenho e ficou para o proximo ciclo; o comportamento correto ja esta escrito
  como teste `xfail(strict=True)`
  (`TestA9IdentidadeNaoEForjavelPelaApiDeMemoria`), entao quando a correcao
  entrar o teste passa, o `strict` derruba a suite e obriga a remover o
  marcador. Nao da para esquecer.
- **S4 (D-F4-S4), CRITICO, ABERTO: TTL nao apaga no Qdrant.**
  `garbage_collect` remove os pontos `kind="entry"` e deixa os `kind="version"`,
  que guardam o conteudo verbatim. Uma entry com TTL de retencao some das
  buscas mas continua legivel por `get_history` para sempre, porque sem a
  entry-mae nenhum `delete` futuro a alcanca. `pgvector` e `memory` apagam
  corretamente. Enquanto isso nao for corrigido, TTL no Qdrant nao significa
  apagamento e o backend nao pode receber dado com prazo de retencao.
- Tambem bloqueantes para produzir o Qdrant: **D-F4-M1** (trunca antes de
  ordenar em `search_by_tags` e `search_by_text`) e **D-B/D-4** (falta evidencia
  contra um Qdrant SERVIDOR real). As demais dividas do F4 (D-F4-1 a D-F4-8) e
  as menores M1 a M11 seguem registradas pelos mantenedores.

#### Estado da suite ao fechar o ciclo

```
1002 passed, 2 skipped, 1 xfailed
```

Com os tres backends configurados (`ORKMIND_TEST_DATABASE_URL`,
`ORKMIND_TEST_QDRANT_URL`, `ORKMIND_TEST_QDRANT_PREFIX`). O `xfailed` e o S1
acima. Sem nenhuma variavel `ORKMIND_TEST_*` a guarda destrutiva pula os
backends externos e a suite fecha em `721 passed, 284 skipped`, sem tocar em
base de producao.

---

### Added (ciclo "sempre gravar", anterior)

- **Gravacao garantida (solucao "sempre gravar")**: conteudo de agente e
  cron nao pode mais ficar orfao quando as tools de execucao faltam no
  toolset do job ou quando o backend esta fora. Ver `docs/sempre-gravar.md`.
- `orkmind.spool`: fila de saida (outbox) em disco com escrita atomica,
  transicoes por rename (`pending/` -> `done/` | `failed/`), backoff
  progressivo e `content_hash` como chave deterministica de deduplicacao.
- `orkmind.api`: API HTTP local idempotente (Starlette, sem dependencia
  nova). `POST /entries` responde `status: created|duplicate`,
  `GET /entries/by-hash` faz o lookup de idempotencia, `GET /health`
  reporta vivacidade. Token Bearer obrigatorio com falha fechada,
  comparacao em tempo constante, conferencia de simetria de hash e
  lookup que nunca devolve conteudo.
- Comando `orkmind api` para servir a API (loopback por padrao).
- `scripts/orkmind_drain.py`: drainer externo ao agente, com `--once`
  (cron), `--watch` (continuo), `--status` (fila em JSON) e `--backfill`
  (migracao de orfaos legados, repetivel e nao destrutiva).
- `MemoryStore.find_by_content_hash()` e implementacao no
  `PostgresAdapter`, base do lookup de idempotencia.
- `migrations/f4_content_hash_idempotencia.sql`: indice unico parcial
  em `memories(collection, content_hash)`, com consulta de diagnostico
  de duplicatas preexistentes.
- CLI `orkmind add`: opcoes `--content-hash` (conferencia de simetria),
  `--dedupe` (idempotente por hash) e `--json` (saida para automacao).
  Retrocompativel: sem as flags, o comportamento nao muda.
- **Evolucao core (31/08/2026) - Blocos A/B/C**: **A) gap OpenClaw
  N1/N2/N3** em `integrations/openclaw/memory-orkmind`: modulo `rules.ts`
  (port literal da governanca, getMandatoryRules sem embedder, bucket de
  regras + truncamento), shim do SDK com o contrato real de
  `before_prompt_build`, garantia constitucional (N1) sempre presente no
  prompt, guardrail anti-destruicao e via hook (contingencia D1), heuristica
  de `allowPromptInjection`, seed idempotente das regras em `orkmind_openclaw`,
  script de sync + runner de testes TS. **B) Recuperacao**: busca hibrida
  (deterministica + vetorial com rerank RRF) como fonte adicional em
  `query_for_context`, gate/embedder nos pontos de entrada Python, timeout
  curto do embedder no caminho de consulta, janela de sessao ancorando o
  prefetch no Hermes, detectores de palavra-chave pt-BR. **C) Benchmark**:
  `bench/` com runner Hermes e contra o plugin OpenClaw, baseline retroativo
  pre-port (M1/M2 = 0), evidencias e higiene documental.

### Changed (ciclo "sempre gravar", anterior)

- `PostgresAdapter.initialize()` cria o indice unico parcial de
  idempotencia. A criacao e tolerante: se o banco ja tiver duplicatas,
  apenas avisa, sem derrubar o CLI. Resolver duplicata e decisao humana.
- Plugin Hermes 1.0.0 -> 1.1.0: `orkmind_store` estagia na fila ANTES de
  tentar gravar e distingue `status: armazenado` (com id real) de
  `status: pendente` (id nulo). Ver
  `integrations/hermes/orkmind/CHANGELOG.md`.

## [0.1.0] - 2026-08-22

### Added

- Core ontology with 16 typed collections and 5 semantic tag dimensions
- MemoryEntry model (Pydantic v2) with full metadata support
- MemoryStore abstract contract (ABC) with CRUD, tag search, text search, semantic search
- PostgresAdapter with pgvector for vector search, tsvector for FTS, GIN for tags
- SemanticLayer for context-aware memory retrieval with mandatory injection and token budget
- Context detectors: Keyword and File path based detection
- MCP Server (stdio) for Claude Code integration with 7 tools
- Hermes MemoryProvider for Hermes agent integration
- CLI with commands: add, list, search, detect, remove, export, import, stats, gc
- Configuration via env vars and ~/.orkmind/config.toml
- 75 tests: unit (51), integration (9), contract (15)
- Documentation: ontology, integration guide, MCP setup, Hermes setup
- Skills: SKILL.md for Claude Code, AGENTS.md for Codex
- Apache 2.0 license

### Fixed

- pgvector `Vector.tolist()` compatibility in `query_for_context` return values
- OR-across-dimensions logic in tag search queries (was incorrectly using AND across all dimensions)
