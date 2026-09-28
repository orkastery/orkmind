"""Extracao automatica de memoria de sessao para OrkMind (D9)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel

from orkmind.core.models import MemoryEntry
from orkmind.core.ontology import validate_entry

# Colecoes permitidas para extracao automatica
SOFT_COLLECTIONS = ("fact", "preference", "learning", "content")

MAX_ENTRIES_DEFAULT = 10

EXTRACTION_PROMPT = """\
Analise a sessao abaixo e extraia memorias relevantes.

REGRAS:
- Extraia APENAS fatos, preferencias, aprendizados ou conteudo.
- Colecoes permitidas: fact, preference, learning, content
- NUNCA sugira colecao "rule" ou "instruction"
- NUNCA marque mandatory=true
- Formato de saida: JSON array

Exemplo de saida:
[
  {
    "content": "Usuario prefere commits atomicos",
    "collection": "preference",
    "tags": {"skill": ["git"]},
    "priority": "medium"
  }
]

Exemplos NEGATIVOS (NUNCA faca isso):
- {"collection": "rule", ...}  -> PROIBIDO
- {"collection": "instruction", ...}  -> PROIBIDO
- {"mandatory": true, ...}  -> PROIBIDO

SESSAO:
{transcript}

Extraia as memorias como JSON array:
"""


class RawExtraction(BaseModel):
    """Saida do parser de extracao."""

    content: str
    collection: str = "fact"
    tags: dict[str, list[str]] = {}
    priority: str = "medium"


class ExtractionResult(BaseModel):
    """Resultado de uma extracao com warnings."""

    entry: MemoryEntry
    warnings: list[str] = []
    downgraded: bool = False


class ExtractionProvider(ABC):
    """Interface para providers de extracao."""

    @abstractmethod
    async def extract(self, transcript: str) -> list[RawExtraction]:
        """Extrai memorias de um transcript via LLM."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Nome do provider."""


async def extract_from_session(
    transcript: str,
    provider: ExtractionProvider,
    max_entries: int = MAX_ENTRIES_DEFAULT,
) -> list[ExtractionResult]:
    """Extrai memorias de uma sessao com guardrails de 3 camadas.

    Camada 1: prompt (no provider)
    Camada 2: validacao pos-parse (hardcoded)
    Camada 3: validate_entry (ontology D4+D9)

    Entries NAO sao armazenadas - o caller decide.
    """
    if not transcript or not transcript.strip():
        return []

    # Chamar provider (Camada 1: prompt)
    raw_extractions = await provider.extract(transcript)

    # Limitar a max_entries
    raw_extractions = raw_extractions[:max_entries]

    results: list[ExtractionResult] = []
    for raw in raw_extractions:
        warnings: list[str] = []
        downgraded = False

        # Camada 2: validacao pos-parse (hardcoded)
        collection = raw.collection
        if collection not in SOFT_COLLECTIONS:
            warnings.append(
                f"Collection '{collection}' downgraded para 'fact' "
                "(apenas fact/preference/learning/content permitidos)"
            )
            collection = "fact"
            downgraded = True

        # Construir MemoryEntry com campos FORCADOS
        entry = MemoryEntry(
            content=raw.content,
            collection=collection,  # type: ignore[arg-type]
            tags=raw.tags,
            priority=raw.priority,  # type: ignore[arg-type]
            mandatory=False,  # FORCADO
            source="agent",  # FORCADO
            protected=False,  # FORCADO
            scope="session",  # FORCADO
        )

        # Camada 3: validate_entry (ontology - computa content_hash, detect_injection)
        validation_warnings = validate_entry(entry)
        warnings.extend(validation_warnings)

        results.append(ExtractionResult(
            entry=entry,
            warnings=warnings,
            downgraded=downgraded,
        ))

    return results
