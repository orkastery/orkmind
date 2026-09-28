# Matriz de backends do OrkMind

> Artefato versionado do ciclo "storage plugavel" (F3.9).
> Fonte dos valores: `StoreCapabilities` declarado por cada adapter, verificado
> pela suite de contrato (`tests/contract/test_store_contract.py`,
> `TestCapabilitiesContract`). Se esta tabela divergir do codigo, o teste quebra.

O OrkMind tem tres backends nesta leva (DP-1). Nenhum quarto entra sem passar
pela suite de contrato inteira.

Ver tambem: `GUIA-BACKENDS.md` (como configurar cada um e como migrar).

---

## 1. Matriz de capacidades

| Campo | `pgvector` (default) | `memory` | `qdrant` (**experimental**) |
|---|---|---|---|
| `backend` | `pgvector` | `memory` | `qdrant` |
| `vector_search` | sim | sim (cosseno em numpy) | sim (HNSW nativo) |
| `accepts_external_vectors` | sim | sim | sim |
| `stores_embedding` | sim | sim | sim |
| `native_text_semantic` | nao | nao | nao |
| `text_search` | `ranked` | `match` | `match` |
| `tag_filter` | sim | sim | sim |
| `unique_content_hash` | **sim** | sim (dict, processo unico) | **nao** |
| `versioning` | sim | sim | sim (pontos `kind="version"`) |
| `snapshots` | sim | sim | sim (pontos `kind="snapshot*"`) |
| `parent_id` | sim | sim | sim |
| `ttl_gc` | sim | sim | sim |
| `durable` | sim | **nao** | sim |
| `native_acl_filter` | **sim** | nao | nao |
| `native_constitutional_order` | **sim** | nao | nao |
| `backfill` | sim | sim | sim |

`orkmind store info` imprime esses valores para o backend ativo, junto com os
avisos que decorrem deles.

---

## 2. Requisito de produto x backend

O que o produto promete continua valendo em todos os tres, porque quem cumpre a
promessa e o `GovernedStore`, nao o adapter (DP-4).

| Requisito | `pgvector` | `memory` | `qdrant` | Quem garante |
|---|---|---|---|---|
| Regra `mandatory` sempre injetada (I6) | sim | sim | sim | `GovernedStore` + over-fetch DD-5 |
| Entry `protected` intocavel por agente (I1) | sim | sim | sim | `store/governance.py` |
| Protecao D2 antes da ACL de escrita (I2) | sim | sim | sim | `store/governance.py` |
| ACL de leitura por autor, audience e grupo (I3) | sim | sim | sim | `GovernedStore` + `ProfileStore` |
| Entry expirada nunca aparece (I4) | sim | sim | sim | camada + higiene do adapter |
| `injection_risk` fora de `search_by_tags` (I5) | sim | sim | sim | `GovernedStore` |
| Versao anterior arquivada, `version + 1` (I7) | sim | sim | sim | adapter, invariante conferido pela camada |
| `snapshot_restore` com auto-backup (I8) | sim | sim | sim | adapter |
| Ordem de `search_by_tags` total e identica (I9) | sim | sim | sim | `ordenar_constitucional` (DD-3) |
| Degradacao sempre declarada (I10) | sim | sim | sim | `StoreCapabilities.avisos()` |
| Backfill de embeddings pelo contrato | sim | sim | sim | os tres adapters |
| Export e import entre backends | sim | sim | sim | `store/transfer.py` |

---

## 3. Divergencias declaradas

Nenhuma delas e defeito: sao consequencias do motor escolhido, declaradas em
`capabilities` e visiveis em `orkmind store info`. O que seria defeito e
qualquer uma delas acontecer em silencio (R0.3).

### 3.1 Qualidade do full-text (`text_search`)

- `pgvector` faz full-text com ranking (`ts_rank` sobre `tsvector`).
- `memory` e `qdrant` fazem casamento de token, **sem ranking**.

Efeito pratico: o lado FTS do RRF (`core/search.py`) continua funcionando,
porque o RRF so usa posicao, mas a ordem dentro do lado textual e menos
informativa nos dois backends de `match`. Consultas com varios termos casam por
conjuncao (todos os tokens precisam aparecer), sem stemming.

**Divida relacionada:** D-8, o dicionario de full-text do Postgres esta fixo em
`'english'` num produto pt-BR. Independente desta demanda.

### 3.2 Idempotencia dura (`unique_content_hash`)

- `pgvector`: indice unico parcial `(collection, content_hash)`. O banco recusa
  a duplicata.
- `memory`: dict indexado; recusa a duplicata dentro do processo.
- `qdrant`: **nao ha indice unico.** O `initialize()` emite `logger.warning` e o
  `GovernedStore` consulta por `content_hash` antes de gravar (best-effort,
  DP-6).

Consequencia honesta: em Qdrant, duas gravacoes concorrentes do mesmo conteudo
podem produzir duas entries. Nenhum teste afirma paridade; o teste de duplicata
vira skip com a capability nomeada no relatorio.

### 3.3 Durabilidade (`durable`)

- `memory` nao sobrevive ao fim do processo. Existe para teste, desenvolvimento
  e para dar cobertura de contrato em qualquer maquina. **Nunca pode virar
  default**, e o factory avisa toda vez que ele e selecionado.

### 3.4 Onde vivem os perfis (DP-A)

- `pgvector`: `PostgresProfileStore`, tabela `profiles`. Nada muda em producao.
- `memory` e `qdrant`: `EntryProfileStore`, perfis viram entries da colecao
  `users`, marcadas com `metadata["orkmind_profile"] = True`.

Efeito observavel: nesses dois backends, `orkmind list --collection users` e
`orkmind stats` contam perfis junto com memorias sobre pessoas. A marca existe
para que um filtro de CLI possa ser adicionado depois sem quebrar nada.

**Divida relacionada:** D-3, os perfis de producao permanecem na tabela
`profiles` e nao migram nesta leva.

### 3.5 Over-fetch de candidatos (DD-5)

- `pgvector` declara `native_acl_filter=True` e
  `native_constitutional_order=True`, entao **nao ha over-fetch**: o `limit`
  enviado ao adapter e o `limit` pedido, e o SQL emitido e o mesmo de antes do
  ciclo. E a garantia mecanica de nao-regressao do default (DP-9), com teste de
  espiao em `tests/unit/test_governed_store.py`.
- `memory` e `qdrant` recebem uma janela maior de candidatos
  (`min(max(limit * 4, limit + 50), teto)`, teto default 500, configuravel em
  `[store.options].candidate_overfetch`). Se a janela satura, sai um
  **warning de saturacao**: uma entry `mandatory` pode ter ficado de fora.

### 3.6 O que o export e o import nao levam

O formato portavel leva **entries**, com embedding. Versoes anteriores e
snapshots podem ser incluidos no dump (`--include-versions`,
`--include-snapshots`), mas **nao sao reinjetados no destino**: o contrato
`MemoryStore` nao tem caminho para escrever historico. Isso e contado, nomeado e
exige `--accept-loss`.

---

## 4. Marcadores de teste por backend

| Backend | Roda quando | Variaveis | Marcador pytest |
|---|---|---|---|
| `memory` | sempre, sem servico | nenhuma | `memory` |
| `pgvector` | ha banco de teste | `ORKMIND_TEST_DATABASE_URL` (ou `ORKMIND_DATABASE_URL` com marcador no nome) | `pgvector` |
| `qdrant` | ha instancia de teste | `ORKMIND_TEST_QDRANT_URL` **e** `ORKMIND_TEST_QDRANT_PREFIX` com marcador | `qdrant` |

Regra de seguranca que vale para backend novo (R0.1): **nao ha fallback a partir
de variavel de producao**. `ORKMIND_QDRANT_URL` nunca vira alvo de teste, e
coincidencia entre o alvo de teste e a instancia de producao levanta erro em vez
de pular. Detalhe em `tests/conftest.py` e `tests/unit/test_guarda_backends.py`.

**Divida relacionada:** D-4, o Qdrant ainda nao e um servico do CI; o skip la e
ruidoso, nunca silencioso.
