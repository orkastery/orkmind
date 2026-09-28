# Changelog do plugin OrkMind para Hermes

Todas as mudancas relevantes do plugin. O formato segue
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e o
versionamento e [SemVer](https://semver.org/lang/pt-BR/).

A fonte de verdade deste plugin e `integrations/hermes/orkmind/` no
repositorio OrkMind. A copia viva em `~/.hermes/plugins/orkmind/` e
publicada por `scripts/sync_hermes_plugin.sh` e nunca deve ser editada
diretamente.

## [1.1.0] - 2026-08-31

### Adicionado

- **Fila de saida (spool) em disco** como primeiro passo do caminho de
  escrita. Novas funcoes `stage_to_spool()` e `mark_done()`, escritas
  apenas com a stdlib para continuarem funcionando quando o pacote
  `orkmind` nao esta importavel ou o banco esta fora.
- Variavel de ambiente `ORKMIND_SPOOL_DIR` para redirecionar a raiz da
  spool (default `~/.hermes/orkmind-spool`).
- `content_hash` (SHA-256) em toda resposta de `orkmind_store`, servindo
  de chave deterministica de deduplicacao e de rastro para auditoria.

### Modificado

- **`orkmind_store` agora estagia antes de gravar.** A ordem passou a
  ser: estagiar na fila em disco, tentar a gravacao direta e so entao
  responder. Antes a tool tentava gravar direto, e uma falha do backend
  ou a ausencia das tools de execucao no toolset de um cron deixava o
  conteudo orfao.
- Resposta de `orkmind_store` ficou explicita sobre o desfecho:
  `status='armazenado'` traz o `id` real confirmado pelo backend;
  `status='pendente'` traz `id=null` mais o `spool_item`, sinalizando que
  o drainer confirmara depois. A descricao da tool instrui o agente a
  nunca presumir um entry_id nesse caso.
- Descricao do schema `ORKMIND_STORE_SCHEMA` documenta o novo contrato de
  gravacao garantida.

### Notas de compatibilidade

- Retrocompativel: o caminho feliz continua devolvendo
  `{"status": "armazenado", "id": ...}` como antes, agora com o campo
  extra `content_hash`.
- Quem consumir a resposta deve tratar `status='pendente'` como
  "ainda nao confirmado", jamais como sucesso com id.

## [1.0.0] - 2026-08-23

### Adicionado

- Versao inicial do plugin: tools `orkmind_recall`, `orkmind_search`,
  `orkmind_store` e `orkmind_rules`, injecao de regras mandatorias,
  camadas de contexto E1/E2/E3 e extracao de memorias soft no fim da
  sessao.
