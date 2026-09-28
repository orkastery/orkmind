"""Geracao de camadas de contexto E1/E2/E3 para OrkMind."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from orkmind.core.models import MemoryEntry

Layer = Literal["essence", "structure", "source"]

# Limites de caracteres (~tokens * 4)
ESSENCE_MAX_CHARS = 400
STRUCTURE_MAX_CHARS = 8000


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Trunca texto em fronteira de frase, NUNCA corta no meio de uma palavra.

    Usa '. ' e '\\n' como delimitadores de frase.
    """
    if len(text) <= max_chars:
        return text

    # Procurar ultima fronteira de frase antes do limite
    candidate = text[:max_chars]

    # Tentar '. ' primeiro (fronteira de frase mais forte)
    last_period = candidate.rfind(". ")
    last_newline = candidate.rfind("\n")

    # Usar a fronteira mais proxima do limite
    best = max(last_period, last_newline)

    if best > 0:
        if best == last_period:
            return text[: best + 1]  # inclui o ponto
        return text[:best]

    # Fallback: fronteira de palavra (espaco)
    last_space = candidate.rfind(" ")
    if last_space > 0:
        return text[:last_space]

    # Ultimo recurso: retornar o maximo sem cortar palavra
    return candidate


def _truncate_at_paragraph(text: str, max_chars: int) -> str:
    """Trunca texto em fronteira de paragrafo, preservando paragrafos completos."""
    if len(text) <= max_chars:
        return text

    paragraphs = text.split("\n\n")
    result: list[str] = []
    total = 0

    for p in paragraphs:
        # +2 para o separador '\n\n' entre paragrafos
        sep_len = 2 if result else 0
        if total + sep_len + len(p) > max_chars:
            break
        result.append(p)
        total += sep_len + len(p)

    if result:
        return "\n\n".join(result)

    # Fallback: truncar em fronteira de frase se nenhum paragrafo completo cabe
    return _truncate_at_sentence(text, max_chars)


def generate_essence(content: str) -> str:
    """Gera camada E1 (essencia, ~100 tokens / ~400 chars).

    Preserva primeira frase completa + trunca em fronteira de frase.
    """
    return _truncate_at_sentence(content, ESSENCE_MAX_CHARS)


def generate_structure(content: str) -> str:
    """Gera camada E2 (estrutura, ~2000 tokens / ~8000 chars).

    Preserva paragrafos completos + trunca em fronteira de paragrafo.
    """
    return _truncate_at_paragraph(content, STRUCTURE_MAX_CHARS)


def generate_layers(entry: MemoryEntry) -> MemoryEntry:
    """Preenche essence + structure + layer_generated_at.

    Se entry.mandatory, essence e structure = content (SEMPRE E3 integral).
    """
    if entry.mandatory:
        entry.essence = entry.content
        entry.structure = entry.content
    else:
        entry.essence = generate_essence(entry.content)
        entry.structure = generate_structure(entry.content)
    entry.layer_generated_at = datetime.now(timezone.utc)
    return entry


def get_content_for_layer(entry: MemoryEntry, layer: Layer) -> str:
    """Retorna conteudo da camada solicitada, com fallback.

    Fallback: essence=None -> structure; structure=None -> content (E3).
    """
    if layer == "essence":
        return entry.essence or entry.structure or entry.content
    elif layer == "structure":
        return entry.structure or entry.content
    return entry.content
