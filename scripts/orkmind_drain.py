#!/usr/bin/env python
"""Drainer da spool do OrkMind: fecha a conta de toda intencao de memoria.

Este e o processo que torna a solucao "sempre gravar" real. Ele e
EXTERNO ao agente: nao depende do toolset que um cron recebeu, nem de o
Hermes estar vivo. Enquanto houver disco com itens em pending/, o
conteudo acaba virando memoria assim que o backend voltar.

Fluxo por item:

  1. Lookup por content_hash. Se ja existe entry, o item e fechado em
     done/ com o entry_id REAL existente (status duplicate). Zero
     duplicacao, e nenhum id inventado.
  2. Se nao existe, grava. Ordem de backends: API local
     (POST /entries) e, se ela estiver fora, o CLI orkmind com
     --dedupe --json.
  3. Sucesso: entry_id real gravado no .json e item movido para done/.
  4. Falha transitoria: attempts++ e backoff progressivo, item fica em
     pending/. Esgotadas as tentativas, vai para failed/ e aguarda
     revisao humana. Nada e apagado em nenhum caminho.

Uso:

    orkmind_drain.py --once                 uma passada (ideal para cron)
    orkmind_drain.py --watch --interval 60  laco continuo
    orkmind_drain.py --status               resumo da fila, nao grava
    orkmind_drain.py --backfill ~/.hermes/reports --collection content \\
        --tags '{"project": ["possibilidades-ia"]}'
                                            estagia orfaos legados

Ambiente:

    ORKMIND_SPOOL_DIR    raiz da spool (default ~/.hermes/orkmind-spool)
    ORKMIND_API_URL      base da API local (default http://127.0.0.1:8077)
    ORKMIND_API_TOKEN    token Bearer da API local
    ORKMIND_DATABASE_URL DSN usado pelo fallback CLI
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from orkmind.spool import (  # noqa: E402
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    STATES,
    SpoolItem,
    compute_content_hash,
    default_spool_dir,
    ensure_spool_dirs,
    iter_items,
    move_item,
    read_content,
    stage_item,
    write_item,
)

logger = logging.getLogger("orkmind.drainer")

DEFAULT_API_URL = "http://127.0.0.1:8077"

#: Caminho do CLI dentro do venv do repo. Nunca dependemos do PATH,
#: porque o cron costuma rodar com PATH minimo.
VENV_CLI = REPO_ROOT / ".venv" / "bin" / "orkmind"


def setup_logging(root: Path, verbose: bool = False) -> None:
    """Loga em drainer.log e tambem no stderr."""
    root.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(root / "drainer.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


# ---------------------------------------------------------------------------
# Backends de gravacao
# ---------------------------------------------------------------------------


class DrainError(Exception):
    """Falha transitoria: o item continua em pending para novo retry."""


class ApiBackend:
    """Gravacao via API local do OrkMind (caminho preferencial)."""

    name = "api"

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def available(self) -> bool:
        """True se a API responde /health. Nunca levanta."""
        if not self.configured:
            return False
        try:
            import httpx

            resp = httpx.get(f"{self.base_url}/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def lookup(self, content_hash: str, collection: str) -> Optional[str]:
        import httpx

        try:
            resp = httpx.get(
                f"{self.base_url}/entries/by-hash",
                params={"hash": content_hash, "collection": collection},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise DrainError(f"api lookup indisponivel: {exc}") from exc

        if resp.status_code == 404:
            return None
        if resp.status_code == 200:
            return str(resp.json().get("entry_id") or "") or None
        raise DrainError(f"api lookup HTTP {resp.status_code}: {resp.text[:200]}")

    def store(self, item: SpoolItem, content: str) -> tuple[str, str]:
        import httpx

        payload = {
            "content": content,
            "collection": item.collection,
            "tags": item.tags,
            "priority": item.priority,
            "source": item.source,
            "scope": item.scope,
            "mandatory": item.mandatory,
            "content_hash": item.content_hash,
            "metadata": item.metadata,
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/entries",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise DrainError(f"api store indisponivel: {exc}") from exc

        if resp.status_code in (200, 201):
            data = resp.json()
            entry_id = str(data.get("entry_id") or "")
            if not entry_id:
                raise DrainError("api store nao devolveu entry_id")
            return entry_id, str(data.get("status") or "created")

        # 4xx de validacao e erro permanente do item, nao vale retry
        # infinito, mas tratamos como falha normal para que o backoff e o
        # limite de tentativas levem para failed/ com o erro registrado.
        raise DrainError(f"api store HTTP {resp.status_code}: {resp.text[:300]}")


class CliBackend:
    """Fallback: CLI orkmind do venv do repo, com --dedupe --json."""

    name = "cli"

    def __init__(self, executable: Optional[Path] = None, timeout: float = 120.0) -> None:
        self.executable = Path(executable) if executable else VENV_CLI
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return self.executable.exists()

    def available(self) -> bool:
        return self.configured

    def store(self, item: SpoolItem, content: str) -> tuple[str, str]:
        cmd = [
            str(self.executable), "add",
            "--collection", item.collection,
            "--content", content,
            "--priority", item.priority,
            "--scope", item.scope,
            "--source", item.source,
            "--content-hash", item.content_hash,
            "--dedupe",
            "--json",
        ]
        if item.tags:
            cmd += ["--tags", json.dumps(item.tags, ensure_ascii=False)]
        if item.mandatory:
            cmd.append("--mandatory")

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DrainError(f"cli indisponivel: {exc}") from exc

        if proc.returncode != 0:
            raise DrainError(
                f"cli saiu com codigo {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[:300]}"
            )

        # A ultima linha nao vazia e o JSON; logs eventuais vem antes.
        linhas = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not linhas:
            raise DrainError("cli nao produziu saida")
        try:
            data = json.loads(linhas[-1])
        except json.JSONDecodeError as exc:
            raise DrainError(f"cli devolveu saida nao-JSON: {linhas[-1][:200]}") from exc

        entry_id = str(data.get("entry_id") or "")
        if not entry_id:
            raise DrainError("cli nao devolveu entry_id")
        return entry_id, str(data.get("status") or "created")


# ---------------------------------------------------------------------------
# Nucleo do drainer
# ---------------------------------------------------------------------------


class Drainer:
    """Processa a fila de saida, item a item, de forma idempotente."""

    def __init__(
        self,
        root: Path,
        api: Optional[ApiBackend] = None,
        cli: Optional[CliBackend] = None,
        dry_run: bool = False,
    ) -> None:
        self.root = ensure_spool_dirs(root)
        self.api = api
        self.cli = cli
        self.dry_run = dry_run

    # --- gravacao ---

    def _resolve_existing(self, item: SpoolItem) -> Optional[str]:
        """Consulta se o hash ja virou memoria. None se nao souber dizer.

        Um erro de lookup nao e fatal: seguimos para a gravacao, que e
        idempotente do outro lado (API devolve duplicate, CLI usa
        --dedupe, e o indice unico e a rede de seguranca final).
        """
        if self.api and self.api.configured:
            try:
                return self.api.lookup(item.content_hash, item.collection)
            except DrainError as exc:
                logger.debug("lookup via api falhou para %s: %s", item.item_id, exc)
        return None

    def _store(self, item: SpoolItem, content: str) -> tuple[str, str, str]:
        """Grava pelo primeiro backend que funcionar.

        Devolve (entry_id, status, backend). Levanta DrainError se todos
        falharem, e ai o item permanece em pending para novo retry.
        """
        erros: list[str] = []

        for backend in (self.api, self.cli):
            if backend is None or not backend.configured:
                continue
            try:
                entry_id, status = backend.store(item, content)
                return entry_id, status, backend.name
            except DrainError as exc:
                erros.append(f"{backend.name}: {exc}")
                logger.warning("backend %s falhou para %s: %s", backend.name, item.item_id, exc)

        if not erros:
            raise DrainError("nenhum backend configurado (API sem token e CLI ausente)")
        raise DrainError(" | ".join(erros))

    # --- processamento ---

    def process_item(self, item: SpoolItem) -> str:
        """Processa um item. Devolve o desfecho para o relatorio."""
        try:
            content = read_content(item, self.root, STATE_PENDING)
        except OSError as exc:
            item.schedule_retry(f"conteudo ilegivel: {exc}")
            self._persist_failure(item)
            return "erro"

        # Integridade: o hash tem que descrever o conteudo em disco. Se
        # divergir, o item esta corrompido e nao pode ser gravado sob uma
        # chave de dedupe errada. Vai direto para failed/, sem retry.
        real_hash = compute_content_hash(content)
        if item.content_hash != real_hash:
            item.last_error = (
                f"content_hash nao confere com o conteudo em disco "
                f"(json={item.content_hash} arquivo={real_hash})"
            )
            item.result = "corrompido"
            logger.error("item %s corrompido: %s", item.item_id, item.last_error)
            if not self.dry_run:
                move_item(item, self.root, STATE_FAILED)
            return "corrompido"

        if self.dry_run:
            logger.info(
                "[dry-run] item %s colecao=%s hash=%s",
                item.item_id, item.collection, item.content_hash[:12],
            )
            return "dry-run"

        # 1. Ja existe? Fecha com o id real, sem inserir de novo.
        existing = self._resolve_existing(item)
        if existing:
            item.entry_id = existing
            item.result = "duplicate"
            item.backend = "lookup"
            item.last_error = None
            move_item(item, self.root, STATE_DONE)
            logger.info(
                "item %s ja existia: entry_id=%s (duplicate)", item.item_id, existing
            )
            return "duplicate"

        # 2. Grava.
        try:
            entry_id, status, backend = self._store(item, content)
        except DrainError as exc:
            item.schedule_retry(str(exc))
            self._persist_failure(item)
            return "falha"

        item.entry_id = entry_id
        item.result = status
        item.backend = backend
        item.last_error = None
        item.last_attempt_at = None
        move_item(item, self.root, STATE_DONE)
        logger.info(
            "item %s gravado via %s: entry_id=%s status=%s",
            item.item_id, backend, entry_id, status,
        )
        return status

    def _persist_failure(self, item: SpoolItem) -> None:
        """Grava a tentativa falha; manda para failed/ se esgotou."""
        if self.dry_run:
            return
        if item.exhausted:
            item.result = "failed"
            logger.error(
                "item %s esgotou %s tentativas, movido para failed/: %s",
                item.item_id, item.max_attempts, item.last_error,
            )
            move_item(item, self.root, STATE_FAILED)
        else:
            write_item(item, self.root, STATE_PENDING)
            logger.warning(
                "item %s falhou (tentativa %s/%s), novo retry em %s: %s",
                item.item_id, item.attempts, item.max_attempts,
                item.next_attempt_at, item.last_error,
            )

    def drain_once(self) -> dict[str, int]:
        """Uma passada completa por pending/. Devolve o placar."""
        placar = {
            "total": 0, "created": 0, "duplicate": 0,
            "falha": 0, "adiado": 0, "corrompido": 0, "erro": 0, "dry-run": 0,
        }
        for item in iter_items(self.root, STATE_PENDING):
            placar["total"] += 1
            if not item.is_due():
                placar["adiado"] += 1
                logger.debug(
                    "item %s em backoff ate %s", item.item_id, item.next_attempt_at
                )
                continue
            desfecho = self.process_item(item)
            placar[desfecho] = placar.get(desfecho, 0) + 1
        return placar

    def watch(self, interval: float) -> None:
        """Laco continuo: drena, dorme, repete. Sai limpo no Ctrl-C."""
        logger.info("drainer em modo watch (intervalo %ss), spool=%s", interval, self.root)
        try:
            while True:
                placar = self.drain_once()
                if placar["total"]:
                    logger.info("passada: %s", placar)
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("drainer encerrado pelo usuario")


# ---------------------------------------------------------------------------
# Backfill de orfaos legados
# ---------------------------------------------------------------------------


def spool_hashes(root: Path) -> dict[str, str]:
    """Mapa content_hash -> estado, considerando TODOS os estados.

    Serve para o backfill nao reestagiar o que ja esta na fila ou ja foi
    gravado, mantendo a operacao repetivel sem efeito colateral.
    """
    conhecidos: dict[str, str] = {}
    for state in STATES:
        for item in iter_items(root, state):
            if item.content_hash:
                conhecidos.setdefault(item.content_hash, state)
    return conhecidos


def backfill(
    source_dir: Path,
    root: Path,
    collection: str = "content",
    tags: Optional[dict[str, list[str]]] = None,
    priority: str = "medium",
    source: str = "agent",
    pattern: str = "*.md",
    dry_run: bool = False,
) -> list[SpoolItem]:
    """Estagia arquivos orfaos legados na spool (opcao migratoria).

    Nao apaga nem move o arquivo de origem: o relatorio continua sendo
    artefato de leitura humana em ~/.hermes/reports/, e a spool passa a
    carregar a intencao de memoria. Roda quantas vezes for preciso: o
    que ja esta na fila (por hash) e ignorado.
    """
    source_dir = Path(source_dir).expanduser()
    if not source_dir.is_dir():
        logger.error("diretorio de backfill inexistente: %s", source_dir)
        return []

    ensure_spool_dirs(root)
    conhecidos = spool_hashes(root)
    estagiados: list[SpoolItem] = []

    for arquivo in sorted(source_dir.glob(pattern)):
        if not arquivo.is_file():
            continue
        try:
            conteudo = arquivo.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("backfill ignorou %s: %s", arquivo.name, exc)
            continue
        if not conteudo.strip():
            logger.warning("backfill ignorou %s: arquivo vazio", arquivo.name)
            continue

        conteudo_hash = compute_content_hash(conteudo)
        if conteudo_hash in conhecidos:
            logger.info(
                "backfill pulou %s: hash ja na spool (%s)",
                arquivo.name, conhecidos[conteudo_hash],
            )
            continue

        if dry_run:
            logger.info("[dry-run] backfill estagiaria %s (hash %s)",
                        arquivo.name, conteudo_hash[:12])
            continue

        item = stage_item(
            content=conteudo,
            collection=collection,
            tags=tags or {},
            priority=priority,
            source=source,
            label=arquivo.stem,
            origin=str(arquivo),
            metadata={"backfill": True, "arquivo_origem": str(arquivo)},
            root=root,
        )
        conhecidos[conteudo_hash] = STATE_PENDING
        estagiados.append(item)
        logger.info("backfill estagiou %s como %s", arquivo.name, item.item_id)

    return estagiados


def status_report(root: Path) -> dict[str, Any]:
    """Resumo da fila, sem gravar nada."""
    resumo: dict[str, Any] = {"spool": str(root)}
    for state in STATES:
        itens = list(iter_items(root, state))
        resumo[state] = len(itens)
        if state == STATE_PENDING and itens:
            resumo["pending_itens"] = [
                {
                    "item_id": i.item_id,
                    "collection": i.collection,
                    "attempts": i.attempts,
                    "next_attempt_at": i.next_attempt_at,
                    "last_error": i.last_error,
                }
                for i in itens
            ]
    return resumo


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orkmind_drain",
        description="Drena a fila de saida do OrkMind gravando a memoria pendente.",
    )
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--once", action="store_true", help="Uma passada e sai (cron).")
    modo.add_argument("--watch", action="store_true", help="Laco continuo.")
    modo.add_argument("--status", action="store_true", help="Mostra a fila e sai.")

    parser.add_argument("--interval", type=float, default=60.0,
                        help="Segundos entre passadas no modo --watch.")
    parser.add_argument("--spool-dir", default=None, help="Raiz da spool.")
    parser.add_argument("--api-url", default=None, help="Base da API local.")
    parser.add_argument("--cli-path", default=None, help="Caminho do executavel orkmind.")
    parser.add_argument("--no-api", action="store_true", help="Ignora a API, usa so o CLI.")
    parser.add_argument("--no-cli", action="store_true", help="Ignora o CLI, usa so a API.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nao grava nada, apenas relata o que faria.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log detalhado.")

    grupo = parser.add_argument_group("backfill de orfaos legados")
    grupo.add_argument("--backfill", default=None,
                       help="Diretorio a varrer, estagiando arquivos na spool.")
    grupo.add_argument("--pattern", default="*.md", help="Glob do backfill (default *.md).")
    grupo.add_argument("--collection", default="content", help="Colecao alvo do backfill.")
    grupo.add_argument("--tags", default=None, help="Tags JSON do backfill.")
    grupo.add_argument("--priority", default="medium",
                       choices=["critical", "high", "medium", "low"],
                       help="Prioridade do backfill.")
    grupo.add_argument("--source", default="agent",
                       choices=["human", "agent", "system", "bootstrap"],
                       help="Source do backfill.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.spool_dir).expanduser() if args.spool_dir else default_spool_dir()
    root = ensure_spool_dirs(root)
    setup_logging(root, args.verbose)

    if args.status:
        print(json.dumps(status_report(root), ensure_ascii=False, indent=2))
        return 0

    if args.backfill:
        try:
            tags = json.loads(args.tags) if args.tags else {}
        except json.JSONDecodeError as exc:
            logger.error("--tags nao e JSON valido: %s", exc)
            return 2
        estagiados = backfill(
            source_dir=Path(args.backfill),
            root=root,
            collection=args.collection,
            tags=tags,
            priority=args.priority,
            source=args.source,
            pattern=args.pattern,
            dry_run=args.dry_run,
        )
        logger.info("backfill estagiou %s item(ns)", len(estagiados))
        if not (args.once or args.watch):
            return 0

    api: Optional[ApiBackend] = None
    if not args.no_api:
        api = ApiBackend(
            base_url=args.api_url or os.environ.get("ORKMIND_API_URL", DEFAULT_API_URL),
            token=os.environ.get("ORKMIND_API_TOKEN", "").strip(),
        )
        if not api.configured:
            logger.info(
                "API nao configurada (sem ORKMIND_API_TOKEN); usando o CLI como backend."
            )

    cli: Optional[CliBackend] = None
    if not args.no_cli:
        cli = CliBackend(Path(args.cli_path) if args.cli_path else None)
        if not cli.configured:
            logger.warning("CLI orkmind nao encontrado em %s", cli.executable)

    drainer = Drainer(root=root, api=api, cli=cli, dry_run=args.dry_run)

    if args.watch:
        drainer.watch(args.interval)
        return 0

    placar = drainer.drain_once()
    logger.info("passada concluida: %s", placar)
    print(json.dumps(placar, ensure_ascii=False))
    # Codigo 1 quando sobrou falha, para o cron conseguir alertar.
    return 1 if placar.get("falha") else 0


if __name__ == "__main__":
    sys.exit(main())
