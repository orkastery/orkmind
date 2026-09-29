# OrkMind

[English](README.md) · **Português**

[![PyPI](https://img.shields.io/pypi/v/orkmind)](https://pypi.org/project/orkmind/) [![Licença MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-blue)](LICENSE)

> **Em uma frase:** o OrkMind é memória tipada e governada para agentes de IA: as regras que
> importam sempre chegam ao modelo, a recuperação é determinística e o histórico nunca é
> reescrito.

- **Status:** 0.4.0, alfa · no PyPI como [`orkmind`](https://pypi.org/project/orkmind/) ·
  [mudanças por versão](CHANGELOG.md)
- **Prova:** o CI de todo pull request roda a suíte sem banco em Python 3.11 e 3.12, confere o
  sdist e o wheel e testa o plugin do OpenClaw.
- **Projeto:** código aberto sob a licença MIT, mantido pela [Orkastery](https://github.com/orkastery),
  contribuição por pull request ([CONTRIBUTING](CONTRIBUTING.md)).
- **Idioma:** o [README em inglês](README.md) é a entrada canônica, e esta página o espelha; parte da
  documentação e dos comentários do código está em português.

```bash
pip install "orkmind>=0.3.0"
bash scripts/setup_postgres.sh        # PostgreSQL + pgvector no Docker (ou use o seu)
export ORKMIND_DATABASE_URL="postgresql://orkmind:senha@localhost:5432/orkmind"
orkmind add --collection rule --content "Nunca rode rm -rf em produção" \
  --tags '{"skill": ["deploy"]}' --mandatory
orkmind search --tags '{"skill": ["deploy"]}'
```

## Por que existe

- Agentes guardam a memória em arquivos de instrução (`AGENTS.md`, `CLAUDE.md`, `MEMORY.md`) que
  crescem até a regra importante se perder no meio.
- A busca por embedding é probabilística: a regra de segurança que você precisa neste turno pode
  simplesmente não aparecer entre os primeiros resultados.
- **O OrkMind leva essa memória para um armazenamento tipado**, com recuperação determinística por
  tags, e as regras marcadas como `mandatory` sempre voltam quando as tags batem com o contexto.

## Conceitos principais

- **23 coleções tipadas:** rule, instruction, fact, learning, preference, decision, content, agenda,
  contacts, handoff, roadmap, files, docs, dags, tools, users, artifact, compliance, semantic_log,
  session, product, project, initiative.
- **11 dimensões de tag:** skill, agent, domain, project, situation, person, audience, editors,
  prod, proj, init. `project` continua sendo o espaço físico; `prod`, `proj` e `init` modelam o
  portfólio de negócio sem ampliar acesso.
- **Recuperação determinística:** casamento exato de tags. A busca semântica (FTS + vetor, fundidos
  com RRF) é opcional e soma, nunca substitui.
- **Injeção obrigatória:** entradas com `mandatory: true` sempre voltam quando as tags batem.
- **Orçamento de tokens:** a camada semântica respeita um orçamento configurável e carrega primeiro
  o que é crítico e obrigatório.
- **Proteção:** entradas `protected` e `priority: critical` recusam edição e remoção por agente; só
  fontes humanas autenticadas mudam essas entradas.
- **Histórico só de acréscimo:** todas as versões ficam em `memory_versions`.
- **Anti-injeção:** conteúdo suspeito é guardado, mas fica fora da injeção automática, com
  integridade por SHA-256.
- **Conflitos:** entradas obrigatórias com tags sobrepostas esperam revisão humana.
- **Camadas de contexto:** carregamento progressivo por fidelidade (essência, estrutura, fonte).
- **Snapshots:** commit, log, diff e restauração da árvore inteira da memória.
- **Criptografia em repouso:** AES-256-GCM opcional para coleções sensíveis.

## Começo rápido

```bash
pip install "orkmind>=0.3.0"

# PostgreSQL + pgvector com o script de apoio (precisa de Docker)
bash scripts/setup_postgres.sh
# ou num servidor que você já tem
createdb orkmind
psql orkmind -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Aponte o OrkMind para o banco com `ORKMIND_DATABASE_URL`, ou com `~/.orkmind/config.toml`:

```toml
[store]
backend = "pgvector"                                     # padrão
database_url = "postgresql://orkmind:senha@localhost:5432/orkmind"

[server]
log_level = "INFO"
token_budget = 4000
```

```bash
orkmind add --collection rule --content "Nunca rode rm -rf em produção" \
  --tags '{"skill": ["deploy"], "domain": ["infra"]}' --mandatory
orkmind list --collection rule
orkmind search --tags '{"skill": ["deploy"]}'
orkmind detect --text "Vamos subir as mudanças do terraform"
orkmind stats
orkmind add --collection content --content "relatório semanal" --dedupe --json   # idempotente
```

## Integrações

| Integração | Como conecta | Regras em todo turno |
| --- | --- | --- |
| Hermes | plugin MemoryProvider ([configuração](docs/hermes-setup.md)) | sim, injeção incondicional |
| OpenClaw | plugin de memória em `integrations/openclaw` | sim, injeção incondicional |
| Claude Code, Codex e qualquer cliente MCP | `python -m orkmind.mcp` ([configuração](docs/mcp-setup.md)) | sob demanda, pelas tools |
| CLI | `orkmind` | sob demanda |

O servidor MCP e o CLI nunca montam o prompt do modelo, então expõem as mesmas regras sob demanda
em vez de injetá-las. Só os dois plugins que montam o prompt garantem que a governança chegue ao
modelo em todo turno, e os dois estão cobertos pelo benchmark do [README em inglês](README.md),
gerado por `bench/run.py`.

## Armazenamento

O OrkMind separa **persistir** de **governar**. O backend guarda e devolve bytes; proteção, ACL,
versionamento e ordenação rodam numa camada acima, igual para todo backend.

| Backend | Quando usar | Custo |
| --- | --- | --- |
| `pgvector` (padrão) | Produção | Nenhum. É a referência |
| `memory` | Testes, desenvolvimento local, CI | Volátil: os dados morrem com o processo |
| `qdrant` | Você já roda Qdrant | Experimental; idempotência de melhor esforço e declarada |

Toda degradação é declarada em `StoreCapabilities` e impressa por `orkmind store info`, nunca em
silêncio. Veja o [guia de backends](docs/storage-backends/GUIA-BACKENDS.md) e a
[matriz de capacidades](docs/storage-backends/MATRIZ-BACKENDS.md).

## Gravação garantida

O que agentes e crons gravam passa primeiro por uma fila durável em disco e depois é reconciliado,
então sobrevive a uma tool ausente ou a um banco fora do ar por um momento. A idempotência usa o
`content_hash` SHA-256 com índice único, então reprocessar a fila nunca duplica memória.

```bash
export ORKMIND_API_TOKEN="<token>"
orkmind api --port 8077                           # API local e idempotente, só no loopback
python scripts/orkmind_drain.py --once            # drena a fila uma vez (cron)
python scripts/orkmind_drain.py --status          # inspeciona sem gravar
```

Detalhes em [docs/sempre-gravar.md](docs/sempre-gravar.md).

## Memory Provider nativo

`orkmind.memory_provider` é um pacote assíncrono à parte, para quem precisa de uma memória completa
e não só da camada de regras: três camadas (Core, Recall, Wiki), recuperação híbrida densa e léxica
fundida com RRF no SQL, pré-filtro determinístico por RBAC e ingestão idempotente de documentos
inteiros (Markdown com wikilinks e frontmatter, texto, CSV, PDF, DOCX). O banco recusa `UPDATE` e
`DELETE` nas tabelas de histórico, e um servidor MCP (`python -m orkmind.memory_provider.mcp`)
expõe `memory_search` e `memory_get`.

```bash
pip install "orkmind[memory-provider]>=0.3.0"
export ORKMIND_PROVIDER_DATABASE_URL=postgresql://usuario:senha@localhost:5433/memory_provider
python examples/memory_provider.py
```

Veja [docs/memory-provider/README.md](docs/memory-provider/README.md).

## Desenvolvimento

```bash
pip install -e ".[dev]"
pytest -m "not integration"      # o que o CI roda, sem banco
pytest                           # a suíte inteira; precisa de ORKMIND_TEST_DATABASE_URL com "test" no nome
```

Os testes de integração apagam o banco para onde apontam, então a guarda dos testes só aceita um
banco cujo nome o marque como banco de teste.

## Documentação

- [Referência da ontologia](docs/ontologia.md): coleções, dimensões de tag, validação
- [Guia de integração](docs/integration-guide.md): Claude Code e Hermes
- [Configuração do MCP](docs/mcp-setup.md) · [Configuração do Hermes](docs/hermes-setup.md)
- [Memória federada](docs/federation.md): armazenamento por projeto, recall compartilhado e ACL de perfis
- [Gravação garantida](docs/sempre-gravar.md): fila, drenador, API idempotente
- [Memory Provider nativo](docs/memory-provider/README.md)
- [Benchmark](bench/README.md): método, métricas e o que ele não prova
- [Changelog](CHANGELOG.md)

## Projeto

O OrkMind é código aberto sob a [licença MIT](LICENSE), mantido pela
[Orkastery](https://github.com/orkastery). Contribuições chegam por pull request, com a evidência
descrita no [CONTRIBUTING](CONTRIBUTING.md). Vulnerabilidades se reportam em privado, como diz o
[SECURITY](SECURITY.md). O [código de conduta](CODE_OF_CONDUCT.md) vale em todos os espaços do
projeto.

## Licença

[MIT](LICENSE)
