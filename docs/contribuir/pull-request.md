# Pull request

> **Em uma frase:** título que diz o resultado, descrição com o problema, a mudança, a prova e o risco, os checks obrigatórios do CI verdes e commits com a sua identidade.

## Título

- Curto, dizendo o resultado para quem usa, como "`orkmind store info` ganha a saída em JSON".
- Em pt-BR ou em inglês.

## Descrição

O [modelo de PR](../../.github/PULL_REQUEST_TEMPLATE.md) já abre com as seções. Seção em branco faz o PR voltar.

| Seção | O que traz |
| --- | --- |
| O problema | O que estava errado, com o comando ou o passo que reproduz |
| A mudança | O que você fez, e o que deliberadamente não fez |
| A prova | A saída real dos comandos, colada, não descrita |
| Baseline | O que já falhava antes da sua mudança |
| O risco | O que pode quebrar, e o que fica sem cobertura |

Um PR que descreve o problema em uma linha e cola a saída de um teste novo vale mais que três parágrafos sem comando.

## Evidência esperada

- A saída de `python -m pytest -m "not integration" -q`, como diz [testes](testes.md#o-que-rodar-antes-do-pr).
- Mudou comportamento: um teste que falhava antes e passa agora.
- Mexeu em `store`, `memory_provider` ou no que grava no banco: a saída da suíte de integração, que o CI não roda. Ver [o que precisa de banco](testes.md#o-que-precisa-de-banco).
- Mexeu em Python: a saída do ruff nos arquivos que você mudou, como diz [lint e estilo](lint-e-estilo.md#ruff-nos-arquivos-que-você-mudou).
- Mexeu em texto: a saída de [conferir antes do PR](documentacao.md#conferir-antes-do-pr).

## Checks obrigatórios

A `main` é protegida: o merge só sai com os checks obrigatórios verdes no último commit do PR. São os jobs de [ci.yml](../../.github/workflows/ci.yml).

| Check | O que roda |
| --- | --- |
| `testes (Python <versão>)`, um por versão da matriz | A instalação com os extras e a suíte sem banco |
| `pacote (sdist e wheel)` | Build, `twine check --strict`, o `conferir_pacote.py` e o wheel instalado num venv limpo |
| `plugin OpenClaw (memory-orkmind)` | `npm ci`, os testes e o build do plugin |

- Nenhum check roda ruff, mypy ou a suíte de integração: por isso a descrição traz essa prova quando ela vale.
- No primeiro PR vindo de fork, o GitHub pode esperar o mantenedor liberar os checks.

## Identidade

- Commite com a sua identidade do GitHub. Para não expor seu e-mail, use o endereço `noreply` que o GitHub mostra em Settings, Emails:

  <!-- checagem: citado -->

  ```bash
  git config user.email "<id>+<usuario>@users.noreply.github.com"
  ```

- Um agente escreveu parte do diff? Diga na seção "Uso de agente" do PR e ponha o trailer `Co-Authored-By:` com o agente no commit.
- Você responde pelo diff inteiro: revise antes de enviar.

## Commits

- Mensagem no formato `<tipo>(<área>): <o que muda>`, com a área opcional: `feat(memory-provider): ...`, `fix: ...`, `docs: ...`, `test: ...`.
- Os PRs entram na `main` por merge commit, e os seus commits ficam no histórico: cada um precisa fazer sentido sozinho.
- Um commit por mudança lógica. Depois que a revisão começar, prefira commit novo a reescrever o histórico.

## Depois de abrir

- O mantenedor revisa, pede ajuste ou aprova, no prazo do guia de [triagem](triagem.md#prazo-de-resposta).
- O merge é do mantenedor, com os checks verdes no commit exato.

## Próximo passo

[Triagem](triagem.md), para saber o que acontece com o PR e com as issues.

Índice dos guias: [CONTRIBUTING](../../CONTRIBUTING.md).
