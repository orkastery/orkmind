"""Agregacao das metricas do benchmark e geracao do report publicavel.

Recebe os JSONs crus produzidos por `run_hermes.py` e `run_openclaw.mjs`
(mesmo schema) e produz:

  1. o agregado `bench/results/latest.json`, com as tres rodadas lado a lado
     (Hermes atual, OpenClaw atual, OpenClaw pre-port) e o M7 calculado;
  2. o report em markdown, com num, den e taxa em toda metrica de fracao -
     nunca so percentual, porque percentual sem denominador esconde o
     tamanho da amostra;
  3. a secao de resultados do README raiz, derivada do proprio agregado.

Regra de honestidade adotada aqui: nenhuma tabela deste report e digitada a
mao. Se um numero aparece no README, ele saiu de `latest.json`, que saiu de
uma rodada real. Ver `bench/README.md` para o que o benchmark NAO prova.

Uso direto (sem rodar o benchmark de novo, so regerando os textos):

    python bench/harness/metrics.py --latest bench/results/latest.json \
        --report-md /tmp/report.md --readme README.md
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BENCH_DIR.parent

# Inicio do bloco canonico de regras. E o mesmo texto nos dois runners: e
# justamente essa igualdade literal que o M7 mede.
MARCADOR_DE_REGRAS = "## REGRAS MANDATORIAS"

# Nucleo do guardrail anti-destruicao. Os nomes das ferramentas diferem entre
# Hermes (`orkmind_recall`) e OpenClaw (`memory_recall`), entao a verificacao
# cruzada usa a frase que carrega a garantia, nao o paragrafo inteiro.
NUCLEO_DO_GUARDRAIL = (
    "NAO deve compactar, resumir destrutivamente, deletar ou sobrescrever "
    "memorias para abrir espaco"
)

# Rotulos das rodadas na ordem em que aparecem no report: o "antes" primeiro,
# porque a tabela existe para mostrar o delta.
ORDEM_DAS_RODADAS = ("openclaw_pre_port", "openclaw", "hermes")
TITULOS = {
    "openclaw_pre_port": "OpenClaw pre-port (antes)",
    "openclaw": "OpenClaw atual (depois)",
    "hermes": "Hermes atual",
}


# --- Utilitarios de formatacao ----------------------------------------------


def taxa(num: float, den: float) -> float:
    return round(num / den, 4) if den else 0.0


def _fracao(metrica: dict[str, Any] | None) -> str:
    """`num/den (taxa)` - o formato exigido pelo plano: nunca so percentual."""
    if not metrica:
        return "n/d"
    num = metrica.get("num", 0)
    den = metrica.get("den", 0)
    return f"{num}/{den} ({taxa(num, den):.2f})"


def _num(valor: Any, casas: int = 2) -> str:
    if valor is None:
        return "n/d"
    if isinstance(valor, (int, float)):
        return f"{valor:.{casas}f}" if isinstance(valor, float) else str(valor)
    return str(valor)


def _sim_nao(valor: bool | None) -> str:
    if valor is None:
        return "n/d"
    return "sim" if valor else "nao"


# --- M7: fidelidade cruzada Hermes vs OpenClaw ------------------------------


def bloco_canonico(texto: str | None) -> str:
    """Extrai o bloco de regras de um prompt capturado.

    O OpenClaw injeta guardrail + regras num unico `prependSystemContext`,
    enquanto o Hermes devolve o bloco de regras puro e o guardrail em separado.
    Cortar no marcador comum e o que torna os dois comparaveis sem inventar
    equivalencia onde nao ha.
    """
    if not texto:
        return ""
    posicao = texto.find(MARCADOR_DE_REGRAS)
    return (texto[posicao:] if posicao >= 0 else texto).strip()


def diff_de_chars(a: str, b: str) -> int:
    """Chars que nao casam entre os dois blocos (insercoes + remocoes)."""
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    iguais = sum(bloco.size for bloco in matcher.get_matching_blocks())
    return (len(a) - iguais) + (len(b) - iguais)


def calcular_m7(hermes: dict | None, openclaw: dict | None) -> dict[str, Any]:
    """M7: o mesmo contrato textual chega aos dois harnesses?"""
    if not hermes or not openclaw:
        return {
            "blocos_equivalentes": False,
            "diff_chars": 0,
            "nota": "M7 exige as duas rodadas atuais; uma delas nao rodou.",
        }

    bloco_h = bloco_canonico(hermes.get("bloco_de_regras"))
    bloco_o = bloco_canonico(openclaw.get("bloco_de_regras"))
    diff = diff_de_chars(bloco_h, bloco_o)

    # O guardrail e injetado por caminhos diferentes: no Hermes ele vive em
    # `_GUARDRAIL_ANTI_DESTRUICAO`, no OpenClaw ele sai no promptBuilder e no
    # prependSystemContext. Procuramos nos dois lugares de cada lado.
    texto_h = " ".join(
        filter(None, [hermes.get("guardrail"), hermes.get("bloco_de_regras")])
    )
    texto_o = " ".join(
        filter(None, [openclaw.get("prompt_builder"), openclaw.get("bloco_de_regras")])
    )
    guardrail_nos_dois = (
        NUCLEO_DO_GUARDRAIL in texto_h and NUCLEO_DO_GUARDRAIL in texto_o
    )

    return {
        "blocos_equivalentes": bloco_h == bloco_o and bool(bloco_h),
        "diff_chars": diff,
        "chars_hermes": len(bloco_h),
        "chars_openclaw": len(bloco_o),
        "guardrail_nos_dois": guardrail_nos_dois,
        "nota": (
            "M7 compara o bloco de regras a partir de "
            f"'{MARCADOR_DE_REGRAS}', que e a parte que precisa ser literal e "
            "identica nos dois harnesses. Guardrail e alerta de fail-safe tem "
            "nomes de ferramenta proprios de cada integracao: o guardrail e "
            "conferido pela frase que carrega a garantia e o alerta e coberto "
            "pelo M3."
        ),
    }


# --- Agregacao ---------------------------------------------------------------


def agregar(
    hermes: dict | None,
    openclaw: dict | None,
    baseline: dict | None,
    banco: str = "",
) -> dict[str, Any]:
    """Monta o conteudo de `results/latest.json`."""
    rodadas: dict[str, Any] = {}
    for chave, dados in (
        ("hermes", hermes),
        ("openclaw", openclaw),
        ("openclaw_pre_port", baseline),
    ):
        if dados:
            rodadas[chave] = dados

    agregado: dict[str, Any] = {
        "gerado_em": _agora(),
        "commit": (hermes or openclaw or baseline or {}).get("commit", "desconhecido"),
        "banco": banco,
        "embedder": "fake deterministico (sha256 por bloco, L2 normalizado)",
        "rodadas": rodadas,
        "M7": calcular_m7(hermes, openclaw),
        "delta": _delta(baseline, openclaw),
    }
    agregado["report_markdown"] = render_report(agregado)
    return agregado


def _agora() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _delta(baseline: dict | None, openclaw: dict | None) -> dict[str, Any]:
    """Antes e depois do OpenClaw, que e a unica comparacao de mesmo runner.

    Comparar Hermes com OpenClaw pre-port seria comparar dois programas
    diferentes e atribuir a diferenca ao port, o que seria desonesto.
    """
    if not baseline or not openclaw:
        return {}
    resultado: dict[str, Any] = {}
    for chave in ("M1", "M2", "M3"):
        antes = baseline["metrics"].get(chave, {})
        depois = openclaw["metrics"].get(chave, {})
        resultado[chave] = {
            "antes": _fracao(antes),
            "depois": _fracao(depois),
            "delta_taxa": round(
                taxa(depois.get("num", 0), depois.get("den", 0))
                - taxa(antes.get("num", 0), antes.get("den", 0)),
                4,
            ),
        }
    return resultado


# --- Report em markdown ------------------------------------------------------


def _linha(colunas: list[str]) -> str:
    return "| " + " | ".join(colunas) + " |"


def _tabela(cabecalho: list[str], linhas: list[list[str]]) -> str:
    partes = [_linha(cabecalho), _linha(["---"] * len(cabecalho))]
    partes.extend(_linha(linha) for linha in linhas)
    return "\n".join(partes)


def _rodadas_presentes(agregado: dict) -> list[str]:
    return [c for c in ORDEM_DAS_RODADAS if c in agregado.get("rodadas", {})]


def _metrica(agregado: dict, rodada: str, chave: str) -> dict[str, Any]:
    return agregado["rodadas"][rodada].get("metrics", {}).get(chave, {}) or {}


def render_report(agregado: dict) -> str:
    """Report completo em markdown, para o terminal e para o log do ciclo."""
    presentes = _rodadas_presentes(agregado)
    linhas: list[str] = []
    add = linhas.append

    add("# Benchmark OrkMind")
    add("")
    add(f"- Gerado em: `{agregado['gerado_em']}`")
    add(f"- Banco: `{agregado.get('banco') or 'n/d'}` (descartavel, so do bench)")
    add(f"- Embedder: {agregado.get('embedder', 'fake')}")
    add("")
    add(
        "Toda fracao aparece como `num/den (taxa)`. Percentual sem "
        "denominador esconde o tamanho da amostra, entao aqui nao existe."
    )
    add("")

    add("## Rodadas medidas")
    add("")
    add(
        _tabela(
            ["Rodada", "Runner", "Commit", "Timestamp"],
            [
                [
                    TITULOS[c],
                    agregado["rodadas"][c].get("runner", "?"),
                    f"`{agregado['rodadas'][c].get('commit', '?')}`",
                    agregado["rodadas"][c].get("timestamp", "?"),
                ]
                for c in presentes
            ],
        )
    )
    add("")

    add("## M1 - Injecao constitucional (regras N1, `priority = critical`)")
    add("")
    add("Turnos em que TODAS as regras criticas apareceram literalmente no prompt.")
    add("")
    add(
        _tabela(
            ["Rodada", "num/den (taxa)"],
            [[TITULOS[c], _fracao(_metrica(agregado, c, "M1"))] for c in presentes],
        )
    )
    add("")

    add("## M2 - Regras mandatorias (`mandatory = true`)")
    add("")
    add(
        _tabela(
            ["Rodada", "num/den (taxa)", "Omitidas por budget"],
            [
                [
                    TITULOS[c],
                    _fracao(_metrica(agregado, c, "M2")),
                    str(_metrica(agregado, c, "M2").get("omitidas_por_budget", 0)),
                ]
                for c in presentes
            ],
        )
    )
    add("")

    add("## M3 - Fail-safe")
    add("")
    add(
        "Cenarios de falha do backend depois de uma carga bem-sucedida devem "
        "produzir o alerta; os casos de controle NAO devem. O acerto conta os "
        "dois sentidos, senao um plugin que alerta sempre marcaria 100%."
    )
    add("")
    add(
        _tabela(
            ["Rodada", "acertos/casos (taxa)"],
            [[TITULOS[c], _fracao(_metrica(agregado, c, "M3"))] for c in presentes],
        )
    )
    add("")
    for chave in presentes:
        casos = _metrica(agregado, chave, "M3").get("casos", [])
        if not casos:
            continue
        add(f"### M3 detalhado: {TITULOS[chave]}")
        add("")
        add(
            _tabela(
                ["Cenario", "Alerta presente", "Esperado", "Acertou"],
                [
                    [
                        caso.get("caso", "?"),
                        _sim_nao(caso.get("alerta_presente")),
                        _sim_nao(caso.get("esperado")),
                        _sim_nao(
                            caso.get("alerta_presente") == caso.get("esperado")
                        ),
                    ]
                    for caso in casos
                ],
            )
        )
        add("")

    add("## M4 - Recuperacao (estrutura validada, numero adiado)")
    add("")
    add(
        _tabela(
            ["Rodada", "recall@5", "precision@5", "Sanidade do pipeline", "Embedder"],
            [
                [
                    TITULOS[c],
                    _num(_metrica(agregado, c, "M4").get("recall_at_5"), 4),
                    _num(_metrica(agregado, c, "M4").get("precision_at_5"), 4),
                    _fracao(_metrica(agregado, c, "M4").get("sanidade_pipeline")),
                    str(_metrica(agregado, c, "M4").get("embedder", "n/d")),
                ]
                for c in presentes
            ],
        )
    )
    add("")
    add(
        "**Leia isto antes de citar M4:** com embedder fake, `recall@5` e "
        "`precision@5` NAO medem qualidade semantica. O embedder e um hash: "
        "textos de mesmo sentido geram vetores nao relacionados, entao o "
        "numero honesto e proximo de zero e isso e esperado. O que este "
        "ciclo valida e a ESTRUTURA, medida por `sanidade_pipeline` "
        "(consultar pelo texto exato de uma memoria tem que traze-la no "
        "top-5: se isso nao der 1.00, o pipeline esta quebrado). O numero "
        "com embedder real fica para o proximo ciclo."
    )
    add("")

    add("## M5 - Custo por turno")
    add("")
    add(
        _tabela(
            ["Rodada", "Chars injetados (medio)", "Latencia p50 (ms)"],
            [
                [
                    TITULOS[c],
                    _num(_metrica(agregado, c, "M5").get("chars_injetados_medio")),
                    _num(_metrica(agregado, c, "M5").get("latencia_ms_p50"), 3),
                ]
                for c in presentes
            ],
        )
    )
    add("")

    add("## M6 - Diluicao ao longo da sessao")
    add("")
    add(
        "Fatia do prompt do turno ocupada pelo bloco de regras, nos turnos 1, "
        "10, 20 e 40 de uma sessao longa sintetica. Mede o quanto o contexto "
        "recuperado dilui a governanca: cair para perto de zero significa que "
        "as regras viraram rodape de um prompt tomado por outra coisa. Zero "
        "no pre-port tem outra causa: nao ha bloco de regras nenhum."
    )
    add("")
    add(
        _tabela(
            ["Rodada", "Turno 1", "Turno 10", "Turno 20", "Turno 40"],
            [
                [
                    TITULOS[c],
                    _num(_metrica(agregado, c, "M6").get("turno_1"), 4),
                    _num(_metrica(agregado, c, "M6").get("turno_10"), 4),
                    _num(_metrica(agregado, c, "M6").get("turno_20"), 4),
                    _num(_metrica(agregado, c, "M6").get("turno_40"), 4),
                ]
                for c in presentes
            ],
        )
    )
    add("")

    m7 = agregado.get("M7", {})
    add("## M7 - Fidelidade cruzada (Hermes vs OpenClaw)")
    add("")
    add(
        _tabela(
            ["Item", "Valor"],
            [
                ["Blocos equivalentes", _sim_nao(m7.get("blocos_equivalentes"))],
                ["Chars divergentes", str(m7.get("diff_chars", "n/d"))],
                ["Chars do bloco (Hermes)", str(m7.get("chars_hermes", "n/d"))],
                ["Chars do bloco (OpenClaw)", str(m7.get("chars_openclaw", "n/d"))],
                ["Guardrail nos dois", _sim_nao(m7.get("guardrail_nos_dois"))],
            ],
        )
    )
    add("")
    if m7.get("nota"):
        add(m7["nota"])
        add("")

    delta = agregado.get("delta") or {}
    if delta:
        add("## Antes e depois do port (mesmo runner, mesmo banco)")
        add("")
        add(
            _tabela(
                ["Metrica", "Antes (pre-port)", "Depois (atual)", "Delta da taxa"],
                [
                    [
                        chave,
                        valores["antes"],
                        valores["depois"],
                        f"{valores['delta_taxa']:+.2f}",
                    ]
                    for chave, valores in delta.items()
                ],
            )
        )
        add("")
        add(
            "So o OpenClaw aparece nesta tabela. Comparar Hermes com OpenClaw "
            "pre-port seria comparar dois programas diferentes e creditar a "
            "diferenca ao port."
        )
        add("")

    add("## O que este benchmark NAO prova")
    add("")
    add(
        "- Nao prova qualidade de recuperacao semantica: o embedder e fake "
        "(ver M4)."
    )
    add(
        "- Nao prova obediencia do modelo: mede o que chega ao prompt, nao o "
        "que o modelo faz com isso."
    )
    add(
        "- Nao e um benchmark de memoria de longo prazo tipo LoCoMo: isso "
        "esta no roadmap."
    )
    add(
        "- Nao mede sessao viva: roda headless, com banco descartavel e "
        "datasets versionados."
    )
    return "\n".join(linhas) + "\n"


# --- Secao do README raiz ----------------------------------------------------

MARCA_INICIO = "<!-- BENCH:INICIO (gerado por bench/run.py; nao editar a mao) -->"
MARCA_FIM = "<!-- BENCH:FIM -->"


def secao_readme(agregado: dict) -> str:
    """Secao de resultados do README raiz, derivada de `latest.json`.

    Em ingles porque o README raiz e o artefato publico e ja esta em ingles;
    misturar idiomas dentro do mesmo arquivo publico seria pior do que
    manter a coerencia.
    """
    rodadas = agregado.get("rodadas", {})
    hermes = rodadas.get("hermes", {}).get("metrics", {})
    openclaw = rodadas.get("openclaw", {}).get("metrics", {})
    pre = rodadas.get("openclaw_pre_port", {}).get("metrics", {})
    m7 = agregado.get("M7", {})

    def frac(metrics: dict, chave: str) -> str:
        return _fracao(metrics.get(chave, {}))

    linhas = [
        MARCA_INICIO,
        "## Benchmark",
        "",
        "Numbers below are generated by `python bench/run.py` and read from",
        "`bench/results/latest.json`. They are never typed by hand. Every",
        "fraction is reported as `hits/total (rate)`, never as a bare",
        "percentage. See `bench/README.md` for the method and for what this",
        "benchmark does **not** prove.",
        "",
        f"Last run: `{agregado.get('gerado_em', '?')}` "
        f"at commit `{agregado.get('commit', '?')}`.",
        "",
        _tabela(
            ["Metric", "Hermes", "OpenClaw (now)", "OpenClaw (pre-port)"],
            [
                [
                    "M1 constitutional injection",
                    frac(hermes, "M1"),
                    frac(openclaw, "M1"),
                    frac(pre, "M1"),
                ],
                [
                    "M2 mandatory rules",
                    frac(hermes, "M2"),
                    frac(openclaw, "M2"),
                    frac(pre, "M2"),
                ],
                [
                    "M3 fail-safe scenarios",
                    frac(hermes, "M3"),
                    frac(openclaw, "M3"),
                    frac(pre, "M3"),
                ],
                [
                    "M4 pipeline sanity (fake embedder)",
                    _fracao(hermes.get("M4", {}).get("sanidade_pipeline")),
                    _fracao(openclaw.get("M4", {}).get("sanidade_pipeline")),
                    _fracao(pre.get("M4", {}).get("sanidade_pipeline")),
                ],
                [
                    "M5 injected chars per turn",
                    _num(hermes.get("M5", {}).get("chars_injetados_medio")),
                    _num(openclaw.get("M5", {}).get("chars_injetados_medio")),
                    _num(pre.get("M5", {}).get("chars_injetados_medio")),
                ],
            ],
        ),
        "",
        "M4 recall/precision are deliberately omitted from this table: with a",
        "fake embedder they measure nothing about semantic quality. What is",
        "validated here is the pipeline structure. Real-embedder numbers are",
        "scheduled for the next cycle.",
        "",
        "M7 (cross-harness fidelity): rules block "
        + ("identical" if m7.get("blocos_equivalentes") else "NOT identical")
        + f", {m7.get('diff_chars', '?')} differing chars, guardrail present in "
        + ("both" if m7.get("guardrail_nos_dois") else "not both")
        + ".",
        "",
        "### Support matrix per integration",
        "",
        _tabela(
            [
                "Integration",
                "Unconditional rule injection",
                "Fail-safe alert",
                "Auto-recall",
                "Covered by benchmark",
            ],
            [
                ["Hermes plugin", "yes", "yes", "yes", "yes (M1-M7)"],
                ["OpenClaw plugin", "yes", "yes", "yes", "yes (M1-M7)"],
                [
                    "MCP server",
                    "n/a (no prompt build)",
                    "n/a",
                    "on demand via `orkmind_get_rules` / recall tools",
                    "no",
                ],
                [
                    "CLI",
                    "n/a (no prompt build)",
                    "n/a",
                    "manual (`orkmind search`)",
                    "no",
                ],
            ],
        ),
        "",
        "MCP and CLI never build a model prompt, so unconditional injection",
        "does not apply to them: they expose the same rules on demand. Only",
        "the two prompt-building integrations can guarantee that governance",
        "reaches the model on every turn, and only those are benchmarked.",
        MARCA_FIM,
    ]
    return "\n".join(linhas) + "\n"


def atualizar_readme(caminho: Path, secao: str) -> bool:
    """Troca o trecho entre as marcas. Cria antes da secao de licenca se nao houver.

    Devolve True se o arquivo mudou.
    """
    texto = caminho.read_text(encoding="utf-8")
    inicio = texto.find(MARCA_INICIO)
    fim = texto.find(MARCA_FIM)
    if inicio >= 0 and fim > inicio:
        novo = texto[:inicio] + secao.rstrip("\n") + texto[fim + len(MARCA_FIM) :]
    else:
        ancora = texto.find("## License")
        if ancora < 0:
            novo = texto.rstrip("\n") + "\n\n" + secao
        else:
            novo = texto[:ancora] + secao + "\n" + texto[ancora:]
    if novo == texto:
        return False
    caminho.write_text(novo, encoding="utf-8")
    return True


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--latest",
        default=str(BENCH_DIR / "results" / "latest.json"),
        help="agregado ja gerado, de onde os textos serao rederivados",
    )
    parser.add_argument("--report-md", help="arquivo para gravar o report completo")
    parser.add_argument("--readme", help="README a atualizar entre as marcas do bench")
    args = parser.parse_args()

    caminho = Path(args.latest)
    if not caminho.exists():
        print(
            f"RECUSADO: {caminho} nao existe. Rode `python bench/run.py` antes.",
            file=sys.stderr,
        )
        return 1
    agregado = json.loads(caminho.read_text(encoding="utf-8"))

    report = agregado.get("report_markdown") or render_report(agregado)
    if args.report_md:
        Path(args.report_md).write_text(report, encoding="utf-8")
        print(f"[metrics] report em {args.report_md}")
    else:
        print(report)

    if args.readme:
        alvo = Path(args.readme)
        mudou = atualizar_readme(alvo, secao_readme(agregado))
        print(f"[metrics] {alvo}: {'atualizado' if mudou else 'sem mudanca'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
