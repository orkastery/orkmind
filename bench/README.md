# Benchmark do OrkMind

Este diretorio mede uma coisa so, e mede com honestidade: **o que efetivamente
chega ao prompt do modelo** em cada integracao do OrkMind, turno a turno,
inclusive quando o backend falha.

Nao e um benchmark de qualidade de memoria semantica. Nao e um benchmark de
obediencia do modelo. A secao [O que este benchmark NAO prova](#o-que-este-benchmark-nao-prova)
delimita o alcance antes de qualquer numero ser citado.

## Como rodar

Pre-requisito: um Postgres com `pgvector` acessivel e um banco descartavel.

```bash
# Postgres com pgvector, descartavel, em uma linha
docker run --rm -d --name orkmind-bench \
  -e POSTGRES_USER=orkmind -e POSTGRES_PASSWORD=senha-de-teste \
  -e POSTGRES_DB=orkmind_bench_test \
  -p 5432:5432 pgvector/pgvector:pg16

# build do plugin OpenClaw (a rodada TS mede o dist/ compilado)
cd integrations/openclaw/memory-orkmind && npm ci && npm run build && cd -

# benchmark completo: seed -> Hermes -> OpenClaw -> baseline -> report
python bench/run.py
```

O agregado sai em `bench/results/latest.json` e o report em markdown vai para
o terminal (ou para um arquivo, com `--report-md`).

Opcoes uteis:

| Flag | Efeito |
| --- | --- |
| `--url <dsn>` | DSN do banco do bench (tambem por `ORKMIND_BENCH_DATABASE_URL`) |
| `--pular-seed` | reaproveita o banco ja semeado |
| `--dist <caminho>` | mede outro build do plugin OpenClaw |
| `--dist-pre-port <caminho>` | remede o baseline "antes" e regrava `results/baseline-pre-port.json` |
| `--sem-baseline` | report so com as rodadas atuais |
| `--atualizar-readme` | reescreve a secao de resultados do README raiz |

Cada runner tambem roda sozinho:

```bash
python bench/harness/seed.py --url postgresql://.../orkmind_bench_test
python bench/harness/run_hermes.py --saida /tmp/hermes.json
node bench/harness/run_openclaw.mjs --saida /tmp/openclaw.json
python bench/harness/metrics.py --latest bench/results/latest.json
```

## Verificacao de escala do Memory Provider nativo

`bench/memory_provider_scale.py` e independente do `run.py` acima e mede outra
coisa: se o `orkmind.memory_provider` continua correto e rapido com dezenas de
milhares de chunks, onde o planner do Postgres para de fazer Seq Scan em tudo.

```bash
export ORKMIND_PROVIDER_TEST_DATABASE_URL=postgresql://postgres:senha-de-teste@127.0.0.1:5433/memory_provider_test
python bench/memory_provider_scale.py            # 30 mil chunks, ~40 s
```

Sai com codigo != 0 se alguma destas garantias quebrar: o plano usa HNSW e GIN;
a latencia nao muda de regime a partir da 6a execucao (plano generico); o chunk
com o identificador exato e o top-1; nada fora do escopo de quem pergunta
aparece; consulta so de palavras onipresentes tem teto de custo.

O guarda de regime foi validado por mutacao: removendo `force_custom_plan` de
`services/retrieval.py` o script falha (mediana ~11 ms -> ~47 ms). A primeira
versao desse guarda tinha um piso absoluto de 50 ms e APROVOU o mutante; o
criterio atual e razao entre medianas com folga de 3 ms.

Nao prova qualidade de recuperacao: os vetores sao ruido e o vocabulario tem 16
palavras, de proposito o pior caso para a perna lexica. So aceita banco com
`test` no nome e so cria/derruba o schema `mp_test_scale_bench`.

## A guarda de banco (leia antes de apontar qualquer DSN)

O seed **apaga** todas as linhas do banco para onde aponta. Em 29/08/2026 uma
execucao sem guarda destruiu 18 entries de producao. Desde entao existe uma
guarda em `tests/conftest.py`, e `bench/harness/seed.py` a **reusa por
import**, nao reimplementa: duas copias da mesma guarda divergem, e a copia
que diverge e a que apaga producao.

Consequencia pratica: o nome do banco precisa ter marcador de teste
(`test`, `_ci`, `sandbox`). Sem isso, nada roda e o exit code e `2`, distinto
de `1` para que o orquestrador diferencie "guarda recusou" de "falhou".

```
$ python bench/run.py --url postgresql://orkmind:senha-de-teste@localhost:5432/orkmind
RECUSADO: o banco 'orkmind' nao tem marcador de teste no nome (test, _ci, sandbox).
$ echo $?
2
```

O benchmark tambem e hermetico quanto a rede: o embedder e fake e o endpoint
de embedding aponta para um endereco morto em loopback, entao nenhuma rodada
faz chamada externa nem gera custo.

## Metricas: definicao operacional

Toda fracao e reportada como `num/den (taxa)`. Percentual sem denominador
esconde o tamanho da amostra e por isso nao aparece aqui.

| Metrica | O que mede | Definicao operacional |
| --- | --- | --- |
| **M1** | injecao constitucional | fracao dos 20 turnos em que **todas** as regras `priority = critical` aparecem **literalmente** no prompt do turno |
| **M2** | regras mandatorias | idem para `mandatory = true`, mais a contagem de regras omitidas por budget (lidas do proprio bloco) |
| **M3** | fail-safe | cenarios de falha do backend **apos** uma carga bem-sucedida devem produzir o alerta; os casos de controle **nao** devem. O acerto conta os dois sentidos, senao um plugin que alerta sempre marcaria 100% |
| **M4** | recuperacao | `recall@5` e `precision@5` sobre `thematic.json`, mais `sanidade_pipeline`: consultar pelo texto exato de uma memoria tem que traze-la no top-5 |
| **M5** | custo | media de chars injetados por turno e latencia p50 do caminho de injecao |
| **M6** | diluicao | fatia do prompt do turno ocupada pelo bloco de regras nos turnos 1, 10, 20 e 40 de uma sessao longa sintetica |
| **M7** | fidelidade cruzada | o bloco de regras a partir de `## REGRAS MANDATORIAS` e literalmente identico entre Hermes e OpenClaw? Quantos chars divergem? |

Sobre M3: "apos uma carga bem-sucedida" nao e detalhe. O alerta so faz sentido
quando o agente **ja soube** que havia regras e as perdeu. Falhar antes de
qualquer carga e o estado inicial, nao uma degradacao, e alertar ali seria
ruido. Por isso o controle vale ponto tanto quanto os cenarios de falha.

Sobre M4: com embedder fake, `recall@5` e `precision@5` **nao medem qualidade
semantica**. O embedder e um hash: textos de mesmo sentido geram vetores nao
relacionados, entao o numero honesto e proximo de zero e isso e esperado. O
que este ciclo valida e a ESTRUTURA, medida por `sanidade_pipeline`. Se esse
numero nao for `1.00`, o pipeline (FTS, vetor, RRF, filtros) esta quebrado e
nenhuma outra metrica de recuperacao significa nada. Numeros com embedder real
ficam para o proximo ciclo.

## O embedder fake

Deterministico, sem rede, sem custo, e **identico em Python e Node**:

```
para i = 0, 1, 2, ... ate juntar `dim` valores:
    bloco = sha256(utf8(texto + ":" + i))
    cada byte b do bloco vira (b / 255) * 2 - 1
corta em `dim` valores e normaliza L2
```

`constitutional.json` carrega em `vetores_de_referencia` amostras gravadas do
algoritmo, o que permite verificar a paridade entre as duas implementacoes sem
rodar as duas ao mesmo tempo.

Determinismo e o ponto: duas rodadas seguidas produzem o mesmo JSON fora do
timestamp e da latencia. Uma metrica que muda entre rodadas nao serve para
tabela antes/depois.

## Datasets

| Arquivo | Conteudo | Schema |
| --- | --- | --- |
| `constitutional.json` | 5 regras (N1 criticas e N2 mandatorias, inclusive o texto anti-destruicao) e 20 turnos de usuario, dos quais 16 **sem** relacao semantica com as regras | `regras[]` com `id, content, collection, priority, mandatory, protected, tags, nivel`; `turnos[]` com `texto, relacionado_as_regras`; `vetores_de_referencia` |
| `thematic.json` | 12 memorias e 4 sessoes com tema declarado e gabarito de relevancia | `memorias[]` com `id, content, collection, tags`; `sessoes[]` com `escopo, turnos, relevantes` |
| `distractors.json` | 64 memorias irrelevantes de alto volume | `memorias[]` com `id, content, collection, priority, mandatory, tags` |

Os 16 turnos sem relacao semantica sao o caso dificil de proposito: e neles
que recuperacao por similaridade nao traz a regra, e so injecao incondicional
garante que ela chegue. Um dataset em que a regra e sempre semanticamente
proxima do turno mediria o proprio dataset, nao o produto.

Os distratores existem pelo mesmo motivo: sem ruido na base, "a regra
apareceu no prompt" e barato demais.

## Baseline retroativo

`results/baseline-pre-port.json` e o "zero": a mesma medicao, com o mesmo
runner e o mesmo banco, contra o `dist/` do commit **anterior** ao port da
governanca para o OpenClaw. O arquivo registra o `dist/` medido e o sha do
worktree, nao o do HEAD, senao a tabela antes/depois atribuiria os dois
numeros ao mesmo commit e perderia o valor probatorio.

Para regerar:

```bash
git worktree add /tmp/orkmind-preport <sha-pre-port>
cd /tmp/orkmind-preport/integrations/openclaw/memory-orkmind && npm ci && npm run build && cd -
python bench/run.py --dist-pre-port /tmp/orkmind-preport/integrations/openclaw/memory-orkmind/dist
git worktree remove /tmp/orkmind-preport
```

A tabela antes/depois compara **o mesmo runner consigo mesmo**. Comparar
Hermes com OpenClaw pre-port seria comparar dois programas diferentes e
creditar a diferenca ao port.

## Higiene de repo

- `bench/` esta **fora** de `testpaths` (`pyproject.toml` fixa
  `testpaths = ["tests"]`). `pytest tests/` nao coleta nada daqui.
- De `results/`, so `latest.json` e `baseline-pre-port.json` sao versionados;
  o resto e ignorado.
- Sem dependencia nova: o harness usa stdlib, o proprio pacote `orkmind` e o
  `pg` que ja vive no `node_modules` do plugin.

## O que este benchmark NAO prova

- **Nao prova qualidade de recuperacao semantica.** O embedder e fake. M4
  valida a mecanica do pipeline, nao o sentido dos resultados.
- **Nao prova obediencia do modelo.** Mede o que chega ao prompt, nao o que o
  modelo faz depois. Injetar a regra e condicao necessaria, nao suficiente.
- **Nao e um benchmark de memoria de longo prazo.** LoCoMo e similares estao
  no roadmap, nao aqui.
- **Nao mede sessao viva.** Roda headless, com banco descartavel e datasets
  versionados. Verificacao em sessao real e outro procedimento.
- **Nao mede as integracoes MCP e CLI.** Elas nao constroem prompt, entao
  injecao incondicional nao se aplica a elas; expoem as mesmas regras sob
  demanda. A matriz de suporte no README raiz declara isso.
