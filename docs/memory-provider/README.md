# Memory Provider nativo

Pacote `orkmind.memory_provider`. Memória de agente em três camadas, assíncrona,
direto sobre PostgreSQL + `pgvector`, sem runtime externo (Letta, Mem0) e **sem
compactação destrutiva**. Não é de um agente nem de um cliente: serve a qualquer
pessoa, empresa ou stack agêntica que precise destas capacidades. Agente, papéis,
departamentos, tipos de documento e canal são todos dados de configuração.

- Código: `src/orkmind/memory_provider/`
- Exemplo executável: `examples/memory_provider.py`
- Testes: `tests/unit/test_memory_provider_*.py`, `tests/integration/test_memory_provider.py`
- Verificação de escala: `bench/memory_provider_scale.py`
- Servidor MCP: `src/orkmind/memory_provider/mcp/` (`python -m orkmind.memory_provider.mcp`)
- Extra de instalação: `pip install 'orkmind[memory-provider]'` (traz `asyncpg`)
  — o pacote **importa sem o driver**; só conectar exige o extra, e a falta dele
  vira uma mensagem dizendo o que instalar. Assim o chunker, os modelos e o RBAC
  são testáveis em qualquer máquina, sem Postgres nem driver.

## O problema que isto resolve

"Memória em 99%, vou compactar." O defeito não é o resumo ser ruim; é o desenho
que o torna necessário: tratar o histórico como algo que **mora dentro do
contexto** e, quando enche, precisa ser trocado por uma síntese que ninguém
audita.

Aqui nada mora no contexto. Tudo mora no banco, íntegro, e o que vai para o
modelo a cada turno é uma **visão montada na hora e limitada por construção**:

| Camada | O que entra no turno | O que fica no banco |
|---|---|---|
| Core | 3 blocos pinados, sempre | todas as versões de cada bloco |
| Recall | os últimos 6 turnos (mensagem gigante entra paginada) | todos os turnos, para sempre |
| Wiki | ≤ 5 chunks de ~200–400 tokens | documentos íntegros + todas as revisões |

Não existe caminho pelo qual esse contexto cresça com a idade da conversa. O
teste `test_contexto_do_turno_nao_cresce_com_a_idade_da_conversa` mede isso: o
total de tokens do turno com 10 e com 210 turnos de histórico difere em no
máximo 12 tokens.

Turno antigo não se resume: **se recupera** (`search_history`, full-text) ou se
lê paginado (`read_history_message`). E o banco recusa o contrário:
`chat_history`, `wiki_document_versions` e `core_blocks` são append-only **por
trigger**, inclusive contra `TRUNCATE`.

## Arquitetura

```mermaid
flowchart TD
    CH[Canal autenticado<br/>Teams · Slack · sua API] -->|UserContext| P[NativeMemoryProvider]
    P --> C[CoreMemoryService<br/>core_blocks]
    P --> R[RecallService<br/>chat_history]
    P --> M[MetaIndexService<br/>meta_index]
    P --> RT[RetrievalService<br/>RRF denso + léxico]
    P --> I[IngestionService<br/>chunking + embeddings + fila]
    RT --> S{{scope.py<br/>pré-filtro RBAC único}}
    M --> S
    I --> CK[chunking.py]
    S --> PG[(PostgreSQL 17 + pgvector<br/>HNSW · GIN tsv · GIN jsonb)]
    C --> PG
    R --> PG
    I --> PG
```

```
memory_provider/
  provider.py            NativeMemoryProvider (fachada)
  config.py              MemoryProviderSettings, ChunkingOptions
  embeddings.py          lote, concorrência, retry, conferência de dimensão
  tokens.py              estimativa de tokens sem tokenizer externo
  db/schema.sql          DDL canônico (template: dimensão e idioma)
  db/migrations.py       apply_schema, create_pool
  models/schemas.py      Pydantic v2 (inclui ExecutiveScopeGuard)
  services/
    core_memory.py  recall.py  meta_index.py
    chunking.py     ingestion.py  retrieval.py  scope.py
```

## Banco

Tabelas: `core_blocks`, `chat_history`, `wiki_documents`,
`wiki_document_versions`, `wiki_chunks`, `meta_index` e `memory_provider_meta`.

Índices de `wiki_chunks`:

```sql
CREATE INDEX ... USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX ... USING gin (tsv);            -- full-text PT-BR
CREATE INDEX ... USING gin (metadata_tags);  -- filtros por contenção JSONB
```

Garantias que o **banco** dá, e não o código Python:

- **Append-only por trigger** em `chat_history` e `wiki_document_versions`
  (`UPDATE`, `DELETE` e `TRUNCATE` levantam `restrict_violation`).
- **`core_blocks` imutável**: a única mutação aceita é aposentar a versão
  vigente. Trocar o texto insere a versão seguinte.
- **Campo auditado só muda com revisão**: em `wiki_documents`, alterar
  conteúdo, classificação, escopo ou status sem `version = version + 1` é
  recusado — inclusive por SQL manual. Baixar a classificação de um estudo sem
  deixar rastro não passa. Toda revisão é copiada por trigger para
  `wiki_document_versions`.
- **Hierarquia do meta-índice**: nó de nível *n* exige pai de nível *n − 1*.
- **Mudança incompatível é recusada**: `apply_schema` compara dimensão do vetor
  e idioma do FTS com `memory_provider_meta` e levanta `SchemaMismatchError` em
  vez de recriar a coluna e invalidar os vetores.

`wiki_chunks` é a única tabela com `DELETE`: chunks são derivados e sempre dá
para refazê-los a partir de `raw_content`. Classificação e escopo **não** são
copiados para os chunks — a fonte única é `wiki_documents`, via `JOIN`, para que
chunk e documento nunca discordem sobre quem pode ler.

## API

```python
async with await NativeMemoryProvider.connect(settings) as memory:
    system  = await memory.get_system_context("assistant", user)          # Tier 1
    message = await memory.append_history(session, "user", texto)    # Tier 2
    hits    = await memory.search_wiki(pergunta, scope, limit=5)     # Tier 3
    arvore  = await memory.browse_index(scope, depth=2)              # meta-índice
    turno   = await memory.assemble_turn_context("assistant", session, user, pergunta)
```

| Método | Camada | Observação |
|---|---|---|
| `get_system_context(agent_id, user_context)` | Core | `persona` e `system_invariants` são obrigatórios: sem eles levanta `CoreMemoryMissingError`. `.render()` devolve o texto do system prompt. |
| `set_core_block(...)` | Core | API de **operador**. Não exponha como ferramenta do agente. |
| `append_history(session_id, role, content, tool_calls)` | Recall | Papéis: `user`, `assistant`, `tool_call`, `tool_result`. |
| `get_recent_history(session_id, turns=6)` | Recall | Janela. Mensagem acima de `recall_max_message_tokens` entra paginada, com ponteiro. |
| `read_history_message(id, offset)` | Recall | Lê o restante de uma mensagem paginada. |
| `search_history(query, session_id= / user_id=)` | Recall | Exige sessão ou usuário; sem isso atravessaria conversas alheias. |
| `get_full_history(session_id)` | Recall | Trilha íntegra, para auditoria. |
| `search_wiki(query, scope_filter, limit=5)` | Wiki | Híbrida RRF. `search_wiki_detailed` devolve também `token_total` e `degraded`. |
| `get_document(slug_or_id, scope)` | Wiki | Documento íntegro, sob o mesmo escopo da busca. |
| `ingest_document(request)` / `submit_document(request)` | Wiki | Direta / em fila, sem travar o canal. |
| `retire_document(slug, actor=)` | Wiki | Sai da busca; o íntegro permanece. Não há exclusão física. |
| `browse_index(scope, path, depth)` | Meta-índice | Navegação recursiva. |
| `assemble_turn_context(...)` | JIT | As três leituras em paralelo + contagem de tokens por camada. |

Para o contrato de memória do OpenClaw (`memory_search` / `memory_get`):
`search_wiki` é o `memory_search`, e
`get_document` / `read_history_message` cobrem o `memory_get`. **O plugin
OpenClaw que expõe essas tools ainda não foi escrito** — ver
[Não entregue](#não-entregue).

## Ingestão

1. **SHA-256** do conteúdo normalizado (fim de linha e bordas; `raw_content` é
   gravado intacto). Hash já presente e sem `force_update` → `DUPLICATE`, antes
   de fatiar ou chamar a API de embedding. Duplicata não custa nada.
2. **Chunking** em thread e **embeddings** em lote, fora de transação.
3. **Uma transação** grava documento, chunks e apontadores do meta-índice — tudo
   ou nada. Advisory locks por slug e por hash serializam ingestões concorrentes
   e a decisão do passo 1 é refeita dentro do lock.

| Situação | Resultado |
|---|---|
| slug novo, conteúdo novo | `CREATED` v1 |
| mesmo slug, conteúdo diferente (ou `force_update`) | `UPDATED` v+1, chunks trocados |
| mesmo slug e conteúdo, **classificação/escopo/tags diferentes** | `METADATA_UPDATED` v+1, **sem reembedar** |
| mesmo slug, tudo igual | `DUPLICATE`, nada gravado |
| slug novo, conteúdo que já existe em outro slug | `DUPLICATE`, `duplicate_of=<slug>` |

A terceira linha existe por governança: se reingerir o mesmo conteúdo com
`access_level` mais alto devolvesse só "duplicata", a reclassificação seria
ignorada em silêncio.

`access_level` é **obrigatório** e sem default: quem ingere classifica.

`submit_document` devolve um `IngestJob` na hora. A fila vive em memória: se o
processo cair, os jobs pendentes se perdem — o chamador reenvia, e a
idempotência por hash garante que reenviar é seguro.

### Chunking

Segue a estrutura, não a contagem de caracteres:

- títulos até `###` são fronteira **dura**; um chunk nunca atravessa seções;
- a unidade é o bloco. Bloco que não cabe desce um degrau: lista → itens,
  parágrafo → frases, tabela → linhas (repete o cabeçalho), código → linhas
  (repete a cerca);
- overlap de **10% a 15%** entre chunks consecutivos *da mesma seção*, alinhado
  a início de frase quando dá. Sem overlap entre seções nem depois de
  tabela/código;
- chunk minúsculo (título solto) é fundido com o vizinho;
- o embedding vê o caminho de títulos (`Contrato X > Vigência > Prazo`), e o
  `tsv` dá peso A a esse caminho e B ao corpo.

HTML é convertido para Markdown mínimo antes de fatiar. PDF deve chegar já
extraído como texto (`content_format="pdf_text"`); o pacote não extrai PDF.

## Recuperação híbrida

Uma consulta SQL funde as duas pernas sobre o mesmo snapshot:

$$RRF(d) = \sum_i \frac{1}{k + r_i(d)} \qquad k = 60$$

- **densa**: `embedding <=> $1` no HNSW, com `hnsw.iterative_scan` (pgvector ≥
  0.8) para que um pré-filtro seletivo não devolva menos que o `LIMIT`;
- **léxica**: `tsv @@ tsquery` no GIN. É ela que acerta sigla, jargão e número
  de contrato. `CT-2024/0187` vira os lexemas `ct`, `-2024`, `/0187`.

Três decisões que só apareceram medindo em escala
(`bench/memory_provider_scale.py`, 30 mil chunks):

1. **Candidatos só dos lexemas raros.** `plainto_tsquery` liga com AND e uma
   pergunta natural raramente tem todas as palavras num chunk; ligar com OR dá
   recall, mas "contrato" casa com meio corpus e o Postgres ranqueia cada linha
   casada (~600 ms). Solução: mede-se a frequência de cada lexema com teto
   (`sparse_candidate_cap`, em cache) e só os mais raros geram candidatos; o
   ranking continua usando todos. É IDF: quem discrimina é `/0187`.
2. **Teto rígido** de linhas ranqueadas na perna léxica. Quando é atingido (AND
   de palavras onipresentes), a perna léxica ranqueia uma **amostra** e quem
   ordena de fato é a densa. O custo tem teto; a precisão léxica, nesse caso e
   só nesse, não.
3. **`plan_cache_mode = force_custom_plan`.** A partir da 6ª execução o Postgres
   adota o plano genérico do prepared statement, em que os filtros opcionais
   (`$n IS NULL OR ...`) não somem. Medido: ~11 ms → ~47 ms; numa versão anterior
   da consulta, ~650 ms → ~6,2 s.

**Desempate a favor da léxica**: rank 1 só na densa e rank 1 só na léxica valem
o mesmo 1/(k+1). Vizinho mais próximo existe *sempre*, mesmo quando nada no
corpus responde à pergunta; casamento léxico só existe quando há evidência.

Se a API de embedding falhar ou estourar `query_embedding_timeout_s`, a busca
**degrada para léxica pura** e avisa em `SearchResponse.degraded`. Dimensão
errada não é degradação: levanta `EmbeddingDimensionError`.

## Vault: documentos inteiros, teia e tags

O provider guarda o documento **inteiro**, nunca um ponteiro para ele. Para
formato textual isso e literal — `raw_content` recebe o arquivo byte a byte,
frontmatter incluso. Para binario (PDF, DOCX) guarda-se o texto extraido, e
`metadata.source_format` registra de onde veio; o binario original continua
onde estava, apontado por `source_url_or_path`. E uma escolha: o que a memoria
serve ao agente e texto pesquisavel e citavel. Guardar o PDF em si so faz
sentido para reimprimir o original, e ai o lugar e um bucket de objetos.

```python
await memory.ingest_file("contratos.csv")       # vira tabela Markdown
await memory.ingest_vault("/caminho/do/vault")  # pasta inteira, recursiva
```

| entra | vira | observacao |
|---|---|---|
| `.md`, `.markdown`, `.mdx` | `markdown` | frontmatter, wikilinks e `#tags` lidos |
| `.txt`, `.log`, `.rst` | `text` | byte a byte |
| `.html`, `.htm` | `html` | convertido para Markdown no chunking |
| `.csv`, `.tsv` | `markdown` | vira tabela; o chunker repete o cabecalho em cada pedaco |
| `.pdf` | `pdf_text` | por pagina, com marcador `## Pagina N` para citar |
| `.docx` | `docx_text` | titulo do Word vira titulo Markdown; tabelas preservadas |

`ingest_vault` ignora `.obsidian/`, `.git/` e binarios nao suportados, e **um
arquivo ruim nao derruba o vault**: ele entra em `falhas` e os outros seguem.
PDF digitalizado sem camada de texto falha explicitamente pedindo OCR.

### A teia

O que separa um vault de uma pilha de documentos sao as arestas.

```python
await memory.backlinks("rede-neutra", escopo)        # quem aponta para ca
await memory.outgoing_links("rede-neutra", escopo)   # para onde aponta
await memory.unresolved_links(escopo)                # o que falta escrever
await memory.neighborhood("rede-neutra", escopo, depth=2)   # o grafo local
```

- **`[[wikilinks]]`**, `![[transclusao]]`, `[[nota#secao]]`, `[[nota^bloco]]`,
  `[[nota|apelido]]` e links Markdown internos viram linhas em `wiki_links`.
  Link externo (`https://`) nao entra: nao e aresta da teia interna.
- **Link para nota que ainda nao existe e valido**, nao erro. Ele nasce com
  `target_document_id` nulo e **se religa sozinho quando a nota nasce**. A
  lista desses alvos e um mapa do que a organizacao ja citou e nunca escreveu
  — pauta de Company Brain, nao defeito.
- **Backlink carrega o trecho que cita.** Sem contexto, o backlink obriga a
  abrir a nota de origem so para saber por que ela aponta.
- **Codigo nao e link.** `[[isso]]` dentro de bloco cercado ou de crase e
  texto literal, e `#comentario` de shell nao vira tag.
- **Backlink respeita o RBAC do lado de LA.** A consulta filtra o documento de
  ORIGEM pelo escopo de quem pergunta. Sem isso, um documento publico
  entregaria a existencia de um confidencial que aponta para ele: o conteudo
  ficaria protegido e a relacao, nao.

### O grafo local

`neighborhood()` devolve um documento e `depth` saltos de **arestas autoradas**,
com a caminhada acontecendo no banco (CTE recursiva). Ligar dois documentos por
tag compartilhada e adjacencia *inferida*: duas notas com `opex` aparecem juntas
sem que ninguem as tenha relacionado. `wiki_links` guarda relacao *autorada* —
o mapa deixa de ser de temas e vira de dependencias.

Grafo global de milhares de nos e a critica mais justa que o Obsidian recebe:
bonito, e nao responde nada. Por isso a vizinhanca e local por padrao, com teto
de nos que **avisa quando cortou** (`truncado`), em vez de truncar em silencio e
fazer a tela mentir sobre o tamanho.

- **A caminhada e mao dupla**: quem o documento cita e quem o cita.
- **O escopo vale nos DOIS lados de cada aresta.** Um documento invisivel nem
  serve de ponte: se a caminhada o atravessasse, o no do outro lado apareceria e
  entregaria que existe relacao ali — o conteudo ficaria protegido e a relacao,
  nao. O filtro e deliberadamente redundante (na aresta e na caminhada), e isso
  foi verificado por mutacao: derrubar so um dos dois nao vaza, derrubar os dois
  vaza. Nao e copia; e trava dupla.
- **Documento invisivel e documento inexistente dao a mesma resposta vazia.**
- **Fantasma nao some**: o citado e nunca escrito volta como aresta
  `existe: false`, para a tela desenhar tracejado.

### Tags semanticas

Tabela propria (`wiki_document_tags`), nao JSONB, porque tag aqui e coisa
navegavel. Vem do frontmatter (`tags:`), de `#tags` inline e do pedido de
ingestao, com a origem registrada. **A barra e hierarquia**: quem pede `rede`
recebe tambem `rede/backbone`, sem ninguem ter declarado a tag-mae — o
espirito `broader`/`narrower` do SKOS que o roadmap ja adotava como
referencia, agora com uma primeira implementacao.

```python
await memory.list_tags(escopo)                  # painel: tag -> quantos documentos
await memory.documents_by_tag("rede", escopo)   # traz rede/backbone junto
```

## Embeddings: o que foi medido

Validado em 2026-09-20 contra o endpoint real do OpenRouter (ele **serve**
`/embeddings`, ao contrário do que se costuma supor):

| modelo | dims nativas | aceita `dimensions` |
|---|---|---|
| `qwen/qwen3-embedding-8b` (default) | 4096 | sim |
| `openai/text-embedding-3-small` | 1536 | sim |
| `perplexity/pplx-embed-v1-0.6b` | 1024 | sim |

**O default de 4096 dims não é indexável.** Medido no pgvector 0.8.6: o HNSW
aceita no máximo **2000** dimensões no tipo `vector` e **4000** no `halfvec`.
Por isso `embedding_request_dimensions=True` é o padrão: o provider pede o
vetor já em `embedding_dim`, e modelos Matryoshka (Qwen3, text-embedding-3)
truncam e **renormalizam** do lado deles — os vetores voltam com norma 1.0,
que é exatamente o que a distância de cosseno quer. Configurar
`embedding_dim` acima de 2000 falha na validação, com a explicação junto.

### A perna densa contribui? Ablação com modelo real

12 documentos / 17 chunks em português, 22 perguntas (15 de paráfrase, 7 de
identificador exato), `qwen3-embedding-8b` a 1536 dims. A mesma SQL rodou com
uma perna desligada por vez:

| configuração | recall@1 | recall@3 | recall@5 |
|---|---|---|---|
| só densa | 15/22 | **22/22** | 22/22 |
| só léxica | 15/22 | 16/22 | 16/22 |
| **híbrida (atual)** | 15/22 | **22/22** | 22/22 |
| híbrida, desempate pró-densa | 12/22 | 22/22 | 22/22 |

Leituras honestas:

- **A métrica que importa é recall@3**, não top-1: o provider entrega ~5
  chunks ao modelo, e acertar em terceiro serve tanto quanto acertar em
  primeiro. Medido só por top-1, a híbrida parece empatar com cada perna
  isolada; a 3 ela é perfeita.
- **A perna léxica sozinha não acha 6 das 22** — todas de paráfrase. Ela não é
  redundante em relação à densa, é complementar.
- **Num corpus deste tamanho a densa sozinha bastaria.** O que justifica a
  perna léxica é (a) o corpus grande, onde o identificador exato afunda no
  ruído — no teste de 30 mil chunks é ela, e só ela, que traz o número do
  contrato em primeiro; e (b) o modo degradado, quando a API de embedding cai.
- **O desempate pró-léxica se confirmou.** Trocá-lo por pró-densa piora o
  recall@1 (15 → 12) e não melhora o recall@3. A escolha original foi feita
  contra um embedder falso; agora ela tem evidência real.
- Distância de cosseno nos acertos densos: **0,35 a 0,55** (mediana 0,47).
  Use isso para calibrar `dense_max_distance`, que continua `None` por padrão.
  Consequência de deixar `None`: a busca **nunca volta vazia** — a perna densa
  sempre devolve os vizinhos mais próximos, por piores que sejam.

## RBAC e escopo

Pré-filtro determinístico, escrito **em um único lugar** (`services/scope.py`) e
usado pela busca, pelo meta-índice e pela leitura de documento:

```sql
d.status = 'active'
AND d.access_level = ANY($niveis)
AND ($cross_department OR d.department_scope = '{}' OR d.department_scope && $departamentos)
```

- **Hierarquia linear**: `OPERATIONAL` < `EXECUTIVE` < `SYSTEM_ADMIN`; cada papel
  lê o próprio nível e os abaixo.
- **Departamento**: `department_scope = {}` vale para a empresa toda. Um
  executivo vê a empresa toda e o próprio departamento; atravessar departamentos
  (consolidações cross-department) exige `cross_department=True`, que só
  `EXECUTIVE`+ pode pedir.
- **Identidade vem do canal, nunca do agente.** `UserContext` é montado pela
  integração (Teams, Slack, sua API) a partir do usuário autenticado. `ExecutiveScopeGuard`
  confere o filtro contra essa identidade.
- **Fail-closed, e escalada é erro**: `MemoryScopeFilter()` sem papel é
  `OPERATIONAL`. Pedir nível acima do papel, ou declarar no filtro um papel maior
  que o do usuário, levanta `ValidationError` **antes de qualquer SQL** — não é
  silenciosamente reduzido. Tentativa de escalada tem que aparecer.
- **Sigiloso e inexistente devolvem o mesmo erro** (`DocumentNotFoundError`).
- **O meta-índice não vaza taxonomia**: nó criado na ingestão herda a
  classificação do documento, e um nó só aparece se houver ao menos um documento
  visível na subárvore — o NOC não lê `RH/DESLIGAMENTOS_Q4` só por navegar.

Use sempre `MemoryScopeFilter.for_user(user_context, ...)`. Passar
`user_context=` a `search_wiki` aplica o guard mesmo em filtro montado à mão.

> **Limite honesto.** Se a integração chamar `search_wiki(query, scope)` com um
> `MemoryScopeFilter` montado à mão e **sem** `user_context`, o provider confia
> no papel declarado no filtro: ele não tem como saber quem é o usuário. O RBAC
> vale tanto quanto a integração for fiel em montar o `UserContext` a partir do
> canal autenticado.

### Escrita em nome de uma pessoa

A ingestão de sistema (vault, fila, CLI) não tem dono e grava o que mandarem.
Quando a escrita vem de uma pessoa — uma tela, um canal — passe o escopo dela
como `writer`:

```python
escopo = MemoryScopeFilter.for_user(user_context)
await memory.ingest_document(pedido, writer=escopo)
await memory.ingest_bytes(conteudo, "atas/Ata.md", source="upload:atas/Ata.md",
                          ingested_by=user_context.user_id, writer=escopo)
```

A regra é a da leitura: **só grava o que conseguiria ler.**

- Classificação acima do papel, ou em departamento que a pessoa não lê, levanta
  `ClassificationOutOfReachError` antes de fatiar ou chamar embedding.
- Slug de documento que ela não lê levanta `SlugUnavailableError`, mesmo que o
  documento esteja aposentado. A recusa diz só que o nome está em uso; nada do
  documento que o ocupa.
- A deduplicação por conteúdo só olha o que ela alcança. Conteúdo idêntico a um
  documento sigiloso vira documento próprio: apontar para lá entregaria o slug
  dele, e recusar deixaria a pessoa sem o próprio documento.
- Quem lê um documento pode atualizá-lo. Cada atualização é uma revisão nova,
  com `ingested_by` registrando quem foi.

As duas recusas herdam de `WriteOutOfScopeError`. A condição não é uma segunda
regra: `classification_reach_sql` aplica a mesma `document_scope_clause` a uma
linha montada com a classificação pedida.

`suggest_documents(texto, escopo)` alimenta o autocompletar de `[[link]]`: só o
que o escopo vê, começo do slug primeiro, e cada item traz `link`, o texto que
entre colchetes resolve para aquele documento.

## Servidor MCP

```bash
pip install 'orkmind[memory-provider]'
export ORKMIND_PROVIDER_DATABASE_URL=postgresql://user:pass@localhost:5433/memoria
export OPENROUTER_API_KEY=...
export ORKMIND_PROVIDER_MCP_USER_ID=ana
export ORKMIND_PROVIDER_MCP_ROLE=OPERATIONAL
export ORKMIND_PROVIDER_MCP_DEPARTMENTS=noc
python -m orkmind.memory_provider.mcp          # ou: orkmind-memory-mcp
```

| tool | para quê |
|---|---|
| **`memory_search`** | **A principal.** Busca híbrida; devolve trechos, não documentos inteiros. |
| `memory_get` | Documento inteiro, **paginado** por `offset`. |
| `memory_index` | Mapa (domínio › tema › documento). Barato; use antes de buscar. `pendentes: true` mostra o que falta escrever. |
| `memory_links` | A teia de um documento nas duas direções, numa chamada. |
| `memory_tags` | Painel de tags com contagem, ou os documentos de uma tag. |
| `memory_recall` | Full-text em conversas antigas. |
| `memory_read_page` | Continua um turno longo do `memory_recall`. |
| `memory_remember` | Grava um documento. Sai da lista com `ORKMIND_PROVIDER_MCP_READONLY=1`. |

**Identidade não é argumento de tool.** Papel e departamentos são resolvidos
uma vez, no start, a partir do ambiente. Nenhum schema de tool tem campo de
papel, e todos declaram `additionalProperties: false` — se viessem no
argumento, bastaria o modelo pedir `EXECUTIVE` e o RBAC viraria enfeite. Um
teste falha se alguém adicionar esse campo. O modelo só consegue **estreitar**
(`doc_types`, `index_path`, `tags`).

`memory_search` e `memory_get` usam de propósito os nomes que o OpenClaw
espera de um provedor de memória: expor nome fora desse contrato já custou uma investigação inteira ao projeto.

A superfície é deliberadamente pequena, com uma tool claramente principal. É a
lição que o CodeGraph publica no próprio README: uma ferramenta forte guia o
agente melhor do que um menu de ferramentas estreitas, e gasta menos contexto.

## Configuração

`MemoryProviderSettings.from_env()` lê `ORKMIND_PROVIDER_<CAMPO>` (e
`ORKMIND_PROVIDER_CHUNK_<CAMPO>` para o chunking). Os principais:

| Variável | Default | |
|---|---|---|
| `ORKMIND_PROVIDER_DATABASE_URL` | — | Obrigatória. Sem default com senha. |
| `ORKMIND_PROVIDER_DB_SCHEMA` | `public` | Isola as tabelas num schema próprio. |
| `ORKMIND_PROVIDER_EMBEDDING_DIM` | `1536` | Máx. 2000 (limite do HNSW para `vector`). |
| `ORKMIND_PROVIDER_EMBEDDING_MODEL` | `qwen/qwen3-embedding-8b` | Via endpoint OpenAI-compatible. |
| `ORKMIND_PROVIDER_EMBEDDING_REQUEST_DIMENSIONS` | `true` | Pede o vetor já em `embedding_dim`. |
| `ORKMIND_PROVIDER_FTS_CONFIG` | `portuguese` | |
| `ORKMIND_PROVIDER_RECALL_WINDOW_TURNS` | `6` | |
| `ORKMIND_PROVIDER_RRF_K` | `60` | |
| `ORKMIND_PROVIDER_LEXICAL_MODE` | `any` | `all` = AND estrito. |
| `ORKMIND_PROVIDER_SPARSE_CANDIDATE_CAP` | `2000` | Teto de linhas ranqueadas na perna léxica. |
| `ORKMIND_PROVIDER_CHUNK_MAX_TOKENS` | `350` | |
| `ORKMIND_PROVIDER_CHUNK_OVERLAP_RATIO` | `0.12` | Aceita só 0.10–0.15. |

## Rodando

```bash
# Postgres 17 + pgvector descartável na 5433
docker run --rm -d --name orkmind-mp-pg -e POSTGRES_PASSWORD=senha-de-teste \
  -e POSTGRES_DB=memory_provider_test -p 127.0.0.1:5433:5432 pgvector/pgvector:pg17

pip install -e '.[memory-provider,dev]'
export ORKMIND_PROVIDER_TEST_DATABASE_URL=postgresql://postgres:senha-de-teste@127.0.0.1:5433/memory_provider_test

pytest tests/unit/test_memory_provider_chunking.py tests/unit/test_memory_provider_schemas.py
pytest tests/integration/test_memory_provider.py      # -m memory_provider
python bench/memory_provider_scale.py                 # sai != 0 se uma garantia quebrar
```

Os testes de integração exigem `ORKMIND_PROVIDER_TEST_DATABASE_URL`, **sem fallback**
para variável de produção, e o nome do banco precisa conter `test`. Cada teste
cria e derruba só o próprio schema (`mp_test_<hex>`).

## Decisões de design

| Decisão | Por quê |
|---|---|
| **asyncpg puro**, sem ORM | O valor do pacote está em SQL escrito à mão (RRF, CTE recursiva, triggers). Um ORM traria uma dependência grande para esconder exatamente isso. |
| **Python 3.11+** | Mesmo piso do restante do OrkMind (`requires-python >= 3.11`). |
| `gin(tsv)` com `tsv` **coluna gerada**, em vez de índice de expressão | Evita recalcular o tsvector a cada consulta e permite ponderar o caminho de títulos (peso A) acima do corpo (peso B). |
| `department_scope` é **`TEXT[]`** | Um documento pode ser de mais de uma área; `{}` = organização inteira. |
| `wiki_document_versions` separada | "Imutável e auditável" com `version` exige guardar as revisões em algum lugar, e esse lugar é append-only. |
| Apontador do meta-índice é **uma linha com FK**, não um array de IDs | Array de UUID não tem integridade referencial; com FK, aposentar ou apagar documento não deixa ponteiro órfão. |
| Classificação **só** em `wiki_documents` | Copiar `access_level` para os chunks aceleraria o filtro, mas criaria dois lugares capazes de discordar sobre quem pode ler. |
| Papéis, departamentos, `doc_type`, agente e canal são **dados** | Nada disso é fixado no código: `doc_type` é qualquer `snake_case`, departamentos são strings livres, `agent_id` é parâmetro. |

## Não entregue

- **Adaptador de canal corporativo.** O servidor MCP já entrega a memória a
  qualquer runtime que fale MCP, com identidade vinda do ambiente. Falta o
  adaptador que monta `UserContext` a partir de um canal multiusuário real
  (Teams, Slack), onde cada mensagem tem um usuário diferente — hoje o
  servidor MCP tem uma identidade fixa por processo.
- **Plugin nativo do OpenClaw.** O MCP cobre o caso; um plugin TypeScript
  dedicado ainda não existe.
- **Avaliação de recuperação em escala real.** A ablação acima usa 17 chunks.
  Os testes e o benchmark de escala continuam usando
  `HashingEmbeddingProvider` (determinístico, **não semântico**), de propósito:
  CI não deve depender de rede nem de chave de API.
- **Extração de PDF/DOCX.** Chega texto; o pacote não extrai.
- **Fila de ingestão durável.** É em memória (ver Ingestão).
- **Exclusão física** (ex.: pedido LGPD). Só `retire_document`. Apagar de fato
  exige desabilitar triggers conscientemente, por um DBA.
