"""Fila de saida (outbox) em disco do OrkMind.

Este modulo define o CONTRATO da spool usada pela solucao "sempre
gravar": todo conteudo com intencao de memoria e primeiro persistido em
disco (barato e quase infalivel) e so depois reconciliado com o banco
por um processo externo, o drainer (scripts/orkmind_drain.py).

Motivacao: um job agendado pode rodar sem as tools de execucao no
toolset, ou com o backend momentaneamente fora. Nesses casos a gravacao
direta falha e o conteudo viraria orfao. Com a spool, a INTENCAO ja esta
duravel em disco e o drainer fecha a conta depois, de forma retroativa e
auditavel.

Regras constitucionais respeitadas:

- Armazenamento e autoridade: o item so vira "done" com entry_id REAL
  devolvido pelo backend. Nunca se fabrica id.
- Nada se perde: itens sao movidos entre diretorios (rename atomico),
  nunca apagados. `done/` acumula historico, `failed/` retem para
  revisao humana.
- Nao duplicar: o `content_hash` (SHA-256 do conteudo) e a chave
  deterministica de destino, conferida pelo drainer antes de gravar e
  garantida pelo indice unico parcial (collection, content_hash).

Layout em disco (raiz configuravel por ORKMIND_SPOOL_DIR, default
~/.hermes/orkmind-spool):

    pending/<item_id>.json    metadados + hash + estado
    pending/<item_id>.md      conteudo verbatim
    done/<item_id>.json|.md   gravado, com entry_id real
    failed/<item_id>.json|.md esgotou tentativas, aguarda revisao
    drainer.log               auditoria do drainer

O par .json/.md e separado de proposito: o drainer le os metadados sem
carregar conteudo grande, e as tentativas reescrevem apenas o .json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SPOOL_VERSION = 1

#: Variavel de ambiente que redireciona a raiz da spool.
SPOOL_DIR_ENV = "ORKMIND_SPOOL_DIR"

#: Raiz padrao da spool.
DEFAULT_SPOOL_DIR = Path.home() / ".hermes" / "orkmind-spool"

#: Estados possiveis de um item, que sao tambem os subdiretorios.
STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATES = (STATE_PENDING, STATE_DONE, STATE_FAILED)

#: Tentativas antes de mandar o item para `failed/`.
DEFAULT_MAX_ATTEMPTS = 8

#: Backoff em minutos por numero de tentativas ja feitas. A ultima
#: posicao se repete para tentativas alem da lista.
BACKOFF_MINUTES = (1, 5, 15, 60, 240, 480, 720, 1440)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def compute_content_hash(content: str) -> str:
    """SHA-256 do conteudo em UTF-8.

    Espelha orkmind.core.injection.compute_content_hash de proposito:
    a spool precisa ser utilizavel por processos que nao importam o
    pacote orkmind (por exemplo o plugin do Hermes), entao o algoritmo
    fica declarado aqui tambem, e os dois lados precisam concordar.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def default_spool_dir() -> Path:
    """Raiz da spool: env ORKMIND_SPOOL_DIR, senao o default."""
    env_dir = os.environ.get(SPOOL_DIR_ENV, "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return DEFAULT_SPOOL_DIR


def ensure_spool_dirs(root: Optional[Path] = None) -> Path:
    """Garante que pending/, done/ e failed/ existem. Devolve a raiz."""
    base = Path(root).expanduser() if root else default_spool_dir()
    for state in STATES:
        (base / state).mkdir(parents=True, exist_ok=True)
    return base


def slugify(value: str, max_len: int = 48) -> str:
    """Converte texto livre em um slug seguro para nome de arquivo."""
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_RE.sub("-", ascii_only).strip("-")
    return slug[:max_len] or "item"


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _iso(value: datetime) -> str:
    return value.isoformat()


def build_item_id(label: str, content_hash: str, moment: Optional[datetime] = None) -> str:
    """Nome base do item: ordenavel por tempo e unico pelo hash.

    Formato: <YYYYmmdd_HHMMSS>_<slug>_<8 primeiros do hash>. O sufixo de
    hash evita colisao entre dois itens estagiados no mesmo segundo e
    torna o nome auto-descritivo na auditoria.
    """
    stamp = (moment or _now()).strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{slugify(label)}_{content_hash[:8]}"


@dataclass
class SpoolItem:
    """Um item da fila de saida, espelhando o arquivo .json."""

    item_id: str
    collection: str
    content_hash: str
    content_file: str
    created_at: str = field(default_factory=lambda: _iso(_now()))
    version: int = SPOOL_VERSION
    source: str = "agent"
    source_job_id: Optional[str] = None
    tags: dict[str, list[str]] = field(default_factory=dict)
    priority: str = "medium"
    mandatory: bool = False
    scope: str = "global"
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    status: str = STATE_PENDING
    entry_id: Optional[str] = None
    result: Optional[str] = None
    backend: Optional[str] = None
    last_error: Optional[str] = None
    last_attempt_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    origin: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # --- serializacao ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "item_id": self.item_id,
            "created_at": self.created_at,
            "source": self.source,
            "source_job_id": self.source_job_id,
            "collection": self.collection,
            "tags": self.tags,
            "priority": self.priority,
            "mandatory": self.mandatory,
            "scope": self.scope,
            "content_file": self.content_file,
            "content_hash": self.content_hash,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "status": self.status,
            "entry_id": self.entry_id,
            "result": self.result,
            "backend": self.backend,
            "last_error": self.last_error,
            "last_attempt_at": self.last_attempt_at,
            "next_attempt_at": self.next_attempt_at,
            "origin": self.origin,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SpoolItem":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SIM118
        payload = {k: v for k, v in data.items() if k in known}
        payload.setdefault("item_id", "desconhecido")
        payload.setdefault("collection", "content")
        payload.setdefault("content_hash", "")
        payload.setdefault("content_file", f"{payload['item_id']}.md")
        return cls(**payload)

    # --- backoff ---

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """True quando o item pode ser tentado agora (backoff vencido)."""
        if not self.next_attempt_at:
            return True
        try:
            due = datetime.fromisoformat(self.next_attempt_at)
        except ValueError:
            return True
        moment = now or _now()
        if due.tzinfo is None:
            due = due.replace(tzinfo=moment.tzinfo)
        return moment >= due

    def schedule_retry(self, error: str, now: Optional[datetime] = None) -> None:
        """Registra a falha, incrementa tentativas e agenda o retry."""
        moment = now or _now()
        self.attempts += 1
        self.last_error = error
        self.last_attempt_at = _iso(moment)
        idx = min(self.attempts - 1, len(BACKOFF_MINUTES) - 1)
        self.next_attempt_at = _iso(moment + timedelta(minutes=BACKOFF_MINUTES[idx]))

    @property
    def exhausted(self) -> bool:
        """True quando esgotou as tentativas e deve ir para failed/."""
        return self.attempts >= self.max_attempts


def _atomic_write(path: Path, data: str) -> None:
    """Escreve via arquivo temporario + os.replace (atomico no mesmo fs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_item(item: SpoolItem, root: Path, state: str = STATE_PENDING) -> Path:
    """Reescreve apenas o .json do item no estado informado."""
    target = Path(root) / state / f"{item.item_id}.json"
    _atomic_write(target, json.dumps(item.to_dict(), ensure_ascii=False, indent=2))
    return target


def stage_item(
    content: str,
    collection: str,
    tags: Optional[dict[str, list[str]]] = None,
    priority: str = "medium",
    source: str = "agent",
    scope: str = "global",
    mandatory: bool = False,
    label: Optional[str] = None,
    source_job_id: Optional[str] = None,
    origin: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    root: Optional[Path] = None,
) -> SpoolItem:
    """Estagia conteudo em pending/ de forma atomica.

    Esta e a PRIMEIRA acao do caminho de escrita: acontece antes de
    qualquer tentativa de gravar no banco, para que a intencao seja
    duravel mesmo que o backend esteja fora ou a tool nao exista no
    toolset. Devolve o SpoolItem ja persistido.
    """
    base = ensure_spool_dirs(root)
    content_hash = compute_content_hash(content)
    item_id = build_item_id(label or collection, content_hash)

    # Conteudo primeiro: o .json so passa a existir quando o .md ja
    # esta em disco, entao o drainer nunca ve metadado sem conteudo.
    content_name = f"{item_id}.md"
    _atomic_write(base / STATE_PENDING / content_name, content)

    item = SpoolItem(
        item_id=item_id,
        collection=collection,
        content_hash=content_hash,
        content_file=content_name,
        tags=tags or {},
        priority=priority,
        source=source,
        scope=scope,
        mandatory=mandatory,
        source_job_id=source_job_id,
        origin=origin,
        metadata=metadata or {},
    )
    write_item(item, base, STATE_PENDING)
    return item


def read_content(item: SpoolItem, root: Path, state: str = STATE_PENDING) -> str:
    """Le o conteudo (.md) do item no estado informado."""
    return (Path(root) / state / item.content_file).read_text(encoding="utf-8")


def iter_items(root: Path, state: str = STATE_PENDING) -> Iterator[SpoolItem]:
    """Itera os itens de um estado, ordenados por created_at (nome)."""
    directory = Path(root) / state
    if not directory.is_dir():
        return
    for json_path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        yield SpoolItem.from_dict(data)


def move_item(item: SpoolItem, root: Path, to_state: str, from_state: str = STATE_PENDING) -> None:
    """Move .json e .md entre estados via rename atomico.

    Nada e sobrescrito nem apagado: o item apenas muda de diretorio,
    preservando o historico exigido pela regra "nada se perde".
    """
    base = Path(root)
    (base / to_state).mkdir(parents=True, exist_ok=True)
    item.status = to_state

    # O .json e reescrito no destino com o estado final antes de remover
    # a copia de origem, para que uma queda no meio deixe o item visivel
    # em algum dos dois lados, nunca em nenhum.
    write_item(item, base, to_state)

    src_content = base / from_state / item.content_file
    dst_content = base / to_state / item.content_file
    if src_content.exists():
        os.replace(src_content, dst_content)

    src_json = base / from_state / f"{item.item_id}.json"
    if src_json.exists() and src_json != (base / to_state / f"{item.item_id}.json"):
        src_json.unlink()
