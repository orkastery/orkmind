<!--
Seção em branco faz o PR voltar. O guia de PR:
https://github.com/orkastery/orkmind/blob/main/docs/contribuir/pull-request.md
Contributions in English are welcome.
-->

## O problema

<!-- O que estava errado, com o comando ou o passo que reproduz. -->

## A mudança

<!-- O que você fez, e o que você deliberadamente NÃO fez. -->

## A prova

<!--
Cole a SAÍDA REAL, não a descrição dela. Nada entra sem evidência executável.

Da raiz do checkout, com o venv ativo (o guia de testes explica cada um):
  `python -m pytest -m "not integration" -q`
  `git diff --name-only --diff-filter=d origin/main... -- '*.py' | xargs -r ruff check`
https://github.com/orkastery/orkmind/blob/main/docs/contribuir/testes.md

Mexeu em store, memory_provider ou no que grava no banco? A suíte de integração, que o CI não roda:
https://github.com/orkastery/orkmind/blob/main/docs/contribuir/testes.md#o-que-precisa-de-banco

Mexeu em texto? Também:
  `python .github/scripts/checar_guias.py --so-existencia`
https://github.com/orkastery/orkmind/blob/main/docs/contribuir/documentacao.md
-->

```text
```

## Baseline

<!--
Algum comando já falhava ANTES da sua mudança? Diga qual, com a saída na `main`: é dívida
anterior, e não é sua. Como separar:
https://github.com/orkastery/orkmind/blob/main/docs/contribuir/testes.md#falha-anterior-ou-regressão
-->

## O risco

<!-- O que pode quebrar, e o que fica sem cobertura. -->

## Contrato

- [ ] Não altero coleção nem dimensão de tag da ontologia. *Se altero, é mudança de contrato:
      PR próprio, com a conversa antes no Discussions.*
- [ ] Não enfraqueço a governança do `GovernedStore`, e toda degradação nova de backend está
      declarada em `StoreCapabilities`.
- [ ] Não reescrevo o histórico: as versões antigas continuam em `memory_versions`.
- [ ] Não adiciono dependência de runtime em `dependencies` sem conversa antes.
- [ ] Nenhum segredo no diff: DSN e token vêm de variável de ambiente ou do
      `~/.orkmind/config.toml`.
- [ ] Mudança de comportamento (se houver) tem linha em `## [Unreleased]` do `CHANGELOG.md`.

## Uso de agente

- Modelos usados: <!-- ou "nenhum" -->
- Host ou interface: <!-- ou "nenhum" -->
- [ ] Commit com trecho escrito por agente traz o trailer `Co-Authored-By:`.
- [ ] Eu, autor humano, revisei e aprovei o diff inteiro antes de enviar.
