// Stub de runtime do SDK do OpenClaw para os testes e para o harness do
// benchmark. O plugin so usa `definePluginEntry` como invocacao de
// registro: devolver o proprio objeto de opcoes basta para que o teste
// alcance `register` e capture os handlers.
export function definePluginEntry(opts) {
  return opts;
}
