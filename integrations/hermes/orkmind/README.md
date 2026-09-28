# OrkMind - Plugin de Memoria Semantica para Hermes

Camada de memoria **semantica e estruturada** para o Hermes, baseada em
ontologia tipada com busca deterministica por tags e semantica por texto.

## Por que OrkMind?

O OrkMind adota uma filosofia fundamentalmente diferente de providers
baseados em hierarquia de arquivos ou URIs de filesystem. Em vez de
organizar conhecimento como diretorios e documentos numa arvore, o
OrkMind modela memoria como **entradas semanticas tipadas** dentro de
uma ontologia formal:

| Caracteristica | OrkMind | Abordagens baseadas em filesystem |
|---|---|---|
| **Modelo de dados** | Ontologia tipada (16 colecoes + 5 dimensoes de tag) | Hierarquia de diretorios/URIs |
| **Busca** | Deterministica (tags exatas) + semantica (texto) | Semantica fuzzy sobre conteudo |
| **Governanca** | DAG de decisoes, regras mandatorias, protecao critica | Permissoes de arquivo |
| **Contexto** | Camadas E1/E2/E3 com progressive loading | Niveis de detalhe (L0/L1/L2) |
| **Seguranca** | Anti-injection, encryption seletiva, protecao de regras | Controle de acesso por URI |
| **Versionamento** | Append-only + garbage collection | Dependente do backend |

### Diferenciais tecnicos

- **16 colecoes tipadas**: rule, instruction, fact, learning, preference,
  decision, content, agenda, contacts, handoff, roadmap, files, docs,
  dags, tools, users - cada uma com regras de validacao proprias
- **5 dimensoes de tag**: skill, agent, domain, project, situation -
  busca deterministica EXATA, nao fuzzy
- **Camadas E1/E2/E3**: Essencia (~100 tokens), Estrutura (~2k tokens),
  Fonte completa - progressive loading inteligente
- **Regras mandatorias**: injecao automatica no contexto, com protecao
  de regras criticas (so humano autenticado cria/edita criticas)
- **Anti-injection**: deteccao automatica de tentativas de injecao em
  conteudo de memoria
- **Encryption seletiva**: colecoes sensiveis podem ser encriptadas em
  repouso

## Pre-requisitos

### PostgreSQL + pgvector

O OrkMind requer PostgreSQL com a extensao pgvector. O jeito mais
simples e usar o container oficial:

```bash
docker run -d \
  --name orkmind-postgres \
  -e POSTGRES_USER=orkmind \
  -e POSTGRES_PASSWORD=senha-de-teste \
  -e POSTGRES_DB=orkmind \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

### Pacote OrkMind

O pacote orkmind deve estar instalado no venv do Hermes:

```bash
cd ~/OrkMind
pip install -e .
```

## Configuracao

### 1. Variavel de ambiente (recomendado)

Adicione ao `.env` do seu perfil Hermes:

```
ORKMIND_DATABASE_URL=postgresql://orkmind:senha-de-teste@localhost:5432/orkmind
```

### 2. Arquivo config.toml

Alternativamente, crie `~/.orkmind/config.toml`:

```toml
[store]
database_url = "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind"

[server]
token_budget = 4000
log_level = "INFO"
```

### 3. Setup interativo

```bash
hermes memory setup orkmind
```

## Ativacao

Em `~/.hermes/config.yaml`:

```yaml
memory:
  provider: orkmind
```

## Ferramentas disponiveis

O plugin expoe 4 ferramentas para o agente:

### orkmind_recall

Recupera memorias relevantes combinando busca deterministica por tags
com busca semantica por texto. Suporta progressive loading E1/E2/E3.

```json
{
  "context": "estou trabalhando no parser python do hermes",
  "tags": {"domain": ["python"], "project": ["hermes"]},
  "token_budget": 4000
}
```

### orkmind_search

Busca textual livre nas memorias, filtravel por colecao.

```json
{
  "query": "configuracao do gateway telegram",
  "collection": "docs",
  "limit": 5
}
```

### orkmind_store

Armazena nova memoria com classificacao ontologica. O agente so pode
criar memorias soft (source=agent, mandatory=false).

```json
{
  "content": "Usuario prefere commits em portugues brasileiro",
  "collection": "preference",
  "tags": {"domain": ["git"]},
  "priority": "medium"
}
```

#### Gravacao garantida (desde a versao 1.1.0)

A tool estagia o conteudo numa fila de saida em disco **antes** de tentar
gravar. Assim nada se perde quando o backend esta fora ou quando um cron
roda sem as tools de execucao no toolset.

A resposta distingue dois desfechos:

```jsonc
// gravou: o id foi confirmado pelo backend
{"status": "armazenado", "id": "<entry_id real>", "content_hash": "..."}

// nao confirmou: conteudo seguro na fila, id ainda nao existe
{"status": "pendente", "id": null, "spool_item": "...", "content_hash": "..."}
```

Em `status: pendente` o agente **nunca** deve presumir nem inventar um
entry_id. O conteudo esta seguro e sera gravado pelo drainer
(`scripts/orkmind_drain.py` no repositorio OrkMind), que e quem obtem o
id real do backend.

A raiz da fila e configuravel por `ORKMIND_SPOOL_DIR`
(default `~/.hermes/orkmind-spool`). Detalhes em `docs/sempre-gravar.md`.

### orkmind_rules

Consulta regras mandatorias ativas, opcionalmente filtradas por tags.

```json
{
  "tags": {"project": ["hermes"]}
}
```

## Seguranca e governanca

### Protecao de regras criticas

O OrkMind implementa um modelo de governanca onde **regras mandatorias
criticas so podem ser criadas por humano autenticado**. O agente pode
armazenar fatos, preferencias, aprendizados e conteudo, mas NUNCA criar
regras ou instrucoes mandatorias automaticamente.

Tentativas do agente de criar memorias com `mandatory=true` ou
`priority=critical` sao rejeitadas pelo plugin.

### Extracao automatica (on_session_end)

Ao final de cada sessao, o plugin extrai automaticamente memorias das
mensagens do usuario, mas APENAS para colecoes soft:

- **fact** - fatos declarados pelo usuario
- **preference** - preferencias explicitas
- **learning** - aprendizados da sessao
- **content** - conteudo relevante

A extracao e conservadora: so armazena informacoes claras, explicitas
e factuais. Nunca infere informacoes implicitas.

### Anti-injection

O backend OrkMind detecta automaticamente tentativas de injecao em
conteudo de memoria, marcando entradas suspeitas sem bloquea-las
(append-only, nunca perde dados).

## Arquitetura

```
Hermes Agent
    |
    +-- OrkMindHermesProvider (este plugin)
    |       |-- system_prompt_block()  -> instrucoes para o agente
    |       |-- prefetch(query)        -> recall de contexto por turno
    |       |-- handle_tool_call()     -> 4 ferramentas de memoria
    |       |-- on_session_end()       -> extracao automatica soft
    |       |
    |       +-- OrkMindMemoryProvider (backend, pacote orkmind)
    |               |-- recall()       -> busca por contexto + tags
    |               |-- search()       -> busca textual
    |               |-- store_memory() -> armazenamento validado
    |               |-- get_rules()    -> regras mandatorias
    |               |
    |               +-- SemanticLayer
    |                       |-- ontologia, validacao, layers E1/E2/E3
    |                       |-- deteccao de contexto, anti-injection
    |                       +-- MemoryStore (PostgreSQL + pgvector)
```

## Licenca

Parte do ecossistema OrkMind.
