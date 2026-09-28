"""Seed das handoff-rules governadas (fase G3, D-MA12 reescopado).

Cria as entries `rule` protegidas com situation ["handoff-rules"] que
definem as secoes obrigatorias do pacote de handoff (progresso,
decisoes, referencias criticas, proximos passos). Sao a fonte de
verdade da validacao de `orkmind_handoff`; sem elas o validador usa os
defaults do modulo orkmind.guardrails.handoff (mesmo conteudo).

As entries NAO sao mandatory: elas nao pertencem ao bloco D-MA1 de
injecao incondicional; valem apenas na situacao handoff-rules. Sao
protegidas (protected=true, priority=critical) porque so humano pode
alterar o contrato do handoff, e publicas porque qualquer sessao
precisa le-las para montar o pacote.

O seed e idempotente: usa o content_hash de cada regra para verificar
se ela ja existe antes de inserir.

Uso:
    ORKMIND_DATABASE_URL=postgresql://... python scripts/seed_handoff_rules.py
"""

from __future__ import annotations

import asyncio
import sys

from orkmind.core.config import load_config
from orkmind.core.injection import compute_content_hash
from orkmind.core.models import MemoryEntry
from orkmind.core.semantic_layer import SemanticLayer
from orkmind.guardrails.handoff import (
    MIN_CHARS_SECAO_DEFAULT,
    SITUACAO_HANDOFF_RULES,
)
from orkmind.store.factory import create_store

# Conteudo de cada secao obrigatoria do pacote de handoff.
HANDOFF_SECTIONS: dict[str, str] = {
    "progresso": (
        "HANDOFF-RULE: o pacote de handoff DEVE conter a secao "
        "'progresso' descrevendo o que ja foi feito na sessao: tarefas "
        "concluidas, estado atual do trabalho e o ponto exato onde a "
        "sessao parou."
    ),
    "decisoes": (
        "HANDOFF-RULE: o pacote de handoff DEVE conter a secao "
        "'decisoes' registrando as decisoes tomadas na sessao e o porque "
        "de cada uma, incluindo alternativas descartadas quando "
        "relevantes."
    ),
    "referencias_criticas": (
        "HANDOFF-RULE: o pacote de handoff DEVE conter a secao "
        "'referencias_criticas' listando arquivos, ids de entries, "
        "comandos e links indispensaveis para continuar o trabalho sem "
        "redescoberta."
    ),
    "proximos_passos": (
        "HANDOFF-RULE: o pacote de handoff DEVE conter a secao "
        "'proximos_passos' com as acoes pendentes em ordem de prioridade "
        "e os criterios para considerar o trabalho concluido."
    ),
}


def handoff_rules() -> list[MemoryEntry]:
    """Entries `rule` protegidas que definem o contrato do handoff."""
    entries: list[MemoryEntry] = []
    for secao, conteudo in HANDOFF_SECTIONS.items():
        entries.append(
            MemoryEntry(
                content=conteudo,
                collection="rule",
                tags={
                    "situation": [SITUACAO_HANDOFF_RULES],
                    "domain": ["governance"],
                },
                priority="critical",
                mandatory=False,
                protected=True,
                source="human",
                scope="global",
                visibility="public",
                metadata={
                    "seed": "handoff-rules",
                    "decision": "D-MA12",
                    "handoff_section": secao,
                    "handoff_min_chars": MIN_CHARS_SECAO_DEFAULT,
                },
            )
        )
    return entries


async def seed_handoff_rules(
    layer: SemanticLayer | None = None, verbose: bool = True
) -> dict[str, int]:
    """Insere as handoff-rules ausentes. Retorna contagens.

    Aceita um SemanticLayer ja construido (testes); sem ele, abre o
    store do config, como os demais seeds.
    """
    proprio_store = None
    if layer is None:
        cfg = load_config()
        if not cfg.has_store:
            raise RuntimeError(
                "Nenhum backend de storage configurado. Defina "
                "ORKMIND_DATABASE_URL ou ~/.orkmind/config.toml."
            )
        proprio_store = create_store(cfg)
        await proprio_store.initialize()
        layer = SemanticLayer(proprio_store, token_budget=cfg.token_budget)

    created = 0
    skipped = 0
    try:
        existentes = await layer.store.search_by_tags(
            tags={"situation": [SITUACAO_HANDOFF_RULES]},
            collection="rule",
            limit=1000,
        )
        hashes = {
            e.content_hash or compute_content_hash(e.content)
            for e in existentes
        }
        for rule in handoff_rules():
            content_hash = compute_content_hash(rule.content)
            if content_hash in hashes:
                skipped += 1
                if verbose:
                    print(f"[skip] handoff-rule ja existe ({content_hash[:12]}...)")
                continue
            rule.content_hash = content_hash
            entry_id, warnings = await layer.add_memory(rule)
            created += 1
            if verbose:
                print(f"[ok] handoff-rule criada: {entry_id}")
                for warning in warnings:
                    print(f"     aviso: {warning}")
    finally:
        if proprio_store is not None:
            await proprio_store.close()

    return {"created": created, "skipped": skipped}


def main() -> int:
    result = asyncio.run(seed_handoff_rules())
    print(
        f"\nSeed de handoff-rules concluido: "
        f"{result['created']} criada(s), {result['skipped']} ja existente(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
