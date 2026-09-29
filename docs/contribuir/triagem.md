# Triagem

> **Em uma frase:** toda issue nova recebe um rótulo de tipo pelo modelo, a primeira resposta do mantenedor em até 7 dias e um destino claro.

## Rótulos

| Rótulo | Quando |
| --- | --- |
| `bug` | O OrkMind não faz o que a doc diz. Entra pelo modelo de bug |
| `documentation` | Página errada, desatualizada, confusa ou que falta. Entra pelo modelo de documentação |
| `enhancement` | Ideia ou pedido de funcionalidade. Entra pelo modelo de ideia |
| `needs triage` | Issue nova, ainda sem a primeira resposta. Os modelos pedem; sai na triagem |
| `question` | Dúvida de uso. O lugar melhor é o Discussions, categoria Q&A |
| `good first issue` | Pequena, com o caminho descrito, boa para a primeira contribuição |
| `help wanted` | O mantenedor aceita PR de fora e não vai fazer tão cedo |
| `duplicate` | Já existe outra issue; o comentário aponta qual |
| `wontfix` | Fora do escopo do produto; o comentário diz por quê |
| `invalid` | Não é defeito nem pedido deste projeto |
| `accessibility` | Barreira para pessoas com deficiência |

- Os nomes seguem o padrão do GitHub: `good first issue` alimenta a página de contribuição do repositório.
- Criar rótulo é ato do mantenedor. O GitHub ignora o rótulo que o modelo pede e o repositório ainda não tem.

## Prazo de resposta

- Primeira resposta em até 7 dias, a mesma meta do [SECURITY.md](../../SECURITY.md). É meta, não garantia.
- PR segue o mesmo prazo para a primeira revisão.
- Passou do prazo? Comente na issue ou no PR.

## Primeira issue

Uma issue recebe `good first issue` quando:

- cabe num PR pequeno;
- diz o arquivo e o comando que prova a correção;
- não mexe em contrato (coleção, dimensão de tag, governança) nem em dependência.

A lista está em [good first issue](https://github.com/orkastery/orkmind/labels/good%20first%20issue).

## Destinos da triagem

| Resultado | O que acontece |
| --- | --- |
| Reproduzido | O rótulo de tipo fica e `needs triage` sai |
| Falta informação | O mantenedor pede o que falta, com o comando que ajudaria |
| Duplicado | `duplicate`, com o link da outra issue |
| Fora de escopo | `wontfix`, com o motivo |
| Pergunta | Vai para o Discussions, categoria Q&A |
| Ideia que mexe em contrato | Vai para o Discussions, categoria Ideas, antes de virar trabalho |

## Próximo passo

[Versões e publicação](versoes-e-publicacao.md), para quem mantém o projeto.

Índice dos guias: [CONTRIBUTING](../../CONTRIBUTING.md).
