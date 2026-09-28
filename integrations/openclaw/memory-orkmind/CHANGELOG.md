# Changelog

Todas as mudancas relevantes deste plugin.

## [Unreleased]

### Corrigido

- **Tools na assinatura real do OpenClaw.** O runtime (2026.7.1) chama
  `execute(toolCallId, params, signal, onUpdate)`; as cinco tools
  (`memory_search`, `memory_get`, `memory_recall`, `memory_store`,
  `memory_forget`) esperavam `execute(params)`, e `params.query.slice`
  quebrava na primeira busca. O resultado passa ao formato do SDK
  (`content` para o modelo, `details` para log e interface). Testes em
  `test/tools.test.ts` chamam as tools pelas fabricas registradas, como o
  runtime faz.

## [0.2.0] - 2026-08-31

Ciclo de evolucao do core, Bloco A: fechar o gap de garantia entre o plugin
Hermes (Python) e este plugin (TypeScript). Antes deste ciclo o OpenClaw nao
tinha nenhuma injecao de regras mandatorias: a memoria so chegava ao agente
se a busca por similaridade decidisse que a conversa "parecia" com ela.

### Adicionado

- **Injecao incondicional de regras mandatorias (N1/N2).** O hook
  `before_prompt_build` passa a carregar as regras por predicado
  (`WHERE mandatory = true`, sem vetor, sem embedder, sem `LIMIT`) e a
  injeta-las em `prependSystemContext`, campo que o SDK declara cacheavel
  pelo provider. Independe de `autoRecall` e de haver embedder configurado.
- **Fail-safe D-MA9 com maquina de dois estados.** Antes da primeira carga
  bem-sucedida, banco vazio e silencio legitimo. Depois dela, vazio, falha,
  schema invalido ou timeout produzem um bloco de ALERTA explicito no system
  prompt. Bloco de regras e alerta sao mutuamente exclusivos.
- **Guardrail anti-destruicao (D-MA5) por duas vias.** Pelo `promptBuilder`
  e pelo hook. As duas falham em situacoes opostas: o `promptBuilder` nao
  roda em subagentes, o hook nao roda com `allowPromptInjection: false`.
- `getMandatoryRules()` em `database.ts`: carga de regras sem embedder.
- Parametro `excludeMandatory` em `searchByVector` e `searchByText`, usado
  pelo nivel 3 para nao reinjetar o que ja esta no system prompt.
- Chaves de config `tokenBudget`, `mandatoryBudgetRatio`, `failSafe`,
  `rulesCacheTtlMs`, `rulesTimeoutMs`, `recallWindowMessages`, todas
  opcionais com default e clamp.
- Heuristica de deteccao de `hooks.allowPromptInjection: false`: alerta unico
  no log quando 2 ou mais turnos terminam sem um unico disparo do hook.
- Primeira suite de testes do plugin: 63 testes em `node:test`, sem rede e
  sem banco real. A paridade textual com o plugin Hermes e verificada contra
  goldens gerados pelo proprio codigo Python.
- Script `scripts/sync_openclaw_plugin.sh` no repo do OrkMind, com
  `--dry-run`, backup do `dist/` anterior e verificacao de paridade.

### Alterado

- **Consulta do nivel 3 ancorada no escopo da sessao.** A query deixa de ser
  a ultima frase do usuario e passa a ser a janela das ultimas
  `recallWindowMessages` mensagens mais um sufixo `[escopo: projeto=...
  agente=...]` derivado do contexto do runtime.
- `searchByVector` ordena por `mandatory DESC` antes da distancia, para que
  regras mandatorias nunca sejam cortadas pelo `LIMIT` em `memory_search`.
- O `promptBuilder` deixa de retornar vazio quando o agente nao tem tools de
  memoria: o cabecalho e o guardrail passam a ser incondicionais, e so as
  instrucoes de uso das tools seguem condicionadas.

### Corrigido

- **Bug do `schemaValidated`.** A versao anterior marcava o schema como
  validado mesmo quando a validacao falhava, e o turno seguinte prosseguia
  sem validacao nenhuma. Agora o plugin guarda o RESULTADO e revalida
  enquanto estiver invalido.
- `deleteByQuery` reordena candidatos por score antes de eleger o melhor.
  Sem isso a ordenacao nova por `mandatory DESC` faria `memory_forget`
  eleger uma regra mandatoria como candidata a remocao mesmo sendo menos
  similar que o alvo real.

### Verificado em sessao viva (2026-08-31, OpenClaw 2026.7.1-2)

- Sessao normal: bloco `## REGRAS MANDATORIAS (OBEDECER SEMPRE)` presente
  com as 3 regras da base, e guardrail presente.
- Subagente (`sessions_spawn`, depth 1/1): regras presentes e guardrail
  presente.
- Teste negativo com backend derrubado apos uma carga bem-sucedida: bloco
  `## ALERTA: Regras de Governanca Indisponiveis` presente no prompt, sem
  o bloco de regras.

## [0.1.0]

Versao inicial: tools de memoria, auto-recall por similaridade, auto-capture
por triggers e modo `smart`, CLI `openclaw ltm`.
