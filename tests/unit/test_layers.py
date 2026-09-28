"""Testes para camadas de contexto E1/E2/E3 (D6)."""

from __future__ import annotations

from orkmind.core.layers import (
    ESSENCE_MAX_CHARS,
    STRUCTURE_MAX_CHARS,
    generate_essence,
    generate_layers,
    generate_structure,
    get_content_for_layer,
)
from orkmind.core.models import MemoryEntry


class TestGenerateEssence:
    def test_short_content_returns_integral(self) -> None:
        """Content < 400 chars retorna integral."""
        short = "Esta e uma frase curta."
        assert generate_essence(short) == short

    def test_long_content_truncates_at_sentence(self) -> None:
        """Trunca em fronteira de frase para content longo."""
        # Criar content com multiplas frases que excede 400 chars
        sentences = ["Esta e a frase numero %d do conteudo." % i for i in range(20)]
        content = " ".join(sentences)
        assert len(content) > ESSENCE_MAX_CHARS
        result = generate_essence(content)
        assert len(result) <= ESSENCE_MAX_CHARS
        # Nao corta no meio de palavra
        assert not result.endswith(" ")
        # Termina com frase completa (ponto)
        assert result.endswith(".")

    def test_preserves_first_sentence(self) -> None:
        """Primeira frase sempre completa."""
        first = "Primeira frase completa."
        rest = " " + "x" * 500
        content = first + rest
        result = generate_essence(content)
        assert result.startswith("Primeira frase completa.")


class TestGenerateStructure:
    def test_short_content_returns_integral(self) -> None:
        """Content < 8000 chars retorna integral."""
        short = "Paragrafo curto.\n\nSegundo paragrafo."
        assert generate_structure(short) == short

    def test_long_content_truncates_at_paragraph(self) -> None:
        """Trunca em fronteira de paragrafo para content longo."""
        paragraphs = ["Paragrafo %d com conteudo extenso. " % i + "x" * 200 for i in range(50)]
        content = "\n\n".join(paragraphs)
        assert len(content) > STRUCTURE_MAX_CHARS
        result = generate_structure(content)
        assert len(result) <= STRUCTURE_MAX_CHARS
        # Deve terminar com um paragrafo completo (sem corte no meio)
        assert not result.endswith("\n\n")


class TestGenerateLayers:
    def test_mandatory_always_e3(self) -> None:
        """Mandatory entries: essence=structure=content."""
        entry = MemoryEntry(
            content="Conteudo mandatorio importante. " * 50,
            collection="rule",
            mandatory=True,
        )
        result = generate_layers(entry)
        assert result.essence == result.content
        assert result.structure == result.content

    def test_sets_timestamp(self) -> None:
        """layer_generated_at preenchido apos geracao."""
        entry = MemoryEntry(content="teste", collection="fact")
        assert entry.layer_generated_at is None
        generate_layers(entry)
        assert entry.layer_generated_at is not None


class TestGetContentForLayer:
    def test_fallback_essence_none(self) -> None:
        """essence=None -> retorna structure -> retorna content."""
        entry = MemoryEntry(content="full content", collection="fact")
        # Sem essence nem structure
        assert get_content_for_layer(entry, "essence") == "full content"
        # Com structure mas sem essence
        entry.structure = "structured content"
        assert get_content_for_layer(entry, "essence") == "structured content"
        # Com essence
        entry.essence = "essential"
        assert get_content_for_layer(entry, "essence") == "essential"

    def test_explicit_layers(self) -> None:
        """Cada camada retorna campo correto."""
        entry = MemoryEntry(content="full", collection="fact")
        entry.essence = "ess"
        entry.structure = "struct"
        assert get_content_for_layer(entry, "essence") == "ess"
        assert get_content_for_layer(entry, "structure") == "struct"
        assert get_content_for_layer(entry, "source") == "full"
