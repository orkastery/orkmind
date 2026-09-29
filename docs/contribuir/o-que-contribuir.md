# O que contribuir, e por onde começar

> **Em uma frase:** feature nova começa por uma conversa no Discussions, defeito começa por uma issue, e documentação ou correção pequena pode ir direto para um PR.

## Escolha o caminho

| Você tem | Comece por | Por quê |
| --- | --- | --- |
| Uma feature nova: coleção, dimensão de tag, backend, integração ou comando | [Discussions, categoria Ideas](https://github.com/orkastery/orkmind/discussions/categories/ideas) | Feature nova costuma mexer em contrato; combinar antes evita PR recusado |
| Um defeito: o OrkMind não faz o que a doc diz | [Issue de bug](https://github.com/orkastery/orkmind/issues/new?template=bug_report.yml) | O modelo pede o comando, a saída real, a versão e o backend |
| Uma página errada, confusa ou que falta | PR direto, ou a [issue de documentação](https://github.com/orkastery/orkmind/issues/new?template=documentacao.yml) | Doc errada é defeito do projeto |
| Uma correção pequena: digitação, mensagem, teste que falta | PR direto | Cabe numa revisão só |
| Uma dúvida de uso | [Discussions, categoria Q&A](https://github.com/orkastery/orkmind/discussions/categories/q-a) | A resposta fica achável para quem vier depois |
| Uma vulnerabilidade | O canal privado do [SECURITY.md](../../SECURITY.md) | Nunca em issue pública |

## Pede conversa antes, sempre

- **As coleções e as dimensões de tag são contrato.** Mudar uma delas muda a [ontologia](../ontologia.md).
- **A governança fica acima do backend.** Proteção, ACL, versionamento e ordem vivem no `GovernedStore`. Um backend novo não pode enfraquecê-los, e toda degradação é declarada em `StoreCapabilities`. Ver o [guia de backends](../storage-backends/GUIA-BACKENDS.md).
- **O histórico não se reescreve.** Versões antigas ficam em `memory_versions`, e o Memory Provider recusa `UPDATE` e `DELETE` no histórico por trigger.
- **Dependência nova de runtime** em `dependencies` do [pyproject.toml](../../pyproject.toml) é decisão de produto. O que é opcional vai para um extra, como `qdrant` e `memory-provider`.

## Antes de começar

- Procure nas [issues abertas](https://github.com/orkastery/orkmind/issues) e no [Discussions](https://github.com/orkastery/orkmind/discussions). Se o item existe, comente nele o seu caso.
- Vai pegar uma issue? Comente nela antes, para ninguém fazer em dobro.
- Primeira vez? Comece pelas issues com [`good first issue`](https://github.com/orkastery/orkmind/labels/good%20first%20issue).

## O que ajuda muito

- Bug com o comando exato e a saída real, não a descrição dela.
- Teste que reproduz um defeito, mesmo sem a correção.
- Doc que diverge do código, com o comando que mostra a divergência.
- Backend novo com a suíte de contrato de `tests/contract/` passando nele.

## Próximo passo

[Desenvolvimento local](desenvolvimento-local.md): do clone aos testes rodando.

Índice dos guias: [CONTRIBUTING](../../CONTRIBUTING.md).
