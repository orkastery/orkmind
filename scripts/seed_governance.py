"""Seed das regras de governanca do OrkMind (D-MA4).

Cria a regra mandatoria anti-destruicao que impede qualquer agente de
deletar, compactar ou sobrescrever memorias para abrir espaco.

O seed e idempotente: usa o content_hash de cada regra para verificar se
ela ja existe antes de inserir.

Uso:
    ORKMIND_DATABASE_URL=postgresql://... python scripts/seed_governance.py
"""

from __future__ import annotations

import asyncio
import sys

from orkmind.core.config import load_config
from orkmind.core.injection import compute_content_hash
from orkmind.core.models import MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.store.factory import create_store

ANTI_DESTRUICAO = (
    "REGRA DE GOVERNANCA: Nenhum agente pode deletar, compactar, resumir "
    "destrutivamente ou sobrescrever memorias para abrir espaco. Todo "
    "conteudo e armazenado integralmente no OrkMind sem limite de espaco. "
    "O agente deve BUSCAR informacao via orkmind_recall ou orkmind_search, "
    "NUNCA consolidar ou reduzir memorias existentes. Violacoes desta regra "
    "sao falhas de governanca criticas."
)

GOVERNANCE_RULES: list[MemoryEntry] = [
    MemoryEntry(
        content=ANTI_DESTRUICAO,
        collection="rule",
        tags={"domain": ["governance"]},
        priority="critical",
        mandatory=True,
        protected=True,
        source="human",
        scope="global",
        metadata={"seed": "governance", "decision": "D-MA4"},
    ),
]


async def seed_governance(verbose: bool = True) -> dict[str, int]:
    """Insere as regras de governanca ausentes. Retorna contagens."""
    cfg = load_config()
    if not cfg.has_database:
        raise RuntimeError(
            "ORKMIND_DATABASE_URL nao configurado. "
            "Defina a variavel de ambiente ou ~/.orkmind/config.toml."
        )

    store = create_store(cfg)
    await store.initialize()
    layer = SemanticLayer(store, token_budget=cfg.token_budget)

    created = 0
    skipped = 0
    try:
        existing_hashes = await _existing_rule_hashes(store)
        for rule in GOVERNANCE_RULES:
            content_hash = compute_content_hash(rule.content)
            if content_hash in existing_hashes:
                skipped += 1
                if verbose:
                    print(f"[skip] regra ja existe (hash {content_hash[:12]}...)")
                continue
            rule.content_hash = content_hash
            entry_id, warnings = await layer.add_memory(rule)
            created += 1
            if verbose:
                print(f"[ok] regra criada: {entry_id}")
                for warning in warnings:
                    print(f"     aviso: {warning}")
    finally:
        await store.close()

    return {"created": created, "skipped": skipped}


async def _existing_rule_hashes(store) -> set[str]:  # type: ignore[no-untyped-def]
    """Coleta os content_hash das regras ja armazenadas."""
    entries = await store.search_by_tags(
        tags={}, collection="rule", mandatory_only=False, limit=1000
    )
    hashes: set[str] = set()
    for entry in entries:
        hashes.add(entry.content_hash or compute_content_hash(entry.content))
    return hashes


def main() -> int:
    result = asyncio.run(seed_governance())
    print(
        f"\nSeed de governanca concluido: "
        f"{result['created']} criada(s), {result['skipped']} ja existente(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
