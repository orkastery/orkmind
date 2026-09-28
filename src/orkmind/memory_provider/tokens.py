"""Estimativa de tokens sem tokenizer externo.

O provider so precisa de uma regua estavel e conservadora para dimensionar
chunks e janelas; nao vale arrastar um tokenizer de modelo especifico para
isso. A regra: cada palavra custa ceil(len/4) tokens, cada sinal custa 1. E
uma heuristica, nao uma medicao: quem precisa de contagem exata para um
modelo especifico deve medir com o tokenizer dele. A escolha de arredondar
cada palavra para cima e deliberada - num orcamento de contexto, errar para
mais e o lado seguro.
"""

from __future__ import annotations

import re

_PIECE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def _piece_cost(piece: str) -> int:
    if piece[0].isalnum() or piece[0] == "_":
        return -(-len(piece) // 4)
    return 1


def estimate_tokens(text: str) -> int:
    """Estimativa conservadora de tokens de `text` (0 para texto vazio)."""
    return sum(_piece_cost(m.group()) for m in _PIECE.finditer(text))


def head_by_tokens(text: str, budget: int, *, offset: int = 0) -> tuple[str, int]:
    """Fatia de `text` a partir do caractere `offset` que cabe em `budget` tokens.

    Devolve (fatia, offset_do_proximo_caractere). Corta em fronteira de peca,
    nunca no meio de uma palavra.
    """
    if budget <= 0 or offset >= len(text):
        return "", min(offset, len(text))
    spent = 0
    end = offset
    for match in _PIECE.finditer(text, offset):
        cost = _piece_cost(match.group())
        if spent + cost > budget:
            break
        spent += cost
        end = match.end()
    else:
        end = len(text)
    if end == offset:
        # Uma unica peca maior que o orcamento: anda mesmo assim, senao a
        # paginacao nunca termina.
        first = _PIECE.search(text, offset)
        end = first.end() if first else len(text)
    return text[offset:end], end


def tail_by_tokens(text: str, budget: int, *, at_least: int = 0, at_most: int | None = None) -> str:
    """Final de `text` que cabe em `budget` tokens, cortado em fronteira de peca.

    Cortar em fronteira de palavra pode parar um ou dois tokens ANTES de
    `budget`. Quando isso deixa o resultado abaixo de `at_least`, o corte
    avanca peca a peca ate alcancar o piso, sem passar de `at_most`.
    """
    if budget <= 0:
        return ""
    ceiling = budget if at_most is None else max(budget, at_most)
    spent = 0
    start = len(text)
    for match in reversed(list(_PIECE.finditer(text))):
        cost = _piece_cost(match.group())
        fits_budget = spent + cost <= budget
        reaches_floor = spent < at_least and spent + cost <= ceiling
        if not (fits_budget or reaches_floor):
            break
        spent += cost
        start = match.start()
    return text[start:]
