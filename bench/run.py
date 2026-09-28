#!/usr/bin/env python3
"""Orquestrador do benchmark: seed -> rodadas -> agregacao -> report.

Roda, nesta ordem:

  1. `harness/seed.py`     popula o banco DESCARTAVEL do bench;
  2. `harness/run_hermes.py`      rodada do plugin Hermes (Python);
  3. `harness/run_openclaw.mjs`   rodada do plugin OpenClaw (dist/ atual);
  4. baseline pre-port     lido de `results/baseline-pre-port.json`, ou
                           remedido se `--dist-pre-port` for passado;
  5. `harness/metrics.py`  agrega em `results/latest.json` e imprime o report.

A URL do banco passa pela guarda de `tests/conftest.py`, reusada por
`seed.py`. Se a URL nao tiver marcador de teste no nome, nada roda e o exit
code e 2. Isso e deliberado: o seed APAGA as linhas do banco apontado, e em
29/08/2026 uma execucao sem guarda destruiu 18 entries de producao.

Uso:
    python bench/run.py
    python bench/run.py --pular-seed
    python bench/run.py --atualizar-readme
    python bench/run.py --dist-pre-port /tmp/orkmind-preport/.../memory-orkmind/dist

Exit codes:
    0  benchmark completo
    1  alguma etapa falhou
    2  guarda recusou o banco
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCH_DIR.parent
HARNESS = BENCH_DIR / "harness"
RESULTS = BENCH_DIR / "results"

sys.path.insert(0, str(HARNESS))

import metrics as metrics_mod  # noqa: E402
import seed as seed_mod  # noqa: E402

DIST_PADRAO = REPO_DIR / "integrations" / "openclaw" / "memory-orkmind" / "dist"
TIMEOUT_POR_ETAPA_S = 900


def _titulo(texto: str) -> None:
    print(f"\n=== {texto} ===", flush=True)


def _executar(comando: list[str], etapa: str) -> None:
    """Roda uma etapa herdando stdout/stderr. Levanta se falhar."""
    inicio = time.perf_counter()
    concluido = subprocess.run(comando, cwd=REPO_DIR, timeout=TIMEOUT_POR_ETAPA_S)
    duracao = time.perf_counter() - inicio
    if concluido.returncode == 2:
        # Codigo 2 vem da guarda de banco. Propagar sem traduzir: o chamador
        # precisa distinguir "guarda recusou" de qualquer outra falha.
        raise SystemExit(2)
    if concluido.returncode != 0:
        raise RuntimeError(f"{etapa} falhou (exit {concluido.returncode})")
    print(f"[run] {etapa} concluida em {duracao:.1f}s", flush=True)


def _ler_json(caminho: Path) -> dict:
    return json.loads(caminho.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="DSN do banco descartavel do benchmark")
    parser.add_argument(
        "--pular-seed",
        action="store_true",
        help="reaproveitar o banco ja semeado (mais rapido, menos hermetico)",
    )
    parser.add_argument(
        "--dist",
        default=str(DIST_PADRAO),
        help="dist/ compilado do plugin OpenClaw a medir",
    )
    parser.add_argument(
        "--dist-pre-port",
        help=(
            "se passado, remede o baseline a partir deste dist/ em vez de ler "
            "results/baseline-pre-port.json"
        ),
    )
    parser.add_argument(
        "--sem-baseline",
        action="store_true",
        help="rodar sem o antes (report so com as rodadas atuais)",
    )
    parser.add_argument(
        "--saida",
        default=str(RESULTS / "latest.json"),
        help="agregado de saida",
    )
    parser.add_argument("--report-md", help="grava o report em markdown neste arquivo")
    parser.add_argument(
        "--atualizar-readme",
        action="store_true",
        help="reescreve a secao de resultados do README raiz a partir do agregado",
    )
    args = parser.parse_args()

    # A guarda roda ANTES de qualquer etapa: se o banco nao for descartavel,
    # o benchmark nao comeca.
    url = seed_mod.resolver_url(args.url)
    banco = url.rsplit("/", 1)[-1]
    print(f"[run] banco do benchmark: {banco}")
    print(f"[run] dist do OpenClaw: {args.dist}")

    dist = Path(args.dist)
    if not (dist / "index.js").exists():
        print(
            f"RECUSADO: {dist / 'index.js'} nao existe. Rode o build do plugin "
            "(npm ci && npm run build em integrations/openclaw/memory-orkmind).",
            file=sys.stderr,
        )
        return 1

    node = shutil.which("node")
    if node is None:
        print("RECUSADO: `node` nao encontrado no PATH.", file=sys.stderr)
        return 1

    temporario = Path(tempfile.mkdtemp(prefix="orkmind-bench-"))
    try:
        if args.pular_seed:
            print("[run] seed pulado por --pular-seed")
        else:
            _titulo("1/4 seed do banco descartavel")
            _executar(
                [sys.executable, str(HARNESS / "seed.py"), "--url", url],
                "seed",
            )

        _titulo("2/4 rodada Hermes")
        saida_hermes = temporario / "hermes.json"
        _executar(
            [
                sys.executable,
                str(HARNESS / "run_hermes.py"),
                "--url",
                url,
                "--saida",
                str(saida_hermes),
            ],
            "rodada Hermes",
        )

        _titulo("3/4 rodada OpenClaw")
        saida_openclaw = temporario / "openclaw.json"
        _executar(
            [
                node,
                str(HARNESS / "run_openclaw.mjs"),
                "--url",
                url,
                "--dist",
                str(dist),
                "--rotulo",
                "openclaw",
                "--saida",
                str(saida_openclaw),
            ],
            "rodada OpenClaw",
        )

        _titulo("4/4 baseline pre-port")
        baseline: dict | None = None
        arquivo_baseline = RESULTS / "baseline-pre-port.json"
        if args.sem_baseline:
            print("[run] baseline omitido por --sem-baseline")
        elif args.dist_pre_port:
            saida_baseline = temporario / "pre-port.json"
            _executar(
                [
                    node,
                    str(HARNESS / "run_openclaw.mjs"),
                    "--url",
                    url,
                    "--dist",
                    args.dist_pre_port,
                    "--rotulo",
                    "openclaw-pre-port",
                    "--saida",
                    str(saida_baseline),
                ],
                "baseline pre-port",
            )
            baseline = _ler_json(saida_baseline)
            arquivo_baseline.write_text(
                json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"[run] baseline regravado em {arquivo_baseline}")
        elif arquivo_baseline.exists():
            baseline = _ler_json(arquivo_baseline)
            print(
                f"[run] baseline lido de {arquivo_baseline} "
                f"(commit {baseline.get('commit', '?')})"
            )
        else:
            print(
                "[run] AVISO: baseline ausente; o report sai sem a coluna do "
                "antes. Gere com --dist-pre-port apontando para o build do "
                "worktree pre-port."
            )

        hermes = _ler_json(saida_hermes)
        openclaw = _ler_json(saida_openclaw)
    finally:
        shutil.rmtree(temporario, ignore_errors=True)

    _titulo("agregacao")
    agregado = metrics_mod.agregar(hermes, openclaw, baseline, banco=banco)
    caminho_saida = Path(args.saida)
    caminho_saida.parent.mkdir(parents=True, exist_ok=True)
    caminho_saida.write_text(
        json.dumps(agregado, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[run] agregado em {caminho_saida}")

    report = agregado["report_markdown"]
    if args.report_md:
        Path(args.report_md).write_text(report, encoding="utf-8")
        print(f"[run] report em {args.report_md}")

    if args.atualizar_readme:
        readme = REPO_DIR / "README.md"
        mudou = metrics_mod.atualizar_readme(readme, metrics_mod.secao_readme(agregado))
        print(f"[run] README.md: {'atualizado' if mudou else 'sem mudanca'}")

    print()
    print(report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as erro:  # noqa: BLE001
        print(f"[run] benchmark falhou: {erro}", file=sys.stderr)
        sys.exit(1)
