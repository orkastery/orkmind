# Memória federada por projeto

O OrkMind mantém uma memória fisicamente separada por projeto e oferece um
recall somente leitura entre elas. Cada resultado preserva `source`, `project`
e `producer`. O manifesto contém apenas nomes de variáveis de ambiente; as
credenciais não são serializadas nele.

Use [o manifesto de exemplo](../examples/federation.json) e exporte os DSNs no
ambiente do processo. Depois cadastre as três identidades explícitas:

```bash
orkmind federation profile-set --id tomas --display-name "Tomas" \
  --role system_owner --manifest examples/federation.json

orkmind federation profile-set --id builder-web --display-name "Builder Web" \
  --role builder --projects orkastery,orkmind --manifest examples/federation.json

orkmind federation profile-set --id agent-plan --display-name "Agente PLAN" \
  --role agent --projects orkastery --current-project orkastery \
  --manifest examples/federation.json
```

O `system_owner` acessa todos os projetos. Um `builder` acessa somente os
projetos e fontes declarados. Um `agent` fica limitado ao seu
`current_project`. Depois dessa autorização de fonte, o `GovernedStore` aplica
novamente a ACL de cada entrada. Perfil ausente ou malformado é sempre negado.

Ork, Hermes, OpenClaw, Codex e Claude Code usam o mesmo contrato e declaram o
canal em `--caller`:

```bash
orkmind federation recall --requester-id tomas --caller hermes \
  --collection decision --tags '{"skill":["roadmap"]}' \
  --manifest examples/federation.json
```

O provider Python do Hermes também expõe `federated_recall()`. Os outros
runtimes podem consumir o JSON estável `orkmind.federated-recall/v1` pelo CLI.
Uma fonte indisponível aparece em `unavailable_sources` e não elimina os
resultados saudáveis das demais fontes.
