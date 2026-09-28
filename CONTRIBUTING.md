# Contribuindo com o OrkMind

Obrigado por considerar contribuir. Este documento diz o que o projeto espera, e por que.

Contributions in English are welcome. Part of the code comments and documentation is written in
Brazilian Portuguese, and a clear technical contribution in English will never be turned away
for language alone.

## O princípio

> Nada entra sem evidência executável.

Toda mudança de comportamento vem com o comando que a comprova, e o comando roda nesta árvore.

## Antes de abrir um PR

```bash
pip install -e ".[dev]"
pytest -m "not integration"      # o que o CI roda, sem banco
pytest                           # a suíte inteira; exige ORKMIND_TEST_DATABASE_URL
```

A suíte de integração **apaga** o banco para onde aponta. Por isso ela só aceita um banco cujo
nome deixe claro que é de teste. Nunca aponte os testes para um banco com dados reais.

Se um teste já falhava **antes** da sua mudança, diga isso no PR: é dívida anterior, e não sua.

## O que um bom PR traz

| Parte | O que traz |
| --- | --- |
| **O problema** | O que estava errado, com o comando ou o passo que reproduz |
| **A mudança** | O que você fez, e o que deliberadamente não fez |
| **A prova** | O comando que passa agora e falhava antes, com a saída real colada |
| **O risco** | O que pode quebrar, e o que fica sem cobertura |

## Regras que não mudam num PR de feature

- **As coleções e as dimensões de tag são contrato.** Mudar uma delas muda a ontologia
  ([docs/ontologia.md](docs/ontologia.md)) e pede discussão antes do código.
- **A governança fica acima do backend.** Proteção, ACL, versionamento e ordem vivem no
  `GovernedStore`. Um backend novo não pode enfraquecê-los, e toda degradação é declarada em
  `StoreCapabilities`.
- **O histórico não se reescreve.** Versões antigas ficam em `memory_versions`.
- **Nenhum segredo no repositório.** DSN e tokens vêm de variável de ambiente ou do
  `~/.orkmind/config.toml`, nunca do código, dos testes ou dos exemplos.

## Publicar uma versão (mantenedores)

A publicação no PyPI sai do CI por Trusted Publishing, sem token: a versão entra na `main` por PR
(`pyproject.toml`, `__version__` e o CHANGELOG), e a tag `v<versão>` no commit do merge dispara o
`.github/workflows/publicar.yml`.

## Reportando um bug

Abra uma issue com o comando exato, a saída real e a versão (`pip show orkmind`).

## Segurança

Vulnerabilidade não vai em issue pública: veja [SECURITY.md](SECURITY.md).

## Licença

Ao contribuir, você concorda que a sua contribuição é distribuída sob a [licença MIT](LICENSE).
