# Guia de backends do OrkMind

> Como escolher, configurar e trocar o backend de storage.
> Matriz completa de capacidades e divergencias: `MATRIZ-BACKENDS.md`.

O OrkMind separa **persistir** de **governar**. O backend guarda e devolve
bytes; a governanca (protecao de entries, ACL, versionamento, ordenacao
constitucional) roda numa camada acima, igual para todos. Trocar de backend
muda onde o dado mora, nunca o que o produto promete.

---

## 1. Escolha rapida

| Situacao | Backend |
|---|---|
| Producao, instalacao existente, nao quero mexer em nada | `pgvector` (default) |
| Teste, desenvolvimento local, CI sem servico externo | `memory` |
| Ja tenho Qdrant e quero busca vetorial dedicada | `qdrant` (**experimental**) |

> **AVISO (F4 Storage, 2026-09-01): o backend `qdrant` e EXPERIMENTAL e NAO
> habilita em producao.** Dois criticos seguem abertos: **S1** - identidade
> forjavel pela API de memoria (perfis sao entries publicas de `users` sem
> `protected`, permitindo escalada de privilegio; `pgvector` e imune) - e
> **S4** - o `garbage_collect` nao apaga pontos de TTL (`kind="version"`) no
> Qdrant, deixando o conteudo legivel para sempre. Por enquanto use
> `pgvector` em producao.

Uma instalacao que nunca editou o `config.toml` continua em `pgvector` sem
nenhuma alteracao (DP-9).

---

## 2. Configuracao

A precedencia e a de sempre: **variavel de ambiente > arquivo de config >
default**.

### 2.1 `pgvector` (default)

```toml
[store]
backend = "pgvector"                                # opcional, e o default
database_url = "postgresql://localhost/orkmind"
```

Equivalente por ambiente:

```bash
export ORKMIND_DATABASE_URL="postgresql://localhost/orkmind"
```

O alias `postgres` aponta para o mesmo builder, por compatibilidade com
documentacao interna antiga.

**Ganha:** full-text com ranking, indice unico de idempotencia, filtro de ACL e
ordenacao constitucional dentro do proprio motor (sem over-fetch), durabilidade.
**Perde:** nada. E a referencia.

### 2.2 `memory`

```bash
export ORKMIND_STORE_BACKEND=memory
```

**Ganha:** zero dependencia de servico, ideal para teste e desenvolvimento.
**Perde:** durabilidade. Os dados morrem com o processo, e o OrkMind avisa isso
toda vez que este backend e selecionado. Full-text sem ranking.

Nunca use `memory` em producao. Ele nao pode virar default.

### 2.3 `qdrant` (**experimental**)

> **NAO usavel em producao.** S1 (identidade forjavel por perfis publicos de
> `users`) e S4 (TTL nao apagado) estao abertos; ver aviso na secao 1. Use o Qdrant apenas
> em laboratorio ate os dois criticos serem resolvidos e re-avaliados.

Instale o extra:

```bash
pip install "orkmind[qdrant]"
```

```toml
[store]
backend = "qdrant"

[store.options]
url = "http://localhost:6333"
collection = "orkmind_memories"     # opcional
api_key_env = "QDRANT_API_KEY"      # opcional: nome da variavel, nao a chave
candidate_overfetch = 500           # opcional
timeout_s = 30                      # opcional
```

Equivalente por ambiente (JSON, merge raso sobre `[store.options]`):

```bash
export ORKMIND_STORE_BACKEND=qdrant
export ORKMIND_STORE_OPTIONS='{"url": "http://localhost:6333"}'
```

`ORKMIND_STORE_OPTIONS` com JSON invalido falha alto, citando a variavel:
config silenciosamente ignorada e a origem classica de "o backend nao era o que
eu pensava".

**Ganha:** busca vetorial HNSW dedicada, escala horizontal do proprio Qdrant.
**Perde:** indice unico de `(collection, content_hash)`, entao a idempotencia
vira best-effort e o `initialize()` avisa (DP-6). Full-text sem ranking. Perfis
passam a viver como entries da colecao `users` (DP-A).

Sem `qdrant-client` instalado, o erro diz exatamente o que rodar.

---

## 3. Ver o que o backend ativo sabe fazer

```bash
orkmind store info
```

Imprime o backend ativo, todas as `capabilities` e os avisos ativos. E o
caminho oficial para conferir uma degradacao antes de investigar um sintoma:
busca semantica vazia, duplicata que passou, memoria que sumiu apos reiniciar.

---

## 4. Migrar de um backend para outro

A migracao **nunca remove a origem**. Nao existe flag de "mover".

### 4.1 Exportar

```bash
# com o backend de ORIGEM ativo
orkmind store export --out dump.jsonl
```

Opcoes: `--collection C`, `--include-versions`, `--include-snapshots`.

O export cria um snapshot rotulado `auto-export` na origem, que serve de trilha
de auditoria do que foi lido. Nenhuma entry e criada, alterada ou removida, e o
relatorio final confirma a contagem antes e depois.

### 4.2 Conferir antes de escrever

```bash
# com o backend de DESTINO ativo
orkmind store import --in dump.jsonl --dry-run
```

O relatorio diz quantas entries seriam importadas, quantas colidem por `id` ou
por `content_hash`, quantas ficariam sem embedding e o que nao atravessa.

### 4.3 Importar

```bash
orkmind store import --in dump.jsonl
```

O que acontece, nesta ordem:

1. O manifesto e validado. Arquivo sem manifesto do OrkMind e recusado.
2. A perda e calculada. Se houver, o import **aborta** pedindo `--accept-loss`.
3. Um snapshot `auto-backup-pre-import` e criado no destino.
4. As entries sao gravadas. Conflito nao sobrescreve: o default e `skip`
   (use `--on-conflict fail` para abortar no primeiro).
5. A verificacao pos-import roda sempre: contagem por colecao e reconciliacao
   de `mandatory` entry a entry. **Divergencia de `mandatory` e erro**, porque
   uma regra obrigatoria que nao atravessou e governanca perdida.

### 4.4 O que nao atravessa

Versoes anteriores e snapshots podem ser incluidos no dump, mas nao sao
reinjetados no destino: o contrato nao tem caminho para escrever historico.
Isso e contado, nomeado no relatorio e exige `--accept-loss`.

Se voce precisa do historico, mantenha a origem viva. Ela nunca e apagada por
este fluxo.

---

## 5. Rodar os testes com cada backend

```bash
# memory: sempre roda, sem servico
pytest tests/contract -k memory -rs

# pgvector: exige banco DESCARTAVEL
ORKMIND_TEST_DATABASE_URL=postgresql://localhost/orkmind_test pytest tests/contract -k pgvector -rs

# qdrant: exige instancia de teste E prefixo com marcador
ORKMIND_TEST_QDRANT_URL=http://localhost:7333 \
ORKMIND_TEST_QDRANT_PREFIX=orkmind_test_ \
pytest tests/contract -k qdrant -rs
```

Use `-rs` sempre: skip sem razao e proibido nesta suite, e a razao sempre nomeia
a capability ou a variavel que faltou.

### Guarda de seguranca (leia antes de apontar uma variavel)

Em 29/08/2026 uma execucao de `pytest` apontada para a base de producao destruiu
18 entries reais. As fixtures **apagam** o alvo. Por isso:

- `pgvector` so aceita banco cujo nome contenha `test`, `_ci` ou `sandbox`.
- `qdrant` exige **as duas** variaveis `ORKMIND_TEST_QDRANT_*`, com marcador no
  prefixo. Nao ha fallback a partir de `ORKMIND_QDRANT_URL`, e apontar o teste
  para a mesma instancia declarada em producao levanta erro em vez de pular.
- A limpeza do Qdrant nunca apaga tudo: ela enumera e apaga somente colecoes
  cujo nome comeca pelo prefixo aprovado.
- `memory` nao precisa de variavel nenhuma: o alvo e um dict do proprio
  processo.

---

## 6. Escrever um backend novo

Nao ha quarto backend nesta leva (DP-1), mas se houver um dia:

1. Implemente `MemoryStore` em `src/orkmind/store/<nome>_adapter.py`.
   **Persistencia pura.** Se o adapter precisar saber o que e `protected`,
   `mandatory`, `author_id`, `audience`, `editors` ou `injection_risk` para
   **decidir** alguma coisa, o desenho esta errado.
2. Declare `capabilities` com honestidade. Um campo `True` que o backend nao
   cumpre e pego pelo teste de equivalencia, nao por revisao.
3. Registre um builder lazy em `store/factory.py`.
4. Registre o backend em `tests/contract/backends.py`, com alvo, limpeza (que
   chama `assert_destrutivo_permitido` na primeira linha) e capabilities
   esperadas.
5. Rode a suite de contrato inteira. Skip so por capability declarada, sempre
   com a razao impressa.
