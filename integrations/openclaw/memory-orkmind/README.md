# memory-orkmind

Plugin de memoria de primeira classe para OpenClaw, usando PostgreSQL + pgvector como backend de busca vetorial semantica.

Substitui o plugin de memoria padrao (memory-core ou memory-lancedb), ocupando o slot oficial `plugins.slots.memory`.

## Instalacao

```bash
cd integrations/openclaw/memory-orkmind
npm install
npm run build
openclaw plugins install .
```

## Configuracao

No `openclaw.json`:

```json5
{
  plugins: {
    slots: {
      memory: "memory-orkmind"
    },
    entries: {
      "memory-orkmind": {
        enabled: true,
        hooks: {
          allowPromptInjection: true,
          allowConversationAccess: true
        },
        config: {
          databaseUrl: "${ORKMIND_DATABASE_URL}",
          embedding: {
            baseUrl: "https://openrouter.ai/api/v1/embeddings",
            apiKey: "${OPENROUTER_API_KEY}",
            model: "perplexity/pplx-embed-v1-0.6b",
            dimensions: 1024
          },
          autoRecall: true,
          autoCapture: false,
          recallMaxChars: 1000,
          captureMaxChars: 500,
          recallMaxResults: 5
        }
      }
    }
  }
}
```

## Chaves de configuracao da governanca

Todas opcionais. Os defaults sao os valores usados em producao; uma config
existente sem nenhuma delas continua valida.

| Chave | Default | Faixa | Para que serve |
|---|---|---|---|
| `tokenBudget` | 4000 | 500..100000 | budget de tokens do plugin; base do calculo do bloco de regras |
| `mandatoryBudgetRatio` | 0.3 | 0.1..0.9 | fracao do budget reservada as regras mandatorias |
| `failSafe` | `true` | - | emitir o bloco de ALERTA quando as regras somem apos uma carga previa |
| `rulesCacheTtlMs` | 60000 | 0..3600000 | cache das regras; dentro do TTL nao ha consulta ao banco por turno |
| `rulesTimeoutMs` | 2000 | 250..10000 | timeout da carga de regras; estourar aciona o fail-safe, nunca silencio |
| `recallWindowMessages` | 3 | 1..10 | quantas mensagens de usuario ancoram a consulta do nivel 3 |

Sobre o budget: quando o runtime informa a janela real de contexto
(`ctx.contextTokenBudget`), vale o menor entre `tokenBudget` e 15 por cento
da janela real, para o bloco nunca dominar uma janela pequena. Se as regras
nao couberem no budget, elas sao truncadas COM um aviso explicito de quantas
foram omitidas: nunca ha descarte silencioso.

Sobre o cache: em falha de consulta o plugin **nao serve cache velho**. Ele
aciona o fail-safe. Regra desatualizada apresentada como atual e pior do que
um alerta honesto.

## Variaveis de ambiente

- `ORKMIND_DATABASE_URL` - DSN do PostgreSQL (obrigatoria se nao configurada em config)
- `OPENROUTER_API_KEY` - chave de API para embeddings via OpenRouter

## Ferramentas

O plugin registra cinco ferramentas para o agente:

- **memory_search** - busca semantica (contrato obrigatorio do OpenClaw)
- **memory_get** - le o conteudo completo de uma memoria por `orkmind://<id>`
- **memory_recall** - alias de busca semantica na memoria de longo prazo
- **memory_store** - armazena fato, preferencia, decisao ou informacao
- **memory_forget** - remove memoria por ID ou por busca textual

## Garantias de memoria: o que e garantido e o que e melhor esforco

O plugin opera em tres niveis. Eles tem forcas DIFERENTES de proposito, e a
diferenca importa mais do que qualquer numero de benchmark.

| Nivel | O que e | Como e obtido | Forca |
|---|---|---|---|
| N1 | Regras constitucionais (`mandatory = true`, `priority = critical`) | consulta por predicado no banco, injetada em `prependSystemContext` pelo hook `before_prompt_build` | **garantido** enquanto o hook disparar |
| N2 | Demais regras mandatorias | mesmo caminho do N1, no mesmo bloco | **garantido** enquanto o hook disparar |
| N3 | Contexto relevante ao turno | busca semantica (vetor) ou full-text, injetada em `appendContext` | **melhor esforco** |

Regra de leitura: **N1 e N2 nunca dependem de similaridade.** Sao carregados
por `WHERE mandatory = true`, sem vetor, sem embedder e sem `LIMIT`. Um
sistema que recupera regras por similaridade so as entrega quando a conversa
"parece" com elas; aqui elas estao sempre la.

### Fail-safe: nunca operar em silencio sem governanca

O plugin mantem uma maquina de dois estados:

1. **Antes da primeira carga bem-sucedida.** Se o banco nao devolver regras,
   o plugin fica em silencio: o banco pode estar legitimamente vazio.
2. **Depois da primeira carga bem-sucedida.** A existencia de regras esta
   provada neste processo. A partir dai, banco vazio, falha de conexao,
   schema invalido ou timeout produzem um bloco de **ALERTA** explicito no
   system prompt, instruindo o agente a nao prosseguir com operacoes
   criticas. Nunca um silencio.

O bloco de regras e o bloco de alerta sao **mutuamente exclusivos**: o
agente recebe um ou outro, nunca os dois nem nenhum.

Desligar com `failSafe: false` e um opt-out consciente e desaconselhado.

### Guardrail anti-destruicao

O texto que proibe compactar, resumir destrutivamente ou apagar memoria para
abrir espaco e emitido por **duas vias independentes**:

- pelo `promptBuilder` da capability de memoria;
- pelo hook `before_prompt_build`, junto do bloco de regras.

A duplicacao e deliberada porque as duas vias falham em situacoes opostas,
medidas em sessao viva em 31/08/2026:

- o `promptBuilder` **nao roda em subagentes** (`sessions_spawn`), onde o
  hook roda;
- o hook **nao roda** com `hooks.allowPromptInjection: false`, onde o
  `promptBuilder` roda.

Como o guardrail e texto constante, ele fica no espaco cacheavel do prompt e
o custo por turno e proximo de zero.

### Comportamento em subagentes

Verificado em 31/08/2026 contra OpenClaw 2026.7.1-2: um subagente criado por
`sessions_spawn` (depth 1/1) recebe o bloco de regras mandatorias E o
guardrail. A garantia constitucional vale para subagentes.

### Limite estrutural: `hooks.allowPromptInjection`

Se `plugins.entries.memory-orkmind.hooks.allowPromptInjection` for `false`,
o gateway nao chama o hook e **N1/N2 param de ser injetados**. O plugin nao
enxerga essa config, entao nao consegue impedir. O que ele faz:

- emite um `console.error` unico quando detecta 2 ou mais turnos concluidos
  sem um unico disparo do hook;
- mantem o guardrail pelo `promptBuilder`, que continua funcionando.

Se a garantia constitucional importa para voce, mantenha
`allowPromptInjection: true`.

## Auto-recall (nivel 3)

Quando `autoRecall: true`, o plugin monta a consulta do nivel 3 com as
ultimas `recallWindowMessages` mensagens do usuario (nao apenas a ultima
frase) mais um sufixo de escopo derivado da sessao, no formato
`[escopo: projeto=<dir> agente=<id>]`. Com embedder configurado usa busca
vetorial; sem ele, ou se o embedding falhar, cai para full-text search.

Regras mandatorias sao **excluidas** do resultado do nivel 3: elas ja vao
integralmente no system prompt, e reinjeta-las duplicaria custo sem ganho.

O nivel 3 degrada em silencio por definicao: timeout ou erro apenas suprimem
o bloco de contexto, sem afetar N1/N2.

## Auto-capture

Quando `autoCapture: true`, o plugin verifica se a mensagem do usuario contem frases como "lembre-se", "prefiro", "remember", etc, e armazena automaticamente.

## CLI

```bash
openclaw ltm stats
openclaw ltm list [--limit <n>] [--order-by-created-at]
openclaw ltm search <query> [--limit <n>]
```

## Backend

O plugin conecta diretamente ao PostgreSQL do OrkMind usando pgvector para busca vetorial semantica. A tabela `memories` deve existir com a coluna `embedding vector(1024)` e indice HNSW.

## Testes

```bash
npm test
```

Runner `node:test` nativo (sem framework externo). As suites nao tocam rede
nem banco real: pool e embedder sao dublês. A suite de `rules.ts` compara o
bloco gerado contra goldens **produzidos pelo plugin Hermes em Python**, o
que trava a paridade textual entre os dois harnesses.

## Requisitos

- Node.js >= 22
- OpenClaw >= 2026.7.1
- PostgreSQL com extensao pgvector
- Tabela `memories` do OrkMind criada
