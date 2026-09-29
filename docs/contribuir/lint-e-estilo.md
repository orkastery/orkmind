# Lint e estilo

> **Em uma frase:** o ruff confere o Python com as regras do `pyproject.toml`; rode-o nos arquivos que você mudou e não acrescente erro, porque o CI não roda lint e a árvore pode ter dívida anterior.

## O que confere o quê

| Onde | Quem confere |
| --- | --- |
| Python | O ruff, com `[tool.ruff]` do [pyproject.toml](../../pyproject.toml): erros e avisos de estilo (`E`, `W`), erros do pyflakes (`F`), ordem dos imports (`I`) e o limite de `line-length` |
| Tipos | O mypy, com `[tool.mypy]`: toda função com anotação de tipo (`disallow_untyped_defs`) |
| Guias de contribuição | O checador dos guias; ver [documentação](documentacao.md#conferir-antes-do-pr) |
| O que vai ao PyPI | O `conferir_pacote.py`; ver [testes](testes.md#o-pacote) |

## Ruff nos arquivos que você mudou

```bash
git diff --name-only --diff-filter=d origin/main... -- '*.py' | xargs -r ruff check
```

- Lista os `.py` que a sua branch mudou desde a `main` e roda o ruff só neles. O `...` compara commits: commite antes, ou passe o arquivo direto, `ruff check <arquivo>`.
- Num fork, troque `origin` pelo remoto do OrkMind, que costuma se chamar `upstream`.
- `ruff check --fix <arquivo>` corrige o que é seguro, como a ordem dos imports.

## Dívida anterior e o CI

Se `ruff check .`, `ruff format --check .` ou `mypy` saírem diferente de zero na `main`, é dívida anterior. Não é o seu PR que precisa zerar essa dívida; ele só não pode aumentá-la.

- Não rode `ruff format` sobre a árvore: ele reformataria arquivos que você não tocou e esconderia a sua mudança no diff. Não há formatador adotado; siga o arquivo que você edita.
- O mypy olha o pacote inteiro. Compare a saída com a da `main`, como em [falha anterior ou regressão](testes.md#falha-anterior-ou-regressão), e não acrescente erro nos arquivos que você mudou:

<!-- checagem: citado -->

```bash
mypy
```

Nenhum job de [ci.yml](../../.github/workflows/ci.yml) roda ruff ou mypy. Este comando confere que continua assim, e reprova quando um job de lint entrar, para este guia mudar junto:

```bash
! grep -nE 'ruff|mypy' .github/workflows/ci.yml
```

## Estilo de código

- Siga o arquivo vizinho: a API pública em inglês (`MemoryEntry`, `GovernedStore`), e em boa parte do código os nomes internos e os comentários em pt-BR.
- Comentário curto, que explica o porquê, não o quê.
- Tipo em toda função nova, com `from __future__ import annotations` no topo, como na maior parte de `src/orkmind/`.
- Dependência opcional entra por import tardio, dentro da função, para quem não instalou o extra não pagar por ela. É o padrão dos backends em `src/orkmind/store/factory.py` e do `asyncpg` no Memory Provider.
- Nenhum segredo no código, nos testes ou nos exemplos: DSN e token vêm de variável de ambiente, como `ORKMIND_DATABASE_URL` e `ORKMIND_API_TOKEN`, ou do `~/.orkmind/config.toml`.

## Dependências

- As de runtime ficam em `dependencies` do `pyproject.toml`; as opcionais, num extra.
- Dependência nova de runtime pede conversa antes, no [Discussions, categoria Ideas](https://github.com/orkastery/orkmind/discussions/categories/ideas): é decisão de produto.

Para ver as de hoje:

```bash
python -c "import tomllib; print(', '.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))"
```

## Contratos

- As coleções e as dimensões de tag da [ontologia](../ontologia.md).
- O que cada backend declara em `StoreCapabilities`: degradação nova é declarada, nunca silenciosa.
- O histórico que não se reescreve: versões antigas em `memory_versions`.
- Mudar um contrato é PR próprio, com a justificativa separada e a conversa antes, como diz [o que contribuir](o-que-contribuir.md#pede-conversa-antes-sempre).

## Próximo passo

[Pull request](pull-request.md).

Índice dos guias: [CONTRIBUTING](../../CONTRIBUTING.md).
