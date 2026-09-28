# Política de segurança

## Reportar uma vulnerabilidade

Não abra issue pública para vulnerabilidade.

Use o canal privado do GitHub: **Security** > **Report a vulnerability** no repositório
[orkastery/orkmind](https://github.com/orkastery/orkmind/security/advisories/new).

Inclua, se puder:

- o comando ou o passo que reproduz;
- a saída real observada;
- a versão (`pip show orkmind`) e o backend em uso (`orkmind store info`);
- o impacto que você enxerga.

Resposta esperada em até 7 dias. A vulnerabilidade confirmada é corrigida antes da divulgação, e
o crédito vai para quem reportou, salvo pedido em contrário.

To report in English, the same channel works. Please do not open a public issue.

## Superfície do produto

| Superfície | O que existe |
| --- | --- |
| Biblioteca e CLI | Rodam no processo de quem chama; não há daemon obrigatório |
| API local de escrita (`orkmind api`) | Opcional; escuta em 127.0.0.1 por padrão e exige token |
| Servidor MCP | Por stdio, iniciado pelo cliente MCP |
| Plugins do Hermes e do OpenClaw | Injetam regras e memória no prompt do agente a cada turno |
| Estado | O banco que você configurar (PostgreSQL + pgvector por padrão) |

**Memória vira prompt.** O que entra no OrkMind pode chegar ao modelo pelos plugins. Por isso:

- conteúdo suspeito é guardado, mas fica fora da injeção automática;
- entradas `protected` e `priority: critical` recusam edição e remoção vindas de agente;
- o histórico é append-only, então uma alteração maliciosa não apaga a versão anterior.

Trate como não confiável o conteúdo que um agente grava a partir de fonte externa (página,
e-mail, arquivo de terceiros), do mesmo jeito que você trata a entrada de um usuário.

## Segredos

O OrkMind lê a DSN do banco de `ORKMIND_DATABASE_URL` ou do `~/.orkmind/config.toml`, e o token da
API local de `ORKMIND_API_TOKEN`. Nenhum deles deve ir para o repositório, para os exemplos ou
para os testes. A criptografia em repouso das coleções sensíveis é opcional
(`pip install "orkmind[encryption]"`).
