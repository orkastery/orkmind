# Testes

> **Em uma frase:** antes do PR, rode a suíte sem banco, que é a do CI; a de integração precisa de um banco de teste descartável; e compare com a `main` para saber se uma falha é sua ou já existia.

## O que rodar antes do PR

Da raiz do checkout, com o venv ativo:

```bash
python -m pytest -m "not integration" -q
```

- É o passo `testes sem banco` do job `testes (Python <versão>)` de [ci.yml](../../.github/workflows/ci.yml), que roda em cada versão da matriz.
- Não precisa de banco nem de rede. Os módulos que precisam de banco levam a marca `integration` e ficam de fora; os outros testes que dependem de banco se pulam sozinhos, com o motivo.
- Mexeu em Python? Rode também o ruff, como diz [lint e estilo](lint-e-estilo.md).
- Mexeu em texto? Rode também o que [documentação](documentacao.md#conferir-antes-do-pr) lista.

## As três pastas

| Pasta | O que testa | Precisa de |
| --- | --- | --- |
| [tests/unit](../../tests/unit) | Cada módulo isolado | Nada |
| [tests/contract](../../tests/contract) | O mesmo contrato em cada backend de storage: `memory` sempre, `pgvector` e `qdrant` quando há alvo de teste | Banco ou Qdrant só para os backends deles |
| [tests/integration](../../tests/integration) | O pacote contra um PostgreSQL de verdade | Banco de teste |

## Um arquivo, um teste, e por que pulou

```bash
python -m pytest tests/unit/test_models.py -q
python -m pytest tests/unit -q -k guardrail
python -m pytest tests/contract -m "not integration" -q -rs
```

- Troque o arquivo pelo teste que você mexeu; `-k` filtra pelo nome do teste.
- `-rs` lista cada teste pulado com o motivo. O motivo diz a variável que falta: skip silencioso não existe nesta suíte.

## O que precisa de banco

A suíte de integração **apaga** tudo no alvo para onde aponta. Por isso cada alvo tem uma variável própria, e a guarda de [tests/conftest.py](../../tests/conftest.py) só aceita alvo cujo nome deixe claro que é de teste: o nome do banco, ou o prefixo do Qdrant, precisa conter `test`, `_ci` ou `sandbox`. Nunca aponte os testes para um banco com dados reais.

| Variável | Alvo | Quem usa |
| --- | --- | --- |
| `ORKMIND_TEST_DATABASE_URL` | PostgreSQL com pgvector | `tests/integration/` e o backend `pgvector` de `tests/contract/` |
| `ORKMIND_PROVIDER_TEST_DATABASE_URL` | PostgreSQL com pgvector, banco separado | Os testes do Memory Provider; precisam do extra `memory-provider` |
| `ORKMIND_TEST_QDRANT_URL` e `ORKMIND_TEST_QDRANT_PREFIX` | Uma instância de Qdrant de teste | O backend `qdrant` de `tests/contract/`; precisa do extra `qdrant` |

- `ORKMIND_DATABASE_URL` só vale para os testes quando o nome do banco tem o marcador de teste. Os outros alvos não têm esse atalho.
- Qdrant de teste igual ao declarado em produção é erro, não aviso.

Com os bancos de teste do [desenvolvimento local](desenvolvimento-local.md#postgresql-com-pgvector), a suíte inteira:

<!-- checagem: citado -->

```bash
export ORKMIND_TEST_DATABASE_URL="postgresql://orkmind:senha-de-teste@localhost:5432/orkmind_test"
export ORKMIND_PROVIDER_TEST_DATABASE_URL="postgresql://orkmind:senha-de-teste@localhost:5432/memory_provider_test"
python -m pytest -q
```

A senha é a de exemplo do [setup_postgres.sh](../../scripts/setup_postgres.sh); troque pela do seu banco. O CI não roda a integração: rode na sua máquina quando mexer em `store`, `memory_provider` ou em qualquer coisa que grave no banco, e diga no PR o que rodou.

## O plugin do OpenClaw

O plugin em [integrations/openclaw/memory-orkmind](../../integrations/openclaw/memory-orkmind) é um pacote npm, com testes próprios. É o check `plugin OpenClaw (memory-orkmind)`. Precisa de rede para instalar:

<!-- checagem: citado -->

```bash
npm --prefix integrations/openclaw/memory-orkmind ci
npm --prefix integrations/openclaw/memory-orkmind test
npm --prefix integrations/openclaw/memory-orkmind run build
```

## O pacote

O check `pacote (sdist e wheel)` constrói os artefatos e confere o que vai ao PyPI. Mexeu em `pyproject.toml`, em arquivo de dados do pacote ou na lista do sdist? Rode antes; instalar `build` e `twine` precisa de rede:

<!-- checagem: citado -->

```bash
python -m pip install build twine
python -m build
twine check --strict dist/*
python .github/scripts/conferir_pacote.py dist
```

[conferir_pacote.py](../../.github/scripts/conferir_pacote.py) reprova material interno no sdist, arquivo de dados que falta no wheel e texto com cara de credencial.

## Falha anterior ou regressão

Uma falha só é sua se ela não acontecia antes da sua mudança. Para separar:

1. Rode o mesmo comando na `main` atual, numa pasta à parte. Num fork, o remoto do OrkMind costuma se chamar `upstream`: troque `origin` por ele.

   <!-- checagem: citado -->

   ```bash
   git fetch origin
   git worktree add ../orkmind-main origin/main
   cd ../orkmind-main
   PYTHONPATH=src python -m pytest -m "not integration" -q
   ```

   O `PYTHONPATH=src` faz a pasta à parte testar o código dela, e não o do checkout que o venv instalou em modo editável.

2. Passa na `main` e falha na sua branch: é regressão, e é sua.
3. Falha nas duas: é dívida anterior. Diga no PR, na seção "Baseline", com a saída das duas.
4. Falhou uma vez e passou na outra? Rode o arquivo isolado mais vezes antes de concluir, e diga no PR o que viu.

Ao terminar, `git worktree remove ../orkmind-main` apaga a pasta à parte.

## Próximo passo

[Documentação](documentacao.md), se você mexeu em texto; senão, [lint e estilo](lint-e-estilo.md).

Índice dos guias: [CONTRIBUTING](../../CONTRIBUTING.md).
