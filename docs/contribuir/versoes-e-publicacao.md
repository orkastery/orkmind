# Versões e publicação

> **Em uma frase:** a versão nova entra na `main` por PR, a tag `v<versão>` no commit do merge dispara o CI que publica no PyPI por Trusted Publishing, e publicar é ato do mantenedor.

## Para quem contribui

- Não mude a versão no seu PR: nem `version` no `pyproject.toml`, nem `__version__` em `src/orkmind/__init__.py`.
- Mudou comportamento? Acrescente uma linha na seção `## [Unreleased]` do [CHANGELOG](../../CHANGELOG.md), no grupo certo: `### Added`, `### Changed`, `### Fixed` ou `### Removed`.
- A linha diz o efeito para quem usa, em pt-BR, com o nome exato do que mudou em crase.
- Os plugins em `integrations/` têm CHANGELOG próprio: mudou o plugin, a linha vai no dele.

## Versão atual

Do checkout, as duas fontes que a publicação exige iguais:

```bash
python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
python -c "import orkmind; print(orkmind.__version__)"
```

Do PyPI, com rede:

<!-- checagem: citado -->

```bash
python -m pip index versions orkmind
```

## Publicar (mantenedor)

A publicação sai do CI, nunca de uma máquina. O workflow [publicar.yml](../../.github/workflows/publicar.yml) usa Trusted Publishing (OIDC), sem token nem senha, no ambiente `pypi` do repositório.

1. Um PR leva a versão nova: `version` no `pyproject.toml`, `__version__` em `src/orkmind/__init__.py`, e a seção `## [Unreleased]` do CHANGELOG vira a seção da versão, com a data.
2. Com os checks verdes e o merge feito, o mantenedor cria a tag no commit do merge:

   <!-- checagem: citado -->

   ```bash
   git tag v<versao> <sha-do-merge>
   git push origin v<versao>
   ```

3. O workflow confere que a tag bate com o `pyproject.toml` e com o `__version__`, que o commit está na `main`, que a suíte sem banco passa e que os artefatos passam no `conferir_pacote.py`. Só então o job de publicação recebe o token do PyPI.
4. O mantenedor confere a versão nova no PyPI, com o comando de [versão atual](#versão-atual).

## Versão com defeito

- Versão publicada não se reescreve: a correção sai numa versão nova, pelo mesmo caminho.
- O mantenedor pode marcar a versão com defeito como retirada (yank) no PyPI, com o motivo.

## Próximo passo

Voltar ao [índice dos guias](../../CONTRIBUTING.md), ou começar pelo [o que contribuir](o-que-contribuir.md).

Índice dos guias: [CONTRIBUTING](../../CONTRIBUTING.md).
