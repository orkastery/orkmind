# Company Brain v1

O Company Brain é a projeção exata e governada do que a fábrica sabe sobre o próprio portfólio:
produtos (`prod`), projetos (`proj`) e iniciativas (`init`), com captura determinística,
proveniência por item, recibos por etapa e histórico append-only. Ele não é a memória semântica
do OrkMind (coleções, tags e busca): vive em tabelas próprias (`brain_*`) no PostgreSQL e só
responde o que foi gravado por um produtor autorizado, citando de onde veio.

- Código: `src/orkmind/core/company_brain.py` (contrato), `src/orkmind/core/company_brain_access.py`
  (identidade e concessões), `src/orkmind/store/company_brain.py` (projeção, histórico, contexto),
  `src/orkmind/core/company_brain_migration.py` (migração administrativa) e
  `src/orkmind/cli/company_brain.py` (API e CLI).
- Schema do banco: `src/orkmind/store/company_brain_v1.sql`, dentro do pacote.
- Testes: `tests/unit/test_company_brain_*.py` e `tests/integration/test_company_brain_*.py`.
- Produtor e consumidor principal: o núcleo do [Orkastery](https://github.com/orkastery/orkastery)
  (`ork brain`).

## Contrato e API

O contrato `orkmind.company-brain/v1` é o arquivo `src/orkmind/contracts/company-brain.v1.json`,
gerado do modelo Pydantic e idêntico, byte a byte, ao `core/schemas/company-brain.schema.json` do
Orkastery. O sha256 desse arquivo sai em `capabilities` como `contract_hash`, e o Orkastery recusa
um Brain com hash diferente (`brain.contract.mismatch`). Por isso o contrato só muda nos dois
repositórios ao mesmo tempo, com o teste de paridade dos dois lados: o corpus
`tests/fixtures/company-brain-v1.json` e o pacote dourado `tests/fixtures/company-brain-context-v1.json`
são os mesmos arquivos no Orkastery.

| Tipo | Schema | O que é |
| --- | --- | --- |
| Entidade | `orkmind.company-brain-entity/v1` | `prod`, `proj` ou `init`, com versão, pai, dono, fonte e ACL |
| Afirmação | `orkmind.company-brain-assertion/v1` | fato determinístico sobre um sujeito, com validade |
| Evento | `orkmind.company-brain-event/v1` | `upsert`, `assert` ou `tombstone` de um agregado, com `payload_hash` e ciclo |
| Recibo | `orkmind.company-brain-receipt/v1` | uma etapa do evento: `received`, `validated`, `materialized`, `indexed`, `retrievable` |
| Seleção | `orkmind.company-brain-selection/v1` | facets, modo (`selection` ou `context`), `limit` e `offset` |
| Migração | `orkmind.company-brain-migration/v1` | plano conferido por hash, com operações e lacunas |

Toda validação é estrita (campo desconhecido é recusado) e tem uma camada semântica: o id começa
pelo kind, `proj` só tem pai `prod-`, `init` só tem pai `proj-`, o `payload_hash` confere com o
payload e a fonte do evento é a do payload. A falha sai só como `brain.contract.invalid`, sem eco
do conteúdo.

O envelope das chamadas é a API `orkmind.company-brain-api/v1`, separada do contrato: operações e
formatos de resposta novos entram nela de forma aditiva, sem mudar o `contract_hash`. O pacote de
contexto (`orkmind.company-brain-context/v1`) e a operação `history` são dessa API.

## Identidade e concessões

A identidade vem só do transporte autenticado: o `session_user` do PostgreSQL (o login do banco,
que `SET ROLE` não altera) é mapeado para um principal na tabela `brain_transport_principals`, que o
administrador do banco provisiona. Nenhum campo da requisição, variável de ambiente ou argumento de
ferramenta nomeia um principal; o envelope com qualquer chave além de `schema`, `operation` e
`payload` é recusado. Login sem mapeamento vira o principal de serviço `postgres:<usuário>`, e
mapeamento revogado recusa tudo (`brain.access.forbidden`).

Cada principal é `human` ou `service`. As concessões ficam em `brain_grants`, por tenant, principal
e `acl_ref`:

| Campo | Efeito |
| --- | --- |
| `actions` | o que a concessão permite, entre `get`, `query`, `history`, `receipts`, `ingest`, `migrate` e `rollback` |
| `resources` | ids das entidades cobertas |
| `source_instances` | instâncias de origem cobertas |
| `fields` | campos do corpo que a leitura mostra (interseção, nunca união, quando há mais de uma concessão) |
| `expires_at`, `revoked` | validade; a revogação vale na chamada seguinte |

`get`, `query` e `history` são só de humano; `ingest`, `migrate` e `rollback` são só de serviço.
A regra padrão é recusar: sem a concessão da ação, a leitura responde como se a entidade não
existisse.

## Operações

A CLI `orkmind brain request` lê um envelope JSON no stdin (até 2 MB) e escreve a resposta no
stdout. A DSN vem de `ORKMIND_DATABASE_URL` e o tenant de `ORKMIND_BRAIN_TENANT`; nenhum argumento
escolhe banco, raiz ou identidade. O código de saída é 0 nos estados `ok`, `empty`, `unknown` e
`withheld`, e 1 nos demais. A ferramenta MCP `orkmind_brain` recebe o mesmo envelope em `request` e
passa pelo mesmo caminho.

```bash
echo '{"schema":"orkmind.company-brain-api/v1","operation":"capabilities"}' | orkmind brain request
```

| Operação | Payload | Ação exigida | Resposta |
| --- | --- | --- | --- |
| `capabilities` | nenhum | nenhuma | `contract_hash`, `operations`, `human_context_required`, `selection_modes` |
| `get` | `tenant_id`, `id` | `get` | `entity` projetada, ou `unknown` ou `withheld` |
| `query` | uma seleção | `query` (modo `selection`) ou `get` (modo `context`) | ver abaixo |
| `history` | `tenant_id`, `id`, `limit` (1 a 1000, padrão 100) e `offset` (0 a 100000) opcionais | `history` | `versions` e `count` |
| `receipts` | `tenant_id`, `event_id` | `receipts` | os recibos do evento |
| `head` | `tenant_id`, `id` | `ingest` | sequência e fonte da versão atual, para o produtor |
| `ingest` | um evento | `ingest` | recibo `materialized` e recibo `retrievable` |

Estados: `ok`, `empty`, `unknown`, `withheld`, `forbidden`, `unavailable` e `conflict`. Erros saem
só como código `brain.*`, nunca com conteúdo, DSN ou texto de exceção. Os de validação e conflito
são `brain.api.invalid`, `brain.contract.invalid`, `brain.context.invalid`, `brain.event.conflict`,
`brain.event.sequence-gap`, `brain.authority.conflict`, `brain.version.conflict`,
`brain.catalog.parent-missing`, `brain.catalog.dependency-invalid` e `brain.receipt.missing`; os de
acesso e ambiente são `brain.access.forbidden`, `brain.selection.fields-forbidden`,
`brain.history.fields-forbidden`, `brain.configuration.missing`, `brain.transport.unavailable` e
`brain.store.unavailable`.

## Seleção

### Modo `selection`

Os facets (`ids`, `kinds`, `workspace_ids`, `source_instances`) combinam por OU dentro de um facet
e por E entre facets. A ACL é aplicada antes de filtrar, paginar e contar, e usar um facet exige
que todas as concessões do principal mostrem o campo dele; senão a seleção inteira é recusada com
`brain.selection.fields-forbidden`, para que um valor invisível não mude a resposta. Item retido
sai `{state: "withheld"}`, sem id, e a presença de um retido visível numa seleção com facet também
recusa a seleção inteira.

### Modo `context`

O modo `context` monta no servidor o pacote de contexto citável: as entidades pedidas e os pais
que a concessão deixa ver, cada uma com a citação inteira, e as lacunas tipadas. Um OrkMind
anterior a este modo responde `unavailable` com `brain.selection.context-unsupported`, que é o
sinal para o cliente usar outro caminho.

Regras:

- Só `facets.ids` (1 a 1000 ids de entidade, `prod-`, `proj-` ou `init-`), os outros facets
  vazios, `offset` 0 e no máximo `limit` ids; senão `conflict` com `brain.context.invalid`.
- Cada id resolve com a semântica do `get`, autorizado pela ação `get`: ausente, apagado e sem
  concessão saem iguais (`entity.unknown`); retido sai com o id e nada mais.
- A citação só vale inteira (`authority`, `instance`, `source_ref`, `source_hash`,
  `source_version` e `location`): sem ela, o item vira a lacuna `citation.incomplete`, sem
  conteúdo.
- O fecho de pais parte só de item citado, segue o `parent_id` que a concessão mostra e sobe de
  kind (`init`, `proj`, `prod`); retido, sem citação e desconhecido não puxam pai.
- Tudo roda numa transação `REPEATABLE READ, READ ONLY`: um instantâneo, nenhuma escrita.
- O item leva só os campos citáveis que a concessão mostra: `kind`, `version`, `title`, `status`,
  `parent_id`, `depends_on`, `owner`, `source`, `observed_at` e `recorded_at`. Descrição,
  critérios de aceite e aliases continuam no `get`.

Resposta (`state` é `ok` com algum item e `empty` sem nenhum):

```json
{
  "schema": "orkmind.company-brain-api/v1",
  "state": "ok",
  "context": {
    "schema": "orkmind.company-brain-context/v1",
    "tenant_id": "exemplo",
    "requested": ["init-exemplo-um", "init-exemplo-dois"],
    "items": [
      {"id": "prod-exemplo", "state": "ok", "entity": {"kind": "prod", "version": 1, "source": {"...": "..."}}},
      {"id": "init-exemplo-dois", "state": "withheld"}
    ],
    "gaps": [
      {"id": "init-exemplo-dois", "code": "entity.withheld"},
      {"id": "init-exemplo-um", "code": "entity.unknown"}
    ],
    "digest": "<sha256 do JSON canônico de schema, tenant_id, requested, items e gaps>"
  }
}
```

Itens em ordem `prod`, `proj`, `init` e depois id; lacunas por id e código; tudo por code point. O
`digest` não tem horário dentro: o mesmo estado do Brain dá o mesmo digest, e quem recebe o pacote
recalcula e confere.

| Lacuna | Quando |
| --- | --- |
| `entity.unknown` | ausente, apagada ou sem a concessão `get` |
| `entity.withheld` | retida pela ACL: só o id aparece |
| `citation.incomplete` | a citação não está inteira no que a concessão mostra; o item não entra |
| `owner.unresolved` | item citado sem dono resolvido para um principal |
| `observed.unknown` | item citado sem `observed_at` |
| `recorded.unknown` | item citado sem `recorded_at` |

O frescor contra a fonte (o portfólio do Orkastery) não é calculado aqui: o OrkMind não tem a
fonte. O servidor cita; quem tem a fonte compara.

## Histórico

A operação `history` lê as versões append-only de uma entidade, em ordem de `sequence`. Cada
ingestão e cada compensação de rollback gravam uma linha em `brain_history` na mesma transação da
projeção, e nada apaga ou reescreve essas linhas.

```json
{"schema": "orkmind.company-brain-api/v1", "operation": "history",
 "payload": {"tenant_id": "exemplo", "id": "proj-exemplo", "limit": 50}}
```

A resposta traz `id`, `active` (falso depois do tombstone), `count` (total de versões) e
`versions`, cada uma com:

| Campo | Origem |
| --- | --- |
| `sequence`, `event_id`, `operation` | o evento no inbox (`upsert` ou `tombstone`) |
| `version` | a versão materializada, do recibo `materialized` |
| `source`, `producer_id` | a citação da fonte e o produtor do evento |
| `thread_id`, `phase` | o ciclo da fábrica que produziu o evento, ou `null` |
| `recorded_at` | o horário de gravação no Brain, em UTC |
| `rolled_back` | verdadeiro quando um rollback de migração compensou esta versão |
| `entity` | a imagem da entidade depois do evento, projetada pela concessão; `null` no tombstone |

Regras: exige a ação `history`, só de humano; sem ela a resposta é `unknown`, igual a uma entidade
que não existe. Entidade retida responde `withheld`. Concessão sem o campo `source` responde
`forbidden` com `brain.history.fields-forbidden`, porque versão sem origem não é citável. Entidade
apagada continua legível: o tombstone é a última versão. A leitura roda no mesmo instantâneo
somente leitura do modo `context`.

## Escrita e migração

A escrita é só do produtor de serviço. `ingest` recebe um evento, confere a sequência do agregado
(`brain.event.sequence-gap`), a autoria (`brain.authority.conflict`: produtor, instância e ACL não
mudam depois da primeira versão), a versão (`brain.version.conflict`) e as referências, e grava na
mesma transação inbox, projeção, histórico, recibos e outbox. O mesmo evento repetido devolve o
mesmo recibo; o mesmo `source_event_id` do produtor com conteúdo diferente é `brain.event.conflict`.

A migração administrativa usa outro envelope, `orkmind.company-brain-admin/v1`, pela CLI
`orkmind brain migration`, com as operações `plan` (`entities`, `batch_id`, `source_hash`,
`observed_at`), `apply` (`plan`, `expected_hash`, `current_source_hash`) e `rollback`
(`batch_id`). O plano é conferido por hash e pela fonte atual antes de aplicar; o rollback nunca
restaura o banco inteiro: grava uma compensação por entidade, com sequência nova, e deixa em
`conflicts`, sem tocar, a entidade que outra escrita mudou depois do lote. Falha sai como
`brain.migration.unconfirmed`.

## Banco

O schema é criado só por uma operação administrativa explícita (`BrainStore.initialize()`, que
aplica `company_brain_v1.sql`); abrir uma consulta nunca migra. Tabelas: `brain_transport_principals`,
`brain_grants`, `brain_inbox`, `brain_projection`, `brain_history`, `brain_receipts`, `brain_outbox`,
`brain_migration_batches` e `brain_rolled_back_events`.

## Uso pelo Orkastery

O `ork brain context` do Orkastery pede primeiro o modo `context`, confere o pacote inteiro
(pedido, citações, fecho e digest) e acrescenta o frescor contra o portfólio local. Com um OrkMind
que responde `brain.selection.context-unsupported`, ele monta o pacote pelo caminho anterior
(`query` e `get`) e declara isso no pacote. Para os mesmos dados, os dois caminhos dão o mesmo
conteúdo e o mesmo digest.

## Testes

Os testes de integração do Brain exigem um PostgreSQL isolado em `ORKMIND_TEST_DATABASE_URL`, com
"test" no nome do banco, e falham sem ele, de propósito; cada teste cria e apaga um schema próprio.
O CI roda `pytest -m "not integration"`. Para rodar a integração localmente, qualquer PostgreSQL
descartável serve, por exemplo o contêiner de `scripts/setup_postgres.sh` com um banco de teste
criado nele:

```bash
bash scripts/setup_postgres.sh
docker exec orkmind-postgres createdb -U orkmind orkmind_test
export ORKMIND_TEST_DATABASE_URL=postgresql://orkmind:senha-de-teste@localhost:5432/orkmind_test
pytest tests/integration/test_company_brain_*.py
```
