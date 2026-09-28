"""OrkMind CLI entry point."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, cast

import click

from orkmind.core.config import load_config
from orkmind.core.detectors import detect_context
from orkmind.core.injection import compute_content_hash
from orkmind.core.models import MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.store.factory import create_embedder, create_store


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _get_layer() -> SemanticLayer:
    config = load_config()
    store = create_store(config)
    _run(store.initialize())
    embedder = create_embedder(config)
    return SemanticLayer(store, token_budget=config.token_budget, embedder=embedder)


@click.group()
@click.version_option(package_name="orkmind")
def cli() -> None:
    """OrkMind -- Semantic memory layer for AI agents."""


@cli.command()
@click.option("--collection", "-c", required=True, help="Collection type")
@click.option("--content", "-t", required=True, help="Memory content")
@click.option("--tags", default=None, help='Tags as JSON: \'{"skill": ["git"]}\'')
@click.option(
    "--priority", "-p", default="medium",
    type=click.Choice(["critical", "high", "medium", "low"]),
)
@click.option("--mandatory", is_flag=True, default=False)
@click.option(
    "--scope", default="global",
    type=click.Choice(["global", "project", "session"]),
)
@click.option(
    "--source", default="human",
    type=click.Choice(["human", "agent", "system", "bootstrap"]),
)
@click.option(
    "--content-hash", "content_hash", default=None,
    help=(
        "SHA-256 esperado do conteudo. O CLI recomputa e recusa se "
        "divergir, evitando gravar sob uma chave de dedupe errada."
    ),
)
@click.option(
    "--dedupe", is_flag=True, default=False,
    help=(
        "Idempotente: se ja existir entry com o mesmo content_hash na "
        "colecao, devolve o id existente em vez de duplicar."
    ),
)
@click.option(
    "--json", "as_json", is_flag=True, default=False,
    help="Saida em JSON (status/entry_id/content_hash), para automacao.",
)
def add(
    collection: str,
    content: str,
    tags: Optional[str],
    priority: str,
    mandatory: bool,
    scope: str,
    source: str,
    content_hash: Optional[str],
    dedupe: bool,
    as_json: bool,
) -> None:
    """Add a memory entry.

    Com --dedupe a operacao vira idempotente por content_hash: e o modo
    usado pelo drainer da spool quando a API local nao esta disponivel,
    de forma que reprocessar a fila nunca duplica memoria.
    """
    parsed_tags = json.loads(tags) if tags else {}

    # Simetria de hash: quem manda no hash e o conteudo, nao o chamador.
    computed_hash = compute_content_hash(content)
    if content_hash and content_hash.strip() != computed_hash:
        raise click.ClickException(
            f"content_hash divergente do conteudo. "
            f"esperado={computed_hash} recebido={content_hash.strip()}"
        )

    layer = _get_layer()

    if dedupe:
        existing = _run(layer.store.find_by_content_hash(computed_hash, collection))
        if existing is not None:
            if as_json:
                click.echo(json.dumps({
                    "status": "duplicate",
                    "entry_id": existing.id,
                    "content_hash": computed_hash,
                    "collection": existing.collection,
                }, ensure_ascii=False))
            else:
                click.echo(f"Duplicate: {existing.id}")
            return

    entry = MemoryEntry(
        content=content,
        collection=collection,  # type: ignore[arg-type]
        tags=parsed_tags,
        priority=priority,  # type: ignore[arg-type]
        mandatory=mandatory,
        scope=scope,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        content_hash=computed_hash,
    )

    try:
        entry_id, warnings = _run(layer.add_memory(entry))
    except Exception as exc:  # noqa: BLE001
        # Corrida com outro gravador: o indice unico parcial barrou o
        # INSERT. Em modo --dedupe isso e sucesso idempotente.
        if dedupe:
            duplicate = _run(layer.store.find_by_content_hash(computed_hash, collection))
            if duplicate is not None:
                if as_json:
                    click.echo(json.dumps({
                        "status": "duplicate",
                        "entry_id": duplicate.id,
                        "content_hash": computed_hash,
                        "collection": duplicate.collection,
                    }, ensure_ascii=False))
                else:
                    click.echo(f"Duplicate: {duplicate.id}")
                return
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(json.dumps({
            "status": "created",
            "entry_id": entry_id,
            "content_hash": computed_hash,
            "collection": collection,
            "warnings": warnings,
        }, ensure_ascii=False))
        return

    click.echo(f"Added: {entry_id}")
    for w in warnings:
        click.echo(f"  Warning: {w}")


@cli.command("list")
@click.option("--collection", "-c", default=None, help="Filter by collection")
@click.option("--limit", "-n", default=50, help="Max entries to show")
@click.option(
    "--layer", "-l", default="source",
    type=click.Choice(["essence", "structure", "source"]),
    help="Camada de conteudo a exibir",
)
def list_entries(collection: Optional[str], limit: int, layer: str) -> None:
    """List memory entries."""
    from orkmind.core.layers import get_content_for_layer
    sem = _get_layer()
    entries = _run(sem.store.search_by_tags(tags={}, collection=collection, limit=limit))
    for e in entries:
        flag = " [MANDATORY]" if e.mandatory else ""
        content = get_content_for_layer(e, layer)  # type: ignore[arg-type]
        click.echo(f"[{e.collection}] {e.id[:8]}.. {e.priority}{flag}: {content[:80]}")
    click.echo(f"\nTotal: {len(entries)}")


@cli.command()
@click.option("--tags", "-t", default=None, help='Tags as JSON')
@click.option("--text", "-q", default=None, help="Full-text search query")
@click.option("--collection", "-c", default=None)
@click.option("--limit", "-n", default=10)
@click.option(
    "--layer", "-l", default="source",
    type=click.Choice(["essence", "structure", "source"]),
    help="Camada de conteudo a exibir",
)
@click.option("--intent", is_flag=True, default=False, help="Busca semantica com rerank RRF")
def search(
    tags: Optional[str],
    text: Optional[str],
    collection: Optional[str],
    limit: int,
    layer: str,
    intent: bool,
) -> None:
    """Search memories by tags or text."""
    from orkmind.core.layers import get_content_for_layer
    sem = _get_layer()
    if intent and text:
        entries = _run(sem.search_semantic_intent(
            query=text, collection=collection, limit=limit,
        ))
    elif text:
        entries = _run(sem.store.search_by_text(text, collection=collection, limit=limit))
    elif tags:
        parsed = json.loads(tags)
        entries = _run(sem.store.search_by_tags(parsed, collection=collection, limit=limit))
    else:
        click.echo("Provide --tags or --text for search.")
        sys.exit(1)

    for e in entries:
        flag = " [MANDATORY]" if e.mandatory else ""
        content = get_content_for_layer(e, layer)  # type: ignore[arg-type]
        click.echo(f"[{e.collection}] {e.id[:8]}.. {e.priority}{flag}: {content[:80]}")
    click.echo(f"\nResults: {len(entries)}")


@cli.command()
@click.option("--text", "-t", default="", help="Conversation text")
@click.option("--files", "-f", multiple=True, help="File paths")
def detect(text: str, files: tuple[str, ...]) -> None:
    """Detect context tags from conversation/files."""
    tags = detect_context(text, list(files))
    click.echo(json.dumps(tags.to_dict(), indent=2))


@cli.command("extract")
@click.option("--input", "input_file", default=None, help="Arquivo com transcript")
@click.option("--dry-run", is_flag=True, default=False, help="Mostra entries sem armazenar")
def extract(input_file: Optional[str], dry_run: bool) -> None:
    """Extrai memorias de uma sessao (transcript via stdin ou arquivo)."""
    from orkmind.core.extraction import (
        ExtractionProvider,
        RawExtraction,
        extract_from_session,
    )

    if input_file:
        with open(input_file) as f:
            transcript = f.read()
    else:
        transcript = sys.stdin.read()

    if not transcript.strip():
        click.echo("Transcript vazio.")
        return

    # Usar um provider stub que retorna lista vazia
    # (provider real requer config de LLM)
    class StubProvider(ExtractionProvider):
        async def extract(self, transcript: str) -> list[RawExtraction]:
            return []

        @property
        def provider_name(self) -> str:
            return "stub"

    sem = _get_layer()
    results = _run(extract_from_session(transcript, StubProvider()))

    if dry_run:
        click.echo(f"Dry-run: {len(results)} entries extraidas")
        for r in results:
            click.echo(f"  [{r.entry.collection}] {r.entry.content[:80]}")
            for w in r.warnings:
                click.echo(f"    Warning: {w}")
    else:
        for r in results:
            entry_id, warnings = _run(sem.add_memory(r.entry))
            click.echo(f"Armazenada: {entry_id[:8]}.. [{r.entry.collection}]")
            for w in r.warnings + warnings:
                click.echo(f"  Warning: {w}")
    click.echo(f"\nTotal: {len(results)}")


@cli.command()
@click.argument("entry_id")
def remove(entry_id: str) -> None:
    """Remove a memory entry by ID."""
    layer = _get_layer()
    success = _run(layer.store.delete(entry_id))
    if success:
        click.echo(f"Deleted: {entry_id}")
    else:
        click.echo(f"Not found: {entry_id}")
        sys.exit(1)


@cli.command("export")
@click.option("--collection", "-c", default=None)
@click.option("--output", "-o", default="-", help="Output file (- for stdout)")
def export_entries(collection: Optional[str], output: str) -> None:
    """Export memories as JSON."""
    layer = _get_layer()
    entries = _run(layer.store.search_by_tags(tags={}, collection=collection, limit=10000))
    data = [e.model_dump(mode="json", exclude={"embedding"}) for e in entries]
    text = json.dumps(data, indent=2, default=str)
    if output == "-":
        click.echo(text)
    else:
        with open(output, "w") as f:
            f.write(text)
        click.echo(f"Exported {len(data)} entries to {output}")


@cli.command("import")
@click.argument("input_file")
def import_entries(input_file: str) -> None:
    """Import memories from JSON file."""
    with open(input_file) as f:
        data = json.load(f)
    layer = _get_layer()
    count = 0
    for item in data:
        entry = MemoryEntry(**item)
        _run(layer.add_memory(entry))
        count += 1
    click.echo(f"Imported {count} entries")


@cli.command("encrypt")
@click.option("--collection", "-c", required=True, help="Colecao a encriptar")
def encrypt_collection(collection: str) -> None:
    """Encripta entries existentes em plaintext de uma colecao."""
    from orkmind.core.config import load_config as _load_config
    from orkmind.core.encryption import (
        LocalKeyProvider,
        encrypt_content,
        encryption_available,
    )

    if not encryption_available():
        click.echo("Erro: biblioteca cryptography nao instalada.")
        sys.exit(1)
    config = _load_config()
    provider = LocalKeyProvider(config.encryption_root_key_path)
    kek = provider.get_kek()
    sem = _get_layer()
    entries = _run(sem.store.search_by_tags(tags={}, collection=collection, limit=10000))
    count = 0
    for e in entries:
        if not e.encrypted:
            ciphertext, meta = encrypt_content(e.content, kek)
            e.content = ciphertext
            e.encrypted = True
            e.encryption_meta = meta
            _run(sem.store.update(e.id, e))
            count += 1
    click.echo(f"Encriptadas: {count} entries na colecao '{collection}'")


@cli.command("decrypt")
@click.option("--collection", "-c", required=True, help="Colecao a decriptar")
def decrypt_collection(collection: str) -> None:
    """Decripta entries de uma colecao (reversao)."""
    from orkmind.core.config import load_config as _load_config
    from orkmind.core.encryption import (
        LocalKeyProvider,
        decrypt_content,
        encryption_available,
    )

    if not encryption_available():
        click.echo("Erro: biblioteca cryptography nao instalada.")
        sys.exit(1)
    config = _load_config()
    provider = LocalKeyProvider(config.encryption_root_key_path)
    kek = provider.get_kek()
    sem = _get_layer()
    entries = _run(sem.store.search_by_tags(tags={}, collection=collection, limit=10000))
    count = 0
    for e in entries:
        if e.encrypted and e.encryption_meta:
            plaintext = decrypt_content(e.content, e.encryption_meta, kek)
            e.content = plaintext
            e.encrypted = False
            e.encryption_meta = None
            _run(sem.store.update(e.id, e))
            count += 1
    click.echo(f"Decriptadas: {count} entries na colecao '{collection}'")


@cli.command("key-rotate")
def key_rotate() -> None:
    """Re-encripta todos os DEKs com nova KEK (rotacao de chave)."""
    from orkmind.core.config import load_config as _load_config
    from orkmind.core.encryption import (
        LocalKeyProvider,
        decrypt_content,
        encrypt_content,
        encryption_available,
    )

    if not encryption_available():
        click.echo("Erro: biblioteca cryptography nao instalada.")
        sys.exit(1)
    config = _load_config()
    old_provider = LocalKeyProvider(config.encryption_root_key_path)
    old_kek = old_provider.get_kek()
    # Gerar nova root key
    LocalKeyProvider.generate_root_key(config.encryption_root_key_path)
    new_provider = LocalKeyProvider(config.encryption_root_key_path)
    new_kek = new_provider.get_kek()
    sem = _get_layer()
    collections = _run(sem.store.list_collections())
    count = 0
    for coll in collections:
        entries = _run(sem.store.search_by_tags(tags={}, collection=coll, limit=10000))
        for e in entries:
            if e.encrypted and e.encryption_meta:
                plaintext = decrypt_content(e.content, e.encryption_meta, old_kek)
                ciphertext, meta = encrypt_content(plaintext, new_kek)
                e.content = ciphertext
                e.encryption_meta = meta
                _run(sem.store.update(e.id, e))
                count += 1
    click.echo(f"Rotacao de chave concluida: {count} entries re-encriptadas")


@cli.command()
def stats() -> None:
    """Show store statistics."""
    layer = _get_layer()
    collections = _run(layer.store.list_collections())
    total = _run(layer.store.count())
    click.echo(f"Total entries: {total}")
    click.echo(f"Collections ({len(collections)}):")
    for coll in collections:
        cnt = _run(layer.store.count(coll))
        click.echo(f"  {coll}: {cnt}")


@cli.command()
def gc() -> None:
    """Garbage collect expired entries."""
    layer = _get_layer()
    removed = _run(layer.store.garbage_collect())
    click.echo(f"Removed {removed} expired entries")


@cli.group()
def snapshot() -> None:
    """Gerenciamento de snapshots globais."""


@snapshot.command("commit")
@click.option("--label", "-l", required=True, help="Label do snapshot")
@click.option("--message", "-m", default="", help="Mensagem descritiva")
def snapshot_commit(label: str, message: str) -> None:
    """Salva snapshot do estado atual."""
    from orkmind.core.snapshots import SnapshotManager
    sem = _get_layer()
    mgr = SnapshotManager(sem.store)
    snap_id = _run(mgr.commit(label=label, message=message))
    click.echo(f"Snapshot criado: {snap_id}")


@snapshot.command("log")
@click.option("--limit", "-n", default=20, help="Numero de snapshots a listar")
def snapshot_log(limit: int) -> None:
    """Lista snapshots."""
    from orkmind.core.snapshots import SnapshotManager
    sem = _get_layer()
    mgr = SnapshotManager(sem.store)
    snaps = _run(mgr.log(limit=limit))
    for s in snaps:
        click.echo(f"{s['id'][:8]}.. [{s['label']}] {s['entry_count']} entries - {s['created_at']}")
    click.echo(f"\nTotal: {len(snaps)}")


@snapshot.command("show")
@click.argument("snapshot_id")
def snapshot_show(snapshot_id: str) -> None:
    """Mostra detalhes de um snapshot."""
    from orkmind.core.snapshots import SnapshotManager
    sem = _get_layer()
    mgr = SnapshotManager(sem.store)
    data = _run(mgr.show(snapshot_id))
    click.echo(f"Snapshot: {data['id']}")
    click.echo(f"Label: {data['label']}")
    click.echo(f"Mensagem: {data['message']}")
    click.echo(f"Entries: {data['entry_count']}")
    click.echo(f"Criado em: {data['created_at']}")
    for e in data.get("entries", []):
        click.echo(f"  - {e['entry_id'][:8]}.. hash={e.get('content_hash', 'N/A')}")


@snapshot.command("diff")
@click.argument("id_a")
@click.argument("id_b")
def snapshot_diff(id_a: str, id_b: str) -> None:
    """Diff entre dois snapshots."""
    from orkmind.core.snapshots import SnapshotManager
    sem = _get_layer()
    mgr = SnapshotManager(sem.store)
    d = _run(mgr.diff(id_a, id_b))
    click.echo(f"Adicionadas: {len(d['added'])}")
    for eid in d["added"]:
        click.echo(f"  + {eid[:8]}..")
    click.echo(f"Removidas: {len(d['removed'])}")
    for eid in d["removed"]:
        click.echo(f"  - {eid[:8]}..")
    click.echo(f"Modificadas: {len(d['modified'])}")
    for eid in d["modified"]:
        click.echo(f"  ~ {eid[:8]}..")


@snapshot.command("restore")
@click.argument("snapshot_id")
@click.option("--yes", is_flag=True, default=False, help="Confirmar sem prompt")
def snapshot_restore(snapshot_id: str, yes: bool) -> None:
    """Restaura estado de um snapshot."""
    if not yes:
        click.confirm("Restaurar snapshot? Um auto-backup sera criado antes.", abort=True)
    from orkmind.core.snapshots import SnapshotManager
    sem = _get_layer()
    mgr = SnapshotManager(sem.store)
    count = _run(mgr.restore(snapshot_id))
    click.echo(f"Restaurado: {count} entries")


@cli.command("api")
@click.option("--host", default="127.0.0.1", help="Interface de escuta (default: loopback).")
@click.option("--port", default=8077, type=int, help="Porta de escuta (default: 8077).")
@click.option("--log-level", default="info", help="Nivel de log do uvicorn.")
def api(host: str, port: int, log_level: str) -> None:
    """Sobe a API local de escrita idempotente.

    Serve POST /entries e GET /entries/by-hash, usados pelo drainer da
    spool. Exige o token em ORKMIND_API_TOKEN: sem ele as rotas
    protegidas respondem 503, nunca ficam abertas.
    """
    from orkmind.api.app import API_TOKEN_ENV, serve

    if host not in ("127.0.0.1", "localhost", "::1"):
        click.echo(
            f"AVISO: escutando em '{host}', fora do loopback. Esta API grava "
            f"memoria e nao deve ficar exposta na rede.",
            err=True,
        )
    if not os.environ.get(API_TOKEN_ENV, "").strip():
        click.echo(
            f"AVISO: {API_TOKEN_ENV} nao definido. As rotas protegidas "
            f"responderao 503 ate o token ser configurado.",
            err=True,
        )

    click.echo(f"OrkMind API em http://{host}:{port}")
    serve(host=host, port=port, log_level=log_level)


# --- Grupo `store`: backend, export e import (F3.8) -------------------------


def _abrir_store():  # type: ignore[no-untyped-def]
    """Store do backend configurado, ja inicializado e governado."""
    config = load_config()
    store = create_store(config)
    _run(store.initialize())
    return store


@cli.group("store")
def store_group() -> None:
    """Operacoes de backend de storage."""


@store_group.command("info")
@click.option("--json", "como_json", is_flag=True, default=False,
              help="Imprime as capabilities em JSON")
def store_info(como_json: bool) -> None:
    """Mostra o backend ativo, o que ele sabe fazer e o que ele nao faz.

    E o caminho oficial para conferir uma degradacao antes de investigar
    um sintoma: nenhuma perda de garantia fica invisivel (R0.3).
    """
    config = load_config()
    store = create_store(config)
    capabilities = store.capabilities

    if como_json:
        click.echo(json.dumps(
            {
                "backend": capabilities.backend,
                "backend_configurado": config.store_backend,
                "capabilities": capabilities.as_dict(),
                "avisos": capabilities.avisos(),
            },
            indent=2,
            ensure_ascii=False,
        ))
        return

    click.echo(f"Backend configurado: {config.store_backend}")
    click.echo(f"Backend ativo: {capabilities.backend}")
    click.echo("")
    click.echo("Capacidades declaradas:")
    for campo, valor in capabilities.as_dict().items():
        if campo == "backend":
            continue
        click.echo(f"  {campo}: {valor}")

    avisos = capabilities.avisos()
    click.echo("")
    if avisos:
        click.echo(f"Avisos ativos ({len(avisos)}):")
        for aviso in avisos:
            click.echo(f"  - {aviso}")
    else:
        click.echo("Avisos ativos: nenhum. Este backend nao degrada nada.")


@store_group.command("export")
@click.option("--out", "saida", required=True, help="Arquivo JSONL de destino")
@click.option("--collection", "-c", default=None, help="Exportar so uma colecao")
@click.option("--include-versions", is_flag=True, default=False,
              help="Inclui as versoes anteriores no dump")
@click.option("--include-snapshots", is_flag=True, default=False,
              help="Inclui os snapshots no dump")
def store_export(
    saida: str,
    collection: Optional[str],
    include_versions: bool,
    include_snapshots: bool,
) -> None:
    """Exporta a memoria do backend ativo para um arquivo JSONL.

    Nunca altera nem remove nada da origem.
    """
    from orkmind.store.transfer import exportar

    store = _abrir_store()
    try:
        relatorio = _run(
            exportar(
                store,
                saida,
                collection=collection,
                include_versions=include_versions,
                include_snapshots=include_snapshots,
            )
        )
    finally:
        _run(store.close())
    click.echo(relatorio.como_texto())


@store_group.command("import")
@click.option("--in", "entrada", required=True, help="Arquivo JSONL de origem")
@click.option("--dry-run", is_flag=True, default=False,
              help="So relata: nada e escrito")
@click.option("--on-conflict", type=click.Choice(["skip", "fail"]), default="skip",
              help="O que fazer quando a entry ja existe no destino")
@click.option("--accept-loss", is_flag=True, default=False,
              help="Aceita explicitamente a perda declarada no relatorio")
def store_import(
    entrada: str, dry_run: bool, on_conflict: str, accept_loss: bool
) -> None:
    """Importa memoria de um arquivo JSONL para o backend ativo.

    Nunca sobrescreve nem apaga. Cria um snapshot do destino antes de
    escrever. O relatorio final e sempre impresso.
    """
    from orkmind.store.transfer import importar

    store = _abrir_store()
    try:
        relatorio = _run(
            importar(
                store,
                entrada,
                dry_run=dry_run,
                on_conflict=on_conflict,  # type: ignore[arg-type]
                accept_loss=accept_loss,
            )
        )
    finally:
        _run(store.close())
    click.echo(relatorio.como_texto())


# --- Grupo `federation`: uma memória por projeto, recall cruzado -----------


@cli.group("federation")
def federation_group() -> None:
    """Consulta federada entre memórias de projeto com ACL por perfil."""


@federation_group.command("profile-set")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
)
@click.option("--id", "profile_id", required=True, help="Identidade estável do perfil")
@click.option("--display-name", required=True)
@click.option(
    "--role",
    required=True,
    type=click.Choice(["system_owner", "builder", "agent"]),
)
@click.option("--projects", default="", help="Projetos separados por vírgula")
@click.option("--sources", default="*", help="Fontes separadas por vírgula")
@click.option("--current-project", default=None, help="Projeto atual obrigatório para agent")
def federation_profile_set(
    manifest_path: Optional[Path],
    profile_id: str,
    display_name: str,
    role: str,
    projects: str,
    sources: str,
    current_project: Optional[str],
) -> None:
    """Cria ou atualiza system_owner, builder ou agent na fonte de identidade."""
    from orkmind.federation import (
        close_federation,
        default_manifest_path,
        load_federation_manifest,
        open_federation,
        set_federation_profile,
    )

    path = manifest_path or default_manifest_path()
    stores = []
    try:
        federation, stores = _run(open_federation(load_federation_manifest(path)))
        profile = _run(set_federation_profile(
            federation.profiles,
            profile_id=profile_id,
            display_name=display_name,
            role=role,  # type: ignore[arg-type]
            projects=projects.split(","),
            sources=sources.split(","),
            current_project=current_project,
        ))
    except (ValueError, PermissionError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        if stores:
            _run(close_federation(stores))
    click.echo(json.dumps(profile.model_dump(mode="json"), indent=2, ensure_ascii=False))


@federation_group.command("recall")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Manifesto JSON; padrão ORKMIND_FEDERATION_MANIFEST ou ~/.orkmind/federation.json",
)
@click.option("--requester-id", required=True, help="Perfil humano/agente autenticado")
@click.option(
    "--caller",
    required=True,
    type=click.Choice(["ork", "hermes", "openclaw", "codex", "claude-code"]),
    help="Canal que iniciou o recall; registrado na proveniência",
)
@click.option("--tags", required=True, help='Tags exatas em JSON: {"project":["x"]}')
@click.option("--collection", default=None)
@click.option("--limit", type=click.IntRange(1, 500), default=50, show_default=True)
def federation_recall(
    manifest_path: Optional[Path],
    requester_id: str,
    caller: str,
    tags: str,
    collection: Optional[str],
    limit: int,
) -> None:
    """Faz fan-out read-only e preserva projeto, origem e produtor."""
    from orkmind.federation import (
        close_federation,
        default_manifest_path,
        load_federation_manifest,
        open_federation,
    )

    path = manifest_path or default_manifest_path()
    try:
        parsed_tags = json.loads(tags)
        if not isinstance(parsed_tags, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
            for key, value in parsed_tags.items()
        ):
            raise ValueError("tags must be an object of string arrays")
        federation, stores = _run(open_federation(load_federation_manifest(path)))
        try:
            result = _run(federation.recall(
                tags=parsed_tags,
                requester_id=requester_id,
                caller=caller,  # type: ignore[arg-type]
                collection=collection,
                limit=limit,
            ))
        finally:
            _run(close_federation(stores))
    except (ValueError, PermissionError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))


# --- Grupo `guardrail`: auditoria de regras por tipo (G1) -------------------


@cli.group("guardrail")
def guardrail_group() -> None:
    """Auditoria de guardrails de conteudo por sessao."""


@guardrail_group.command("check")
@click.option("--session-id", required=True, help="Identificador da sessao auditada")
@click.option("--requester-id", default="", help="Identidade do solicitante (ACL F2)")
@click.option(
    "--tags", default=None,
    help='Tags da sessao como JSON: \'{"project": ["orkmind"]}\'',
)
@click.option(
    "--observed-hash", default=None,
    help="Hash D-MA10 do bloco de regras presente no contexto da sessao",
)
@click.option(
    "--usage", type=float, default=None,
    help="Fracao de uso da janela informada pelo runtime (0.0 a 1.0)",
)
@click.option("--turns", type=int, default=None, help="Turnos decorridos na sessao")
@click.option(
    "--estimated-tokens", type=int, default=None,
    help="Tokens estimados ja consumidos na sessao",
)
@click.option(
    "--json", "como_json", is_flag=True, default=False,
    help="Saida em JSON (GuardrailReport completo), para automacao",
)
def guardrail_check(
    session_id: str,
    requester_id: str,
    tags: Optional[str],
    observed_hash: Optional[str],
    usage: Optional[float],
    turns: Optional[int],
    estimated_tokens: Optional[int],
    como_json: bool,
) -> None:
    """Audita a presenca das regras governadas numa sessao.

    Read-only: emite um GuardrailReport (regras por tipo, sinal de
    janela, fail-safe) e nao escreve nada no store. Quem aplica a
    reinjecao ou decide rotacionar e sempre o runtime. Mesmo padrao de
    consumo de `orkmind store info --json`.
    """
    from orkmind.guardrails import GuardrailSettings, SessionSnapshot, check

    snapshot = SessionSnapshot(
        session_id=session_id,
        requester_id=requester_id,
        session_tags=json.loads(tags) if tags else {},
        observed_rules_hash=observed_hash,
        context_usage_pct=usage,
        turn_count=turns,
        estimated_tokens=estimated_tokens,
    )
    layer = _get_layer()
    settings = GuardrailSettings.from_config(load_config())
    try:
        report = _run(check(snapshot, layer=layer, settings=settings))
    finally:
        _run(layer.store.close())

    if como_json:
        click.echo(json.dumps(report.model_dump(), indent=2, ensure_ascii=False))
        return

    r = report.rules
    click.echo(f"Regras: {r.status}")
    if r.expected_hash:
        click.echo(f"  hash esperado: sha256:{r.expected_hash}")
    for tipo in ("critical", "important", "soft"):
        detalhe = getattr(r.by_type, tipo)
        linha = f"  {tipo}: {detalhe.status} ({detalhe.count})"
        if detalhe.hint:
            linha += f" - {detalhe.hint}"
        click.echo(linha)
    if r.by_type.critical.reinjection_block:
        click.echo("  bloco de reinjecao pronto: sim (use --json para o conteudo)")

    s = report.session
    uso = f"{s.usage_pct * 100:.0f}%" if s.usage_pct is not None else "indisponivel"
    click.echo(f"Sessao: uso {uso} (fonte: {s.usage_source}) - advisory: {s.advisory}")
    click.echo(
        "Handoff de conteudo disponivel: "
        + ("sim" if s.handoff_content_available else "nao")
    )
    if report.fail_safe:
        click.echo("Fail-safe: ATIVO - nao operar a sessao sem tratar 'rules'.")
    else:
        click.echo("Fail-safe: inativo (regras verificadas).")


@cli.group("portfolio")
def portfolio_group() -> None:
    """Gerencia produtos, projetos e iniciativas tipados."""


@portfolio_group.command("create")
@click.argument(
    "kind",
    type=click.Choice(["product", "project", "initiative", "prod", "proj", "init"]),
)
@click.argument("entity_id", required=False)
@click.option("--title", required=True)
@click.option("--description", default="")
@click.option("--parent", default=None, help="Produto do projeto ou projeto da iniciativa")
@click.option("--workspace", "workspaces", multiple=True)
@click.option("--depends-on", "dependencies", multiple=True)
@click.option("--acceptance", "acceptance", multiple=True)
@click.option("--owner", default=None)
@click.option("--status", default="idea", type=click.Choice([
    "idea", "planned", "ready", "in_progress", "validating",
    "delivered", "blocked", "cancelled",
]))
def portfolio_create(
    kind: str,
    entity_id: Optional[str],
    title: str,
    description: str,
    parent: Optional[str],
    workspaces: tuple[str, ...],
    dependencies: tuple[str, ...],
    acceptance: tuple[str, ...],
    owner: Optional[str],
    status: str,
) -> None:
    """Cria uma entidade. Aliases prod/proj/init são aceitos na entrada."""
    from orkmind.core.portfolio import (
        Initiative,
        PortfolioCatalog,
        PortfolioStatus,
        Product,
        Project,
    )

    canonical = {"prod": "product", "proj": "project", "init": "initiative"}.get(kind, kind)
    common: dict[str, Any] = {
        "title": title,
        "description": description,
        "owner_id": owner,
        "status": cast(PortfolioStatus, status),
        "acceptance_criteria": list(acceptance),
    }
    if entity_id:
        common["id"] = entity_id
    layer = _get_layer()
    catalog = PortfolioCatalog(layer.store)
    entity: Product | Project | Initiative
    try:
        if canonical == "product":
            entity = Product(**common)
            entry_id = _run(catalog.add_product(entity))
        elif canonical == "project":
            if not parent:
                raise click.ClickException("project exige --parent prod-...")
            entity = Project(**common, product_id=parent, workspace_ids=list(workspaces))
            entry_id = _run(catalog.add_project(entity))
        else:
            if not parent:
                raise click.ClickException("initiative exige --parent proj-...")
            entity = Initiative(**common, project_id=parent, depends_on=list(dependencies))
            entry_id = _run(catalog.add_initiative(entity))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        _run(layer.store.close())
    click.echo(json.dumps({
        "schema": "orkmind.portfolio/v1",
        "id": entry_id,
        "kind": canonical,
        "entity": entity.model_dump(mode="json"),
    }, ensure_ascii=False))


@portfolio_group.command("list")
@click.argument(
    "kind",
    type=click.Choice(["product", "project", "initiative", "prod", "proj", "init"]),
    required=False,
)
@click.option("--parent", default=None)
def portfolio_list(kind: Optional[str], parent: Optional[str]) -> None:
    """Lista entidades, opcionalmente filtradas pelo pai."""
    from orkmind.core.portfolio import EntityKind, PortfolioCatalog

    canonical = {"prod": "product", "proj": "project", "init": "initiative"}.get(
        kind or "", kind
    )
    kinds: list[EntityKind] = (
        [cast(EntityKind, canonical)]
        if canonical
        else ["product", "project", "initiative"]
    )
    layer = _get_layer()
    try:
        catalog = PortfolioCatalog(layer.store)
        entries = []
        for entity_kind in kinds:
            entries.extend(_run(catalog.list(entity_kind, parent)))
    finally:
        _run(layer.store.close())
    click.echo(json.dumps({
        "schema": "orkmind.portfolio-list/v1",
        "kind": canonical or "all",
        "items": [entry.metadata for entry in entries],
    }, ensure_ascii=False))


from orkmind.cli.company_brain import brain
cli.add_command(brain)

if __name__ == "__main__":
    cli()
