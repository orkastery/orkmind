# Desenvolvimento local

> **Em uma frase:** clone, crie um venv, instale o pacote em modo editável com os extras do CI, e só suba o PostgreSQL com pgvector quando for rodar os testes de integração.

## Requisitos

- Python na faixa de `requires-python` do [pyproject.toml](../../pyproject.toml). O CI testa nas versões da matriz de [ci.yml](../../.github/workflows/ci.yml).
- git.
- Para os testes de integração, e só para eles: PostgreSQL com a extensão pgvector. O jeito mais curto é o Docker, pelo [setup_postgres.sh](../../scripts/setup_postgres.sh).
- Para o plugin do OpenClaw, e só para ele: Node.js na versão de [ci.yml](../../.github/workflows/ci.yml).

## Clonar e instalar

<!-- checagem: citado -->

```bash
git clone https://github.com/orkastery/orkmind.git
cd orkmind
```

Antes de criar o venv, confira o Python contra a faixa pedida:

```bash
python --version
python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['requires-python'])"
```

Se o segundo comando falhar no `import tomllib`, o seu Python é mais antigo que o mínimo: troque de Python antes de seguir.

<!-- checagem: citado -->

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,memory-provider,documents,qdrant,encryption]"
```

- Os comandos destes guias rodam da raiz do checkout, com o venv ativo.
- `-e` instala em modo editável: a mudança em `src/orkmind/` vale na hora, sem reinstalar.
- É o mesmo conjunto de extras do passo `instalar` de [ci.yml](../../.github/workflows/ci.yml). Com menos extras, os testes do que faltou se pulam, e o CI roda os que você pulou.

## Os extras

| Extra | O que traz | Quem precisa |
| --- | --- | --- |
| `dev` | pytest, pytest-asyncio, ruff e mypy | Quem contribui |
| `memory-provider` | asyncpg, para `orkmind.memory_provider` | Quem mexe no Memory Provider |
| `documents` | Leitura de frontmatter YAML, PDF e DOCX na ingestão | Quem mexe na ingestão de documento |
| `qdrant` | O cliente do backend Qdrant | Quem mexe no backend Qdrant |
| `encryption` | Criptografia em repouso das coleções sensíveis | Quem mexe em `orkmind encrypt` e `orkmind decrypt` |
| `hermes` | O agente Hermes, para o plugin | Quem roda o plugin do Hermes de verdade |

Para ver os extras de hoje:

```bash
python -c "import tomllib; print(', '.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['optional-dependencies']))"
```

## Rodar o CLI do checkout

```bash
orkmind --version
orkmind --help
```

`--help` é a fonte de verdade dos comandos; cada comando tem o seu, como `orkmind store --help`.

Os comandos que abrem o store usam o backend de `ORKMIND_STORE_BACKEND` ou de `[store].backend` no `~/.orkmind/config.toml`, que por padrão é o `pgvector`, com a DSN de `ORKMIND_DATABASE_URL` ou do mesmo arquivo. Sem DSN, eles saem 1, com um traceback que termina dizendo o que falta. `orkmind store info`, que o [modelo de bug](../../.github/ISSUE_TEMPLATE/bug_report.yml) pede, mostra o backend configurado e as capacidades que ele declara:

<!-- checagem: citado -->

```bash
orkmind store info
```

Para ver o CLI funcionando sem configurar nada, o backend `memory`, que não guarda nada depois do processo:

```bash
ORKMIND_STORE_BACKEND=memory orkmind store info
```

## PostgreSQL com pgvector

Só para os testes de integração e para usar o OrkMind de verdade. Com Docker:

<!-- checagem: citado -->

```bash
bash scripts/setup_postgres.sh
docker exec orkmind-postgres createdb -U orkmind orkmind_test
docker exec orkmind-postgres createdb -U orkmind memory_provider_test
```

- O script sobe o contêiner `orkmind-postgres` com pgvector e imprime a DSN do banco `orkmind`.
- Os dois `createdb` criam bancos de teste separados. A suíte de integração **apaga** o banco para onde aponta, e só aceita um banco cujo nome deixe claro que é de teste.
- A extensão `vector` é criada pelo próprio OrkMind ao iniciar o schema.
- Como apontar os testes para esses bancos está em [testes](testes.md#o-que-precisa-de-banco).

## Onde fica cada coisa

| Pasta | O que tem |
| --- | --- |
| [src/orkmind](../../src/orkmind) | O pacote: `core`, `store` (backends e `GovernedStore`), `cli`, `mcp`, `api`, `memory_provider`, `guardrails`, `embeddings` |
| [tests](../../tests) | `unit/` sem serviço, `contract/` por backend e `integration/` com banco |
| [integrations](../../integrations) | O plugin do Hermes e o plugin do OpenClaw, este um pacote npm à parte |
| [migrations](../../migrations) | As migrações SQL do schema do núcleo |
| [scripts](../../scripts) | Apoio: PostgreSQL local, seeds, drenador do spool, sincronização dos plugins |
| [examples](../../examples) | Exemplos de uso e de configuração |
| [bench](../../bench) | O benchmark, com o método e o que ele não prova |
| [docs](..) | A documentação técnica e estes guias |

## Próximo passo

[Testes](testes.md): o que rodar antes do PR, e o que precisa de banco.

Índice dos guias: [CONTRIBUTING](../../CONTRIBUTING.md).
