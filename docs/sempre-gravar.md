# Gravacao garantida: spool, drainer e API idempotente

> Como o OrkMind garante que todo conteudo produzido por agente ou cron
> termina gravado, mesmo quando as tools de execucao nao estao no
> toolset do job e mesmo quando o backend esta fora do ar.

## O problema

Um job agendado gerou um relatorio semanal e reportou:

> ERRO: NAO foi possivel executar o `orkmind_store`/CLI nesta rodada: as
> tools de execucao (terminal/CLI orkmind) nao estavam disponiveis no
> contexto do cron. Para nao inventar um entry id falso, nao confirmei
> gravacao.

O agente agiu certo ao nao inventar um id, mas o conteudo ficou orfao em
disco. A causa raiz e estrutural: enquanto a gravacao depender de o
agente ter a tool certa no contexto e de o banco estar de pe naquele
instante exato, sempre havera uma janela de perda.

## O principio

Quatro regras constitucionais governam a solucao:

1. **Armazenamento e autoridade.** O que o agente produz precisa existir
   no OrkMind como fonte primaria.
2. **Contexto e projecao.** O que esta em contexto deriva do
   armazenamento, nunca o substitui.
3. **Nada se perde.** Nunca deletar, resumir ou sobrescrever memoria
   para abrir espaco.
4. **Nao inventar IDs.** Sem confirmacao real do backend, nao ha
   gravacao confirmada.

## A arquitetura

A ideia central e inverter a ordem de escrita: **estagiar em disco
primeiro, gravar depois**. O disco quase nunca falha; o banco e a rede
falham. Persistindo a intencao antes de tentar gravar, a perda deixa de
ser possivel e vira, no pior caso, um atraso.

```
  agente / cron
       |
       v
  [1] spool pending/     <- escrita atomica, sempre acontece
       |
       v
  [2] tentativa direta   <- se a tool existir e o banco responder
       |
       +-- ok  --> done/ com entry_id REAL
       |
       +-- falha --> fica em pending/
                        |
                        v
                   [3] drainer (processo externo)
                        |
                        v
                   [4] API idempotente (fallback CLI)
                        |
                        v
                   done/ com entry_id REAL
```

O drainer e **externo ao agente**: um processo comum rodando no venv do
OrkMind. E isso que remove a raiz do problema, porque ele nao depende de
toolset nenhum.

## Componentes

### 1. Spool (fila de saida em disco)

Modulo `orkmind.spool`. Raiz configuravel por `ORKMIND_SPOOL_DIR`
(default `~/.hermes/orkmind-spool`):

```
pending/<item_id>.json    metadados, hash, tentativas, estado
pending/<item_id>.md      conteudo verbatim
done/                     gravado, com entry_id real
failed/                   esgotou tentativas, aguarda revisao humana
drainer.log               auditoria
```

O `item_id` e `<YYYYmmdd_HHMMSS>_<slug>_<8 primeiros do hash>`:
ordenavel por tempo, e o sufixo de hash impede colisao entre itens
estagiados no mesmo segundo.

Escrita atomica via `tmpfile + os.replace()`, com o `.md` gravado antes
do `.json` para que o drainer nunca encontre metadado apontando para
conteudo inexistente. Transicoes de estado sao rename, nunca delete:
`done/` acumula historico e `failed/` retem para revisao.

### 2. Drainer

`scripts/orkmind_drain.py`. Para cada item vencido em `pending/`:

1. Confere que o `content_hash` do `.json` descreve o `.md` em disco.
   Se divergir, o item esta corrompido e vai direto para `failed/`, sem
   nunca ser gravado sob uma chave de dedupe errada.
2. Faz lookup por hash. Se ja existe entry, fecha em `done/` com o
   `entry_id` existente (`status: duplicate`), sem inserir nada.
3. Grava pela API local; se ela estiver fora, pelo CLI com `--dedupe`.
4. Sucesso move para `done/` com o `entry_id` real. Falha transitoria
   incrementa `attempts` e agenda retry com backoff progressivo
   (1min, 5min, 15min, 1h, 4h, 8h, 12h, 24h). Esgotadas as 8
   tentativas, vai para `failed/`.

```bash
# uma passada (cron)
.venv/bin/python scripts/orkmind_drain.py --once

# laco continuo
.venv/bin/python scripts/orkmind_drain.py --watch --interval 60

# estado da fila, sem gravar
.venv/bin/python scripts/orkmind_drain.py --status

# migrar orfaos legados de um diretorio
.venv/bin/python scripts/orkmind_drain.py \
    --backfill ~/.hermes/reports --collection content \
    --tags '{"project": ["possibilidades-ia"]}' --once
```

O backfill nao apaga nem move o arquivo de origem, e e repetivel: o que
ja esta na fila (por hash) e ignorado.

### 3. API idempotente

Modulo `orkmind.api`, servido por `orkmind api` (default
`127.0.0.1:8077`).

| Rota | Metodo | Descricao |
|---|---|---|
| `/health` | GET | Vivacidade. Publica, nao revela nada do acervo. |
| `/entries/by-hash?collection=&hash=` | GET | Lookup de idempotencia. So metadados. |
| `/entries` | POST | Escrita idempotente. |

O `POST /entries` sempre responde `{entry_id, content_hash, status}`,
com `status` em `created` ou `duplicate`. Repetir a mesma requisicao
nunca gera memoria duplicada, que e o que permite ao drainer reprocessar
a fila sem medo.

**Postura de seguranca:**

- Token Bearer obrigatorio via `ORKMIND_API_TOKEN`, comparado em tempo
  constante. Sem token configurado a API **falha fechada**: as rotas
  protegidas respondem 503, nunca ficam abertas por omissao.
- **Simetria de hash**: se o cliente enviar `content_hash`, o servidor
  recomputa o SHA-256 e rejeita divergencia com 400. Sem isso um cliente
  poderia registrar conteudo sob o hash de outro e envenenar a chave de
  deduplicacao, fazendo o conteudo legitimo ser descartado como
  duplicata.
- O lookup por hash devolve apenas metadados, nunca o conteudo, para nao
  virar oraculo de confirmacao de conteudo alheio.
- `mandatory=true` e `priority=critical` sao recusados quando
  `source=agent`: governanca so vem de humano autenticado.
- Escuta em loopback por padrao. Esta API grava memoria e nao deve ficar
  exposta na rede.

### 4. Idempotencia dura no banco

Indice unico parcial, em `migrations/f4_content_hash_idempotencia.sql`:

```sql
CREATE UNIQUE INDEX uq_memories_collection_content_hash
ON memories (collection, content_hash) WHERE content_hash IS NOT NULL;
```

E a rede de seguranca final: mesmo com dois drainers concorrentes, o
segundo INSERT falha, e tanto a API quanto o CLI reconsultam e devolvem
o `entry_id` de quem chegou primeiro.

O indice e **parcial**: entries legadas com `content_hash` nulo nunca
colidem. Se o banco ja tiver duplicatas, a criacao falha de proposito e
exige revisao humana. `PostgresAdapter.initialize()` tolera essa falha e
apenas avisa, para nunca derrubar o CLI. A consulta de diagnostico esta
no cabecalho do arquivo de migracao. Nenhuma duplicata e resolvida
automaticamente: apagar memoria e decisao humana.

### 5. Plugin Hermes

A tool `orkmind_store` (versao 1.1.0) passou a estagiar antes de gravar.
A resposta agora distingue os dois desfechos:

```jsonc
// gravou
{"status": "armazenado", "id": "<entry_id real>", "content_hash": "..."}

// nao confirmou
{"status": "pendente", "id": null, "spool_item": "...", "content_hash": "..."}
```

Em `status: pendente` o agente **nunca** deve presumir um entry_id: o
conteudo esta seguro na fila e o id verdadeiro so existe apos a
confirmacao do backend, feita pelo drainer.

Os helpers de spool do plugin usam somente a stdlib, de proposito: se o
pacote `orkmind` nao estiver importavel, a intencao de memoria ainda
precisa chegar ao disco.

## Configuracao

| Variavel | Default | Uso |
|---|---|---|
| `ORKMIND_SPOOL_DIR` | `~/.hermes/orkmind-spool` | Raiz da fila |
| `ORKMIND_API_URL` | `http://127.0.0.1:8077` | Base da API para o drainer |
| `ORKMIND_API_TOKEN` | (vazio) | Token Bearer. Sem ele a API nega tudo |
| `ORKMIND_DATABASE_URL` | (config.toml) | DSN usado pelo fallback CLI |

## Operacao

Monitorar `pending/`: se crescer alem de poucos itens ou se houver item
com idade alta, o drainer parou. `--status` devolve JSON pronto para
alerta, e `--once` sai com codigo 1 quando sobrou falha na passada.

Itens em `failed/` sempre exigem olhar humano. Nada ali e descartavel:
sao conteudos que o sistema nao conseguiu gravar e que continuam
integros em disco.

`done/` cresce indefinidamente por design. Se o espaco incomodar,
compacte, nunca apague.
