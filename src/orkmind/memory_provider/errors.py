"""Erros do Memory Provider nativo."""

from __future__ import annotations


class MemoryProviderError(Exception):
    """Base de todos os erros do provider."""


class SchemaMismatchError(MemoryProviderError):
    """O schema instalado nao bate com a configuracao (dimensao ou idioma).

    Trocar a dimensao do embedding invalida todos os vetores ja gravados;
    por isso a migracao se recusa a seguir em vez de recriar a coluna.
    """


class CoreMemoryMissingError(MemoryProviderError):
    """Bloco obrigatorio da Core Memory ausente para o agente.

    `persona` e `system_invariants` sao pinados compulsoriamente. Rodar o
    agente sem eles e falha de governanca, nao degradacao aceitavel.
    """


class EmbeddingDimensionError(MemoryProviderError):
    """O provider de embedding devolveu vetor de dimensao diferente do schema."""


class IngestionQueueFullError(MemoryProviderError):
    """A fila de ingestao assincrona esta cheia; o chamador decide se reenvia."""


class DocumentNotFoundError(MemoryProviderError):
    """Documento inexistente OU fora do escopo de quem pediu.

    Os dois casos devolvem o mesmo erro de proposito: distinguir "nao existe"
    de "voce nao pode ver" ja vaza a existencia de um documento sigiloso.
    """


class WriteOutOfScopeError(MemoryProviderError):
    """Escrita que sairia do alcance de quem escreve.

    A regra da escrita e a da leitura: quem grava so grava o que conseguiria
    ler. Sem ela, qualquer papel poderia sobrescrever um documento sigiloso
    reaproveitando o slug, ou rebaixar a classificacao dele no caminho.
    """


class ClassificationOutOfReachError(WriteOutOfScopeError):
    """A classificacao pedida ficaria fora do alcance do proprio escritor.

    Nivel acima do papel dele ou departamentos que ele nao le. Recusado antes
    de fatiar ou chamar a API de embedding.
    """


class SlugUnavailableError(WriteOutOfScopeError):
    """O slug ja pertence a um documento fora do alcance de quem escreve.

    Slug e unico no acervo inteiro, entao dizer "em uso" e o minimo que a
    recusa precisa revelar. Nada alem disso sai: nem titulo, nem nivel, nem
    departamento do documento que ocupa o nome.
    """
