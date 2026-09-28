// Redireciona o especificador "openclaw/plugin-sdk/plugin-entry" para o stub
// local. Evita instalar o SDK inteiro (dezenas de MB) so para rodar testes
// unitarios de funcoes que nao dependem do runtime do gateway.
//
// Uso: node --import ./test/hooks.mjs --test dist-test/test/
import { registerHooks } from "node:module";

const STUB = new URL("./openclaw-stub.mjs", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "openclaw/plugin-sdk/plugin-entry") {
      return { url: STUB, format: "module", shortCircuit: true };
    }
    return nextResolve(specifier, context);
  },
});
