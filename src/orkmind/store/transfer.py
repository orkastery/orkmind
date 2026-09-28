"""Export e import de memoria entre backends (F3.8, DP-5).

Formato portavel: JSONL. A primeira linha e o manifesto; as demais sao
registros com envelope `{"tipo": ..., "dados": ...}`. O embedding viaja
como lista de floats, que ja era o formato usado pelos snapshots.

Regras que este modulo nao negocia:

- **Migracao nunca remove a origem.** Nao existe flag de "mover".
- **Import nunca sobrescreve nem apaga.** O default de conflito e
  `skip`, seguindo o precedente do indice de idempotencia.
- **Snapshot antes de importar**, espelhando o auto-backup de
  `snapshot_restore`.
- **Perda e declarada**, nunca silenciosa (R0.3): o que nao atravessa e
  contado, nomeado e exige `--accept-loss`.
- **Verificacao pos-import obrigatoria.** Divergencia na contagem de
  entries `mandatory` e ERRO, nao aviso.

Nota de desenho: a enumeracao da origem usa `snapshot_commit` seguido de
`snapshot_show`. E o unico caminho do contrato que devolve TODAS as
entries, inclusive as marcadas com `injection_risk` e as ja expiradas -
`search_by_tags` filtra as duas coisas, e um exportador que perde memoria
em silencio seria pior do que nao existir. O efeito colateral e um
snapshot rotulado `auto-export` na origem, que serve de trilha de
auditoria do que foi exportado. Nenhuma entry da origem e criada,
alterada ou removida.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from orkmind.core.models import MemoryEntry
from orkmind.store.base import MemoryStore

logger = logging.getLogger(__name__)

VERSAO_FORMATO = 1
CHAVE_MANIFESTO = "orkmind_export"

LABEL_EXPORT = "auto-export"
LABEL_PRE_IMPORT = "auto-backup-pre-import"

EstrategiaConflito = Literal["skip", "fail"]


class ExportacaoInvalidaError(ValueError):
    """Arquivo sem manifesto valido, ou de uma versao de formato desconhecida."""


class ConflitoDeImportacaoError(RuntimeError):
    """Entry ja existente no destino com `--on-conflict fail`."""


class PerdaNaoAceitaError(RuntimeError):
    """O destino perderia dado e `--accept-loss` nao foi passado."""


class ReconciliacaoError(RuntimeError):
    """A verificacao pos-import encontrou divergencia bloqueante."""


@dataclass
class RelatorioExport:
    """O que saiu, de onde, e o que ficou para tras."""

    backend_origem: str
    arquivo: str
    entry_count: int = 0
    mandatory_count: int = 0
    profile_count: int = 0
    version_count: int = 0
    snapshot_count: int = 0
    collections: list[str] = field(default_factory=list)
    contagem_origem_antes: int = 0
    contagem_origem_depois: int = 0
    snapshot_de_export: str = ""

    @property
    def origem_intacta(self) -> bool:
        return self.contagem_origem_antes == self.contagem_origem_depois

    def como_texto(self) -> str:
        linhas = [
            f"Backend de origem: {self.backend_origem}",
            f"Arquivo: {self.arquivo}",
            f"Entries exportadas: {self.entry_count} "
            f"(mandatory: {self.mandatory_count})",
            f"Identidades na origem: {self.profile_count}",
            f"Colecoes: {', '.join(self.collections) or '(nenhuma)'}",
            f"Versoes exportadas: {self.version_count}",
            f"Snapshots exportados: {self.snapshot_count}",
            f"Snapshot de auditoria na origem: {self.snapshot_de_export}",
            f"Origem intacta: {'sim' if self.origem_intacta else 'NAO'} "
            f"({self.contagem_origem_antes} antes, "
            f"{self.contagem_origem_depois} depois)",
        ]
        return "\n".join(linhas)


@dataclass
class RelatorioImport:
    """O que entrou, o que colidiu e o que se perdeu."""

    backend_destino: str
    arquivo: str
    backend_origem: str = ""
    dry_run: bool = False
    lidas: int = 0
    importadas: int = 0
    conflitos_por_hash: int = 0
    conflitos_por_id: int = 0
    sem_embedding: int = 0
    versoes_descartadas: int = 0
    snapshots_descartados: int = 0
    mandatory_origem: int = 0
    mandatory_destino: int = 0
    perfis_origem: int = 0
    perfis_que_atravessam: int = 0
    perfis_destino: int = 0
    por_colecao: dict[str, int] = field(default_factory=dict)
    hashes_faltando: list[str] = field(default_factory=list)
    snapshot_pre_import: str = ""
    perdas: list[str] = field(default_factory=list)

    @property
    def mandatory_reconciliado(self) -> bool:
        return self.mandatory_origem == self.mandatory_destino

    def como_texto(self) -> str:
        linhas = [
            f"Backend de destino: {self.backend_destino}",
            f"Backend de origem (manifesto): {self.backend_origem}",
            f"Arquivo: {self.arquivo}",
            f"Modo: {'dry-run (nada foi escrito)' if self.dry_run else 'efetivo'}",
            f"Entries lidas: {self.lidas}",
            f"Entries importadas: {self.importadas}",
            f"Colidiram por content_hash: {self.conflitos_por_hash}",
            f"Colidiram por id: {self.conflitos_por_id}",
            f"Ficariam sem embedding: {self.sem_embedding}",
            f"Versoes que nao atravessam: {self.versoes_descartadas}",
            f"Snapshots que nao atravessam: {self.snapshots_descartados}",
            f"Mandatory na origem: {self.mandatory_origem} | "
            f"no destino: {self.mandatory_destino}",
            f"Identidades na origem: {self.perfis_origem} | que atravessam: "
            f"{self.perfis_que_atravessam} | no destino: {self.perfis_destino}",
            f"Snapshot pre-import no destino: "
            f"{self.snapshot_pre_import or '(nenhum, dry-run)'}",
        ]
        if self.por_colecao:
            detalhe = ", ".join(
                f"{c}={n}" for c, n in sorted(self.por_colecao.items())
            )
            linhas.append(f"Contagem por colecao no destino: {detalhe}")
        if self.hashes_faltando:
            linhas.append(
                f"content_hash da origem ausentes no destino: "
                f"{len(self.hashes_faltando)}"
            )
        for perda in self.perdas:
            linhas.append(f"PERDA DECLARADA: {perda}")
        return "\n".join(linhas)


def _agora() -> datetime:
    return datetime.now(timezone.utc)


async def _contar_perfis(store: MemoryStore) -> int:
    """Quantas identidades a origem/destino conhece. Best-effort.

    Um store que nao exponha perfis devolve 0, e a ausencia nunca derruba
    o export nem o import.
    """
    try:
        perfis = await store.profile_list()  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        logger.debug("Nao foi possivel listar perfis: %s", e)
        return 0
    return len(perfis or [])


def _e_entry_de_perfil(entry: MemoryEntry) -> bool:
    """A entry carrega uma identidade (backends com EntryProfileStore)."""
    return bool(entry.metadata.get("orkmind_profile"))


async def _todas_as_entries(store: MemoryStore, mensagem: str) -> tuple[list[MemoryEntry], str]:
    """Todas as entries da origem, via snapshot de auditoria.

    Devolve tambem o id do snapshot criado, que fica registrado no
    relatorio: o operador sabe exatamente o que foi lido e quando.
    """
    snapshot_id = await store.snapshot_commit(LABEL_EXPORT, mensagem)
    dados = await store.snapshot_show(snapshot_id)
    entries = []
    for registro in dados.get("entries", []):
        entries.append(MemoryEntry(**dict(registro.get("entry_data") or {})))
    return entries, snapshot_id


async def exportar(
    store: MemoryStore,
    destino: Path | str,
    collection: Optional[str] = None,
    include_versions: bool = False,
    include_snapshots: bool = False,
) -> RelatorioExport:
    """Escreve o dump JSONL. Nunca altera as entries da origem."""
    caminho = Path(destino)
    backend = store.capabilities.backend
    antes = await store.count()

    entries, snapshot_id = await _todas_as_entries(
        store, f"export para {caminho.name}"
    )
    if collection:
        entries = [e for e in entries if e.collection == collection]
    entries.sort(key=lambda e: (e.created_at, e.id))

    relatorio = RelatorioExport(
        backend_origem=backend,
        arquivo=str(caminho),
        entry_count=len(entries),
        mandatory_count=sum(1 for e in entries if e.mandatory),
        profile_count=await _contar_perfis(store),
        collections=sorted({e.collection for e in entries}),
        contagem_origem_antes=antes,
        snapshot_de_export=snapshot_id,
    )

    linhas: list[str] = []
    manifesto = {
        CHAVE_MANIFESTO: VERSAO_FORMATO,
        "source_backend": backend,
        "embedding_dim": len(entries[0].embedding)
        if entries and entries[0].embedding
        else None,
        "exported_at": _agora().isoformat(),
        "entry_count": len(entries),
        "mandatory_count": relatorio.mandatory_count,
        # DD-6: em `pgvector` as identidades vivem na tabela `profiles` e
        # NAO viajam como entries. `profile_count` e quantas a origem
        # conhecia; `profiles_as_entries` e quantas atravessam de fato.
        "profile_count": relatorio.profile_count,
        "profiles_as_entries": sum(1 for e in entries if _e_entry_de_perfil(e)),
        "collections": relatorio.collections,
        "include_versions": include_versions,
        "include_snapshots": include_snapshots,
    }
    linhas.append(json.dumps(manifesto, ensure_ascii=False))

    for entry in entries:
        dados = entry.model_dump(mode="json")
        if entry.embedding:
            dados["embedding"] = list(entry.embedding)
        linhas.append(
            json.dumps({"tipo": "entry", "dados": dados}, ensure_ascii=False)
        )

    if include_versions and store.capabilities.versioning:
        for entry in entries:
            for versao in await store.get_history(entry.id):
                relatorio.version_count += 1
                linhas.append(
                    json.dumps(
                        {
                            "tipo": "version",
                            "memory_id": entry.id,
                            "dados": versao.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    )
                )

    if include_snapshots and store.capabilities.snapshots:
        for resumo in await store.snapshot_log(limit=1000):
            if resumo.get("id") == snapshot_id:
                continue  # o snapshot de auditoria deste export nao viaja
            detalhe = await store.snapshot_show(str(resumo["id"]))
            relatorio.snapshot_count += 1
            linhas.append(
                json.dumps(
                    {"tipo": "snapshot", "dados": _serializavel(detalhe)},
                    ensure_ascii=False,
                )
            )

    caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    relatorio.contagem_origem_depois = await store.count()
    if not relatorio.origem_intacta:
        # Nao deveria acontecer: export nao escreve entry nenhuma.
        logger.error(
            "Contagem da origem mudou durante o export (%d -> %d). Verifique "
            "se ha escrita concorrente.",
            relatorio.contagem_origem_antes,
            relatorio.contagem_origem_depois,
        )
    return relatorio


def _serializavel(valor: Any) -> Any:
    """Converte datetime aninhado para texto, preservando o resto."""
    if isinstance(valor, datetime):
        return valor.isoformat()
    if isinstance(valor, dict):
        return {k: _serializavel(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_serializavel(v) for v in valor]
    return valor


def ler_dump(origem: Path | str) -> tuple[dict[str, Any], Iterator[dict[str, Any]]]:
    """Manifesto e registros de um dump. Recusa arquivo sem manifesto."""
    caminho = Path(origem)
    if not caminho.exists():
        raise ExportacaoInvalidaError(f"Arquivo de import '{caminho}' nao existe.")
    linhas = caminho.read_text(encoding="utf-8").splitlines()
    if not linhas:
        raise ExportacaoInvalidaError(f"Arquivo de import '{caminho}' esta vazio.")
    try:
        manifesto = json.loads(linhas[0])
    except json.JSONDecodeError as e:
        raise ExportacaoInvalidaError(
            f"A primeira linha de '{caminho}' nao e um manifesto JSON: {e}"
        ) from e
    if not isinstance(manifesto, dict) or CHAVE_MANIFESTO not in manifesto:
        raise ExportacaoInvalidaError(
            f"'{caminho}' nao tem manifesto do OrkMind na primeira linha "
            f"(chave '{CHAVE_MANIFESTO}'). Import recusado."
        )
    versao = manifesto[CHAVE_MANIFESTO]
    if versao != VERSAO_FORMATO:
        raise ExportacaoInvalidaError(
            f"Formato de export versao {versao}; esta versao do OrkMind le "
            f"apenas a {VERSAO_FORMATO}."
        )

    def registros() -> Iterator[dict[str, Any]]:
        for numero, linha in enumerate(linhas[1:], start=2):
            if not linha.strip():
                continue
            try:
                yield json.loads(linha)
            except json.JSONDecodeError as e:
                raise ExportacaoInvalidaError(
                    f"Linha {numero} de '{caminho}' nao e JSON valido: {e}"
                ) from e

    return manifesto, registros()


async def importar(
    store: MemoryStore,
    origem: Path | str,
    dry_run: bool = False,
    on_conflict: EstrategiaConflito = "skip",
    accept_loss: bool = False,
) -> RelatorioImport:
    """Le o dump e grava no destino. Nunca sobrescreve nem apaga."""
    caminho = Path(origem)
    manifesto, registros = ler_dump(caminho)
    capabilities = store.capabilities

    relatorio = RelatorioImport(
        backend_destino=capabilities.backend,
        arquivo=str(caminho),
        backend_origem=str(manifesto.get("source_backend", "desconhecido")),
        dry_run=dry_run,
        mandatory_origem=int(manifesto.get("mandatory_count") or 0),
        perfis_origem=int(manifesto.get("profile_count") or 0),
        perfis_que_atravessam=int(manifesto.get("profiles_as_entries") or 0),
    )

    entries: list[MemoryEntry] = []
    for registro in registros:
        tipo = registro.get("tipo")
        if tipo == "entry":
            entries.append(MemoryEntry(**dict(registro.get("dados") or {})))
        elif tipo == "version":
            relatorio.versoes_descartadas += 1
        elif tipo == "snapshot":
            relatorio.snapshots_descartados += 1
    relatorio.lidas = len(entries)
    # O manifesto pode estar desatualizado ou o dump pode ter sido filtrado:
    # a fonte da verdade e o que realmente veio no arquivo.
    relatorio.mandatory_origem = sum(1 for e in entries if e.mandatory)

    # --- perdas declaradas ---
    if relatorio.versoes_descartadas:
        relatorio.perdas.append(
            f"{relatorio.versoes_descartadas} versoes anteriores nao "
            f"atravessam: o contrato nao tem caminho para injetar historico "
            f"em um backend de destino"
        )
    if relatorio.snapshots_descartados:
        relatorio.perdas.append(
            f"{relatorio.snapshots_descartados} snapshots nao atravessam pelo "
            f"mesmo motivo"
        )
    if not capabilities.versioning:
        relatorio.perdas.append(
            f"o destino '{capabilities.backend}' declara versioning=False"
        )
    if not capabilities.snapshots:
        relatorio.perdas.append(
            f"o destino '{capabilities.backend}' declara snapshots=False"
        )
    # DD-6: identidade e infraestrutura da ACL. Quando a origem guarda
    # perfis fora do namespace de entries (`pgvector`, tabela `profiles`),
    # eles NAO atravessam, e uma migracao que perde grupo em silencio
    # muda quem enxerga o que no destino. Perda declarada, nunca muda.
    perfis_perdidos = relatorio.perfis_origem - relatorio.perfis_que_atravessam
    if perfis_perdidos > 0:
        relatorio.perdas.append(
            f"{perfis_perdidos} identidades (perfis e grupos) da origem "
            f"'{relatorio.backend_origem}' nao atravessam: elas vivem fora do "
            f"namespace de entries e o dump nao as carrega. No destino a ACL "
            f"passa a resolver so a identidade direta, e entries com "
            f"visibility='restricted' por grupo ficam invisiveis para os "
            f"membros. Recrie os perfis no destino antes de usar"
        )
    if not capabilities.stores_embedding:
        relatorio.perdas.append(
            f"o destino '{capabilities.backend}' declara stores_embedding=False: "
            f"os vetores serao perdidos"
        )
    relatorio.sem_embedding = sum(1 for e in entries if not e.embedding)

    if relatorio.perdas and not accept_loss and not dry_run:
        raise PerdaNaoAceitaError(
            "Import abortado: haveria perda declarada. Rode com --accept-loss "
            "para aceitar, ou com --dry-run para so ver o relatorio.\n"
            + "\n".join(f"  - {p}" for p in relatorio.perdas)
        )

    # --- conflitos ---
    a_gravar: list[MemoryEntry] = []
    for entry in entries:
        colide_id = await store.retrieve(entry.id) is not None
        colide_hash = False
        if entry.content_hash:
            existente = await store.find_by_content_hash(
                entry.content_hash, entry.collection
            )
            colide_hash = existente is not None
        if colide_id:
            relatorio.conflitos_por_id += 1
        if colide_hash:
            relatorio.conflitos_por_hash += 1
        if colide_id or colide_hash:
            if on_conflict == "fail":
                raise ConflitoDeImportacaoError(
                    f"Entry '{entry.id}' ja existe no destino "
                    f"(id={colide_id}, content_hash={colide_hash}) e "
                    f"--on-conflict fail foi pedido. Nada foi importado alem "
                    f"do que ja tinha entrado antes deste ponto."
                )
            continue
        a_gravar.append(entry)

    if dry_run:
        return relatorio

    # --- snapshot do destino antes de qualquer escrita ---
    if capabilities.snapshots:
        relatorio.snapshot_pre_import = await store.snapshot_commit(
            LABEL_PRE_IMPORT, f"antes de importar {caminho.name}"
        )
    else:
        relatorio.perdas.append(
            f"o destino '{capabilities.backend}' nao guarda snapshots: nao foi "
            f"possivel criar o backup pre-import"
        )

    for entry in a_gravar:
        await store.store(entry)
        relatorio.importadas += 1

    # --- verificacao pos-import obrigatoria ---
    for nome in await store.list_collections():
        relatorio.por_colecao[nome] = await store.count(nome)
    relatorio.perfis_destino = await _contar_perfis(store)
    if relatorio.perfis_destino < relatorio.perfis_origem:
        logger.warning(
            "Import terminou com %d identidades no destino contra %d na "
            "origem. A ACL do destino resolve menos grupos que a da origem.",
            relatorio.perfis_destino,
            relatorio.perfis_origem,
        )

    # Reconciliacao entry a entry, e nao por contagem bruta: o destino pode
    # ja ter memoria propria, e uma entry que colidiu por content_hash ESTA
    # no destino, apenas nao foi gravada de novo.
    ausentes: list[MemoryEntry] = []
    faltando_hash: list[str] = []
    for entry in entries:
        if await _presente_no_destino(store, entry):
            if entry.mandatory:
                relatorio.mandatory_destino += 1
        else:
            ausentes.append(entry)
            if entry.content_hash:
                faltando_hash.append(entry.content_hash)
    relatorio.hashes_faltando = sorted(set(faltando_hash))

    if not relatorio.mandatory_reconciliado:
        perdidas = [e.id for e in ausentes if e.mandatory]
        raise ReconciliacaoError(
            f"Divergencia de entries mandatory apos o import: a origem trouxe "
            f"{relatorio.mandatory_origem} e o destino so tem "
            f"{relatorio.mandatory_destino}. Isso e erro, nao aviso: uma regra "
            f"obrigatoria que nao atravessou e governanca perdida. "
            f"Ids ausentes: {', '.join(perdidas) or '(nenhum identificado)'}\n"
            + relatorio.como_texto()
        )
    return relatorio


async def _presente_no_destino(store: MemoryStore, entry: MemoryEntry) -> bool:
    """A entry chegou ao destino, seja pelo id, seja por content_hash."""
    if await store.retrieve(entry.id) is not None:
        return True
    if entry.content_hash:
        achada = await store.find_by_content_hash(
            entry.content_hash, entry.collection
        )
        return achada is not None
    return False
