// Runner do benchmark contra o plugin memory-orkmind (TypeScript).
//
// Importa o `dist/` COMPILADO do plugin e injeta um `api` fake que captura o
// handler de `before_prompt_build` e o `promptBuilder`. Mede as mesmas
// metricas do `run_hermes.py`, no mesmo schema de saida, o que torna o M7
// (fidelidade cruzada) uma comparacao textual direta.
//
// O caminho do `dist/` e parametro (`--dist`) de proposito: e assim que o
// D12 mede o "antes", apontando para o build de um worktree do commit
// pre-port sem precisar congelar o ciclo no meio para medir.
//
// Uso:
//   node bench/harness/run_openclaw.mjs [--dist <caminho>] [--saida <arquivo>]
//                                       [--url <dsn>] [--rotulo <nome>]

import { createRequire, registerHooks } from "node:module";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { criarEmbedderFake } from "./fake_embedder.mjs";

const AQUI = path.dirname(fileURLToPath(import.meta.url));
const BENCH_DIR = path.resolve(AQUI, "..");
const REPO_DIR = path.resolve(BENCH_DIR, "..");
const DIST_PADRAO = path.join(
  REPO_DIR,
  "integrations/openclaw/memory-orkmind/dist"
);
const URL_PADRAO =
  "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind_bench_test";
const MARCADORES_DE_TESTE = ["test", "_ci", "sandbox"];
const TURNOS_DE_DILUICAO = [1, 10, 20, 40];

// O plugin importa o SDK do OpenClaw; para o harness basta um stub que
// devolva o objeto de definicao, dando acesso ao `register`.
const STUB = new URL(
  "../../integrations/openclaw/memory-orkmind/test/openclaw-stub.mjs",
  import.meta.url
).href;
registerHooks({
  resolve(especificador, contexto, proximo) {
    if (especificador === "openclaw/plugin-sdk/plugin-entry") {
      return { url: STUB, format: "module", shortCircuit: true };
    }
    return proximo(especificador, contexto);
  },
});

// --- Argumentos -------------------------------------------------------------

function lerArgs() {
  const args = { dist: DIST_PADRAO, url: URL_PADRAO, saida: null, rotulo: "openclaw" };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i += 2) {
    const chave = argv[i].replace(/^--/, "");
    if (chave in args) args[chave] = argv[i + 1];
  }
  return args;
}

// Mesma guarda do `tests/conftest.py`, aplicada tambem aqui: o runner nao
// escreve, mas apontar um benchmark para producao ja e erro suficiente.
function exigirBancoDescartavel(url) {
  const nome = (new URL(url).pathname || "").replace(/^\//, "").toLowerCase();
  if (!MARCADORES_DE_TESTE.some((m) => nome.includes(m))) {
    console.error(
      `RECUSADO: o banco '${nome}' nao tem marcador de teste no nome ` +
        `(${MARCADORES_DE_TESTE.join(", ")}). Use um banco descartavel.`
    );
    process.exit(2);
  }
}

// --- Dublê do runtime do OpenClaw -------------------------------------------

function criarApiFake(configDoPlugin) {
  const api = {
    pluginConfig: configDoPlugin,
    hooks: {},
    promptBuilder: null,
    registerMemoryCapability(cap) {
      api.promptBuilder = cap.promptBuilder ?? null;
    },
    registerTool() {},
    registerCli() {},
    on(nome, handler) {
      api.hooks[nome] = handler;
    },
  };
  return api;
}

function mensagensDeUsuario(...textos) {
  return textos.map((t) => ({ role: "user", content: t }));
}

async function chamarHook(api, textoDoTurno, ctx = {}) {
  const handler = api.hooks.before_prompt_build;
  if (!handler) return {};
  const resultado = await handler(
    { prompt: textoDoTurno, messages: mensagensDeUsuario(textoDoTurno) },
    ctx
  );
  return resultado ?? {};
}

/** Prompt final montado como o gateway montaria, para medir o que chega. */
function promptFinal(saidaDoHook, api) {
  const partes = [];
  if (api.promptBuilder) {
    const secoes = api.promptBuilder({
      availableTools: new Set([
        "memory_search",
        "memory_get",
        "memory_recall",
      ]),
    });
    partes.push((secoes || []).join("\n"));
  }
  if (saidaDoHook.prependSystemContext) partes.push(saidaDoHook.prependSystemContext);
  if (saidaDoHook.appendSystemContext) partes.push(saidaDoHook.appendSystemContext);
  if (saidaDoHook.appendContext) partes.push(saidaDoHook.appendContext);
  return partes.join("\n");
}

// --- Carga do plugin --------------------------------------------------------

async function carregarPlugin(dist, config) {
  const indexPath = path.join(dist, "index.js");
  if (!existsSync(indexPath)) {
    console.error(`RECUSADO: ${indexPath} nao existe. Rode o build do plugin.`);
    process.exit(1);
  }
  // Query string quebra o cache do ESM: permite recarregar o modulo entre
  // cenarios, o que zera o estado da maquina de dois estados sem depender de
  // API de teste que o build pre-port nao tem.
  const modulo = await import(`${path.resolve(indexPath)}?v=${contadorDeCarga++}`);
  const api = criarApiFake(config);
  modulo.default.register(api);
  return { modulo, api };
}
let contadorDeCarga = 0;

/** Pool de verdade contra o banco do bench, com interruptores de falha. */
function criarPool(pg, url) {
  const pool = new pg.Pool({ connectionString: url, max: 4 });
  const estado = { derrubado: false, vazio: false, schemaInvalido: false, atrasoMs: 0 };
  const espiao = {
    estado,
    async query(sql, params) {
      if (estado.atrasoMs) {
        await new Promise((r) => setTimeout(r, estado.atrasoMs));
      }
      if (estado.derrubado) throw new Error("ECONNREFUSED (cenario do bench)");
      if (estado.schemaInvalido && sql.includes("information_schema.tables")) {
        return { rows: [{ exists: false }] };
      }
      if (estado.vazio && sql.includes("mandatory = true")) return { rows: [] };
      return pool.query(sql, params);
    },
    async end() {
      await pool.end();
    },
  };
  return espiao;
}

// --- Metricas ---------------------------------------------------------------

function omitidasPorBudget(bloco) {
  const achado = /\((\d+) regra\(s\) omitida\(s\) por budget/.exec(bloco || "");
  return achado ? Number(achado[1]) : 0;
}

async function medirInjecao(dist, config, pool, embedder, dados) {
  const { modulo, api } = await carregarPlugin(dist, config);
  await aplicarInjecao(dist, modulo, pool, embedder, config);

  const regras = dados.constitutional.regras;
  const criticas = regras.filter((r) => r.priority === "critical");
  const mandatorias = regras.filter((r) => r.mandatory);
  const turnos = dados.constitutional.turnos;

  let m1 = 0;
  let m2 = 0;
  let omitidas = 0;
  const chars = [];
  const latencias = [];
  const diluicao = {};
  const total = Math.max(...TURNOS_DE_DILUICAO);

  for (let i = 1; i <= total; i++) {
    const turno = turnos[(i - 1) % turnos.length];
    const inicio = process.hrtime.bigint();
    const saida = await chamarHook(api, turno.texto, {
      workspaceDir: "/srv/OrkMind",
      agentId: "bench",
    });
    latencias.push(Number(process.hrtime.bigint() - inicio) / 1e6);
    const prompt = promptFinal(saida, api);

    if (i <= turnos.length) {
      if (criticas.every((r) => prompt.includes(r.content))) m1++;
      if (mandatorias.every((r) => prompt.includes(r.content))) m2++;
      omitidas += omitidasPorBudget(saida.prependSystemContext);
      chars.push((saida.prependSystemContext || "").length);
    }
    if (TURNOS_DE_DILUICAO.includes(i)) {
      const bloco = saida.prependSystemContext || "";
      diluicao[`turno_${i}`] = prompt.length
        ? Number((bloco.length / prompt.length).toFixed(4))
        : 0;
    }
  }

  const den = turnos.length;
  const mediana = (xs) => {
    const s = [...xs].sort((a, b) => a - b);
    return s.length ? s[Math.floor(s.length / 2)] : 0;
  };
  return {
    M1: { num: m1, den, taxa: Number((m1 / den).toFixed(4)) },
    M2: {
      num: m2,
      den,
      taxa: Number((m2 / den).toFixed(4)),
      omitidas_por_budget: omitidas,
    },
    M5: {
      chars_injetados_medio: chars.length
        ? Math.round(chars.reduce((a, b) => a + b, 0) / chars.length)
        : 0,
      latencia_ms_p50: Number(mediana(latencias).toFixed(3)),
    },
    M6: diluicao,
    amostra: await (async () => {
      const saida = await chamarHook(api, turnos[0].texto, {});
      return {
        prependSystemContext: saida.prependSystemContext || "",
        promptBuilder: api.promptBuilder
          ? (api.promptBuilder({ availableTools: new Set() }) || []).join("\n")
          : "",
      };
    })(),
  };
}

/**
 * Injeta pool e embedder no plugin.
 *
 * O build pos-port expoe `__testing`. O build PRE-port nao expoe nada: ali a
 * unica via e deixar o plugin se inicializar sozinho a partir da config, que
 * e exatamente o que queremos medir (o zero pre-port).
 */
async function aplicarInjecao(dist, modulo, pool, embedder, config) {
  if (!modulo.__testing) return false;
  const { resolveConfig } = await import(
    `${path.resolve(path.join(dist, "config.js"))}`
  );
  modulo.__testing.reset();
  // setConfig ANTES de setPool: sem config resolvida o `init()` do handler
  // se inicializaria sozinho e trocaria o pool espiao por um pool real,
  // desligando na pratica os cenarios de falha do M3.
  modulo.__testing.setConfig(resolveConfig(config));
  modulo.__testing.setPool(pool);
  modulo.__testing.setEmbedder(embedder);
  return true;
}

async function medirFailSafe(dist, config, pool, embedder) {
  const ALERTA = "ALERTA: Regras de Governanca Indisponiveis";
  const casos = [];

  const cenario = async (nome, ajustar, esperado) => {
    const { modulo, api } = await carregarPlugin(dist, config);
    const injetou = await aplicarInjecao(dist, modulo, pool, embedder, config);
    Object.assign(pool.estado, {
      derrubado: false,
      vazio: false,
      schemaInvalido: false,
      atrasoMs: 0,
    });
    // Carga previa bem-sucedida: e ela que arma a maquina de dois estados.
    if (nome !== "controle: falha sem carga previa (nao deve alertar)") {
      await chamarHook(api, "turno de aquecimento", {});
    }
    ajustar(pool.estado, modulo.__testing);
    const saida = await chamarHook(api, "turno sob falha", {});
    const prompt = promptFinal(saida, api);
    casos.push({
      caso: nome,
      alerta_presente: prompt.includes(ALERTA),
      esperado,
      injecao_disponivel: injetou,
    });
    Object.assign(pool.estado, {
      derrubado: false,
      vazio: false,
      schemaInvalido: false,
      atrasoMs: 0,
    });
  };

  await cenario("backend derrubado apos carga", (e) => { e.derrubado = true; }, true);
  await cenario("backend vazio apos carga", (e) => { e.vazio = true; }, true);
  // O plugin valida o schema uma vez e guarda o RESULTADO; com schema ja
  // valido ele nao revalida por turno, o que e o comportamento correto e
  // barato. Para exercitar o caminho de schema invalido e preciso forcar a
  // revalidacao, que e exatamente o que acontece num processo que sobe com o
  // banco ja degradado. No build pre-port nao ha como forcar, e o cenario
  // simplesmente nao alerta, que e o resultado que o baseline deve mostrar.
  await cenario(
    "schema invalido apos carga",
    (e, testing) => {
      e.schemaInvalido = true;
      if (testing?.setSchemaValid) testing.setSchemaValid(null);
    },
    true
  );
  await cenario(
    "timeout na carga apos carga previa",
    (e) => { e.atrasoMs = Number(config.rulesTimeoutMs || 2000) + 400; },
    true
  );
  await cenario("sem mensagem de usuario", () => {}, false);
  await cenario(
    "controle: falha sem carga previa (nao deve alertar)",
    (e) => { e.derrubado = true; },
    false
  );

  const acertos = casos.filter((c) => c.alerta_presente === c.esperado).length;
  return { casos, num: acertos, den: casos.length };
}

async function medirRecuperacao(dist, config, pool, embedder, dados) {
  const K = 5;
  const { modulo, api } = await carregarPlugin(dist, config);
  await aplicarInjecao(dist, modulo, pool, embedder, config);

  let recallSoma = 0;
  let precisionSoma = 0;
  let sanidade = 0;

  for (const sessao of dados.thematic.sessoes) {
    const saida = await chamarHook(api, sessao.turnos.join("\n"), {
      workspaceDir: `/projetos/${sessao.escopo}`,
      agentId: "bench",
    });
    const bloco = saida.appendContext || "";
    const relevantes = dados.thematic.memorias.filter((m) =>
      sessao.relevantes.includes(m.id)
    );
    const acertos = relevantes.filter((m) =>
      bloco.includes(m.content.slice(0, 60))
    ).length;
    recallSoma += relevantes.length ? acertos / relevantes.length : 0;
    precisionSoma += acertos / K;
  }

  for (const memoria of dados.thematic.memorias) {
    const saida = await chamarHook(api, memoria.content, {});
    if ((saida.appendContext || "").includes(memoria.content.slice(0, 60))) {
      sanidade++;
    }
  }

  const n = dados.thematic.sessoes.length;
  const total = dados.thematic.memorias.length;
  return {
    recall_at_5: Number((recallSoma / n).toFixed(4)),
    precision_at_5: Number((precisionSoma / n).toFixed(4)),
    embedder: "fake",
    sessoes: n,
    sanidade_pipeline: {
      num: sanidade,
      den: total,
      taxa: Number((sanidade / total).toFixed(4)),
    },
    nota:
      "recall_at_5 e precision_at_5 com embedder fake NAO medem qualidade " +
      "semantica. O que esta validado e a ESTRUTURA, via sanidade_pipeline.",
  };
}

/**
 * Sha do codigo QUE FOI MEDIDO, nao do checkout corrente.
 *
 * O baseline do D12 mede o `dist/` de um worktree do commit pre-port; se
 * gravassemos o HEAD do repo principal, a tabela antes/depois atribuiria os
 * dois numeros ao mesmo commit e perderia todo o valor probatorio.
 */
function shaDoCommit(dist) {
  for (const cwd of [path.resolve(dist), REPO_DIR]) {
    try {
      return execFileSync("git", ["rev-parse", "--short", "HEAD"], {
        cwd,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      }).trim();
    } catch {
      continue;
    }
  }
  return "desconhecido";
}

// --- Orquestracao -----------------------------------------------------------

async function main() {
  const args = lerArgs();
  exigirBancoDescartavel(args.url);

  const dados = {};
  for (const nome of ["constitutional", "thematic", "distractors"]) {
    dados[nome] = JSON.parse(
      readFileSync(path.join(BENCH_DIR, "datasets", `${nome}.json`), "utf8")
    );
  }

  // `pg` vive no node_modules do PLUGIN, nao no do bench. Resolver a partir
  // do package.json ao lado do dist/ faz o mesmo caminho valer tanto para o
  // build atual quanto para o build do worktree pre-port (D12).
  const raizDoPlugin = path.resolve(args.dist, "..");
  const requireDoPlugin = createRequire(path.join(raizDoPlugin, "package.json"));
  const pg = requireDoPlugin("pg");
  const config = {
    databaseUrl: args.url,
    autoRecall: true,
    autoCapture: false,
    rulesCacheTtlMs: 0, // sem cache: cada turno mede uma consulta de verdade
    rulesTimeoutMs: 2000,
    recallMaxResults: 5,
    // O build atual recebe o embedder fake por `__testing.setEmbedder`. O
    // build pre-port nao tem esse gancho e se inicializa sozinho, entao
    // apontamos o embedding para um endereco morto em loopback: o benchmark
    // fica hermetico (sem rede, sem custo e sem depender do 429 de um
    // terceiro) e o caminho sem vetor e justamente o comportamento pre-port
    // que queremos medir.
    embedding: {
      baseUrl: "http://127.0.0.1:9/embeddings",
      apiKey: "bench-sem-rede",
    },
  };
  const pool = criarPool(pg, args.url);
  const embedder = criarEmbedderFake();

  const resultado = {
    runner: "openclaw",
    rotulo: args.rotulo,
    // Relativo ao repositorio: o resultado versionado nao carrega a home de ninguem.
    dist: path.relative(REPO_DIR, path.resolve(args.dist)) || ".",
    commit: shaDoCommit(args.dist),
    timestamp: new Date().toISOString(),
    metrics: {},
  };

  const injecao = await medirInjecao(args.dist, config, pool, embedder, dados);
  resultado.bloco_de_regras = injecao.amostra.prependSystemContext;
  resultado.prompt_builder = injecao.amostra.promptBuilder;
  delete injecao.amostra;
  Object.assign(resultado.metrics, injecao);
  resultado.metrics.M3 = await medirFailSafe(args.dist, config, pool, embedder);
  resultado.metrics.M4 = await medirRecuperacao(
    args.dist,
    config,
    pool,
    embedder,
    dados
  );

  await pool.end();

  const texto = JSON.stringify(resultado, null, 2);
  if (args.saida) {
    writeFileSync(args.saida, texto + "\n", "utf8");
    console.log(`[run_openclaw] resultado em ${args.saida}`);
  } else {
    console.log(texto);
  }
}

main().then(
  () => process.exit(0),
  (err) => {
    console.error("[run_openclaw] falhou:", err);
    process.exit(1);
  }
);
