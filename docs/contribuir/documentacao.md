# Documentação

> **Em uma frase:** cada tipo de texto tem um lugar, o README em inglês muda junto com o espelho em português, e a página cita o comando em vez do número.

## Onde cada doc mora

| O que | Onde | Idioma |
| --- | --- | --- |
| Entrada do repositório | [README.md](../../README.md), espelhado em [README.pt-BR.md](../../README.pt-BR.md) | Inglês, com o espelho em português |
| Como contribuir | esta pasta, com o índice em [CONTRIBUTING.md](../../CONTRIBUTING.md) | pt-BR, índice com nota em inglês |
| Segurança e convivência | [SECURITY.md](../../SECURITY.md) e [CODE_OF_CONDUCT.md](../../CODE_OF_CONDUCT.md) | pt-BR, com uma linha em inglês |
| Referência e arquitetura por tema | [docs/](..): ontologia, integração, MCP, Hermes, federação, gravação garantida, Memory Provider e backends | O da página: hoje há páginas em inglês e em pt-BR |
| Benchmark | [bench/README.md](../../bench/README.md), e a seção de resultados do README | pt-BR no método, inglês no README |
| Exemplos | [examples/](../../examples) | O do exemplo |
| Plugins | README e CHANGELOG próprios em [integrations/hermes/orkmind](../../integrations/hermes/orkmind) e [integrations/openclaw/memory-orkmind](../../integrations/openclaw/memory-orkmind) | pt-BR |
| Skills para agentes | [skills/](../../skills) | Inglês |
| Mudanças por versão | [CHANGELOG.md](../../CHANGELOG.md), no formato Keep a Changelog | Títulos do formato em inglês, entradas em pt-BR |

## Padrão de escrita

- Resposta primeiro: logo abaixo do título, a linha `> **Em uma frase:**`, como nestes guias.
- Uma ideia por linha: tópico em vez de parágrafo longo.
- Cite o comando, não o número. "Rode `python -m pytest -m "not integration" -q`" não envelhece; "a suíte tem tantos testes" envelhece no próximo PR.
- Nome de código, comando, variável e caminho em crase, exatos.
- Em pt-BR, sem travessão longo (U+2014): use vírgula, dois-pontos ou parênteses.
- Nada pessoal: sem caminho da sua máquina, e-mail ou nome de cliente. O repositório é público.
- DSN de exemplo leva senha de marcador, como `senha-de-teste` ou `password`. O [conferir_pacote.py](../../.github/scripts/conferir_pacote.py) reprova, no que vai ao PyPI, DSN com senha que pareça real.

## Paridade

- **De idioma:** `README.md` e `README.pt-BR.md` mudam juntos, no mesmo PR.
- **Com o benchmark:** a seção entre os marcadores `BENCH:INICIO` e `BENCH:FIM` do `README.md` sai de `python bench/run.py --atualizar-readme`, que precisa de banco descartável e do plugin do OpenClaw compilado ([bench/README.md](../../bench/README.md)). Não edite à mão.
- **Com o contrato:** mudou coleção, dimensão de tag ou validação? A [ontologia](../ontologia.md) muda no mesmo PR.
- **Com a versão:** mudança de comportamento ganha linha no CHANGELOG, como diz [versões e publicação](versoes-e-publicacao.md#para-quem-contribui).

## Comandos nos guias

- Todo bloco `bash` destes guias roda, da raiz, na checagem completa, e precisa sair 0. Cada linha roda num `bash -c` próprio: `cd`, `export` e `source` não passam para a linha seguinte.
- Os blocos rodam com o Python que roda a checagem na frente do `PATH`, e sem nenhuma variável `ORKMIND_*`: o que roda ali roda sem banco.
- Comando que usa rede, Docker ou banco, tem efeito fora da máquina, ou sai diferente de zero por dívida anterior que o texto declara, vai num bloco precedido da linha `<!-- checagem: citado -->`. Ele não roda.
- Em todo PR, o teste [test_guias_de_contribuicao.py](../../tests/unit/test_guias_de_contribuicao.py) confere que o que os guias citam existe: comando do `orkmind`, marca do pytest, extra, variável, caminho, link e âncora, rótulo dos modelos e os checks do guia de PR.
- A checagem completa roda os blocos com o resto do seu ambiente: rode só sobre guias que você leu.

## Conferir antes do PR

```bash
python .github/scripts/checar_guias.py --so-existencia
```

- Confere o que o teste confere e, em todo Markdown versionado, os links relativos.
- Mexeu em `CONTRIBUTING.md`, nesta pasta ou nos modelos do `.github`? Rode também a checagem completa, que roda os blocos e leva o tempo da suíte sem banco:

<!-- checagem: citado -->

```bash
python .github/scripts/checar_guias.py
```

## Próximo passo

[Lint e estilo](lint-e-estilo.md).

Índice dos guias: [CONTRIBUTING](../../CONTRIBUTING.md).
