"""Excecoes do subsistema de storage do OrkMind.

As duas primeiras herdam de ValueError de proposito: `create_store`
levantava ValueError antes do F3 e ha consumidores que capturam por
tipo. Quem captura por tipo nao quebra; quem captura por texto quebra,
e isso esta declarado no CHANGELOG (DD-7).
"""

from __future__ import annotations


class BackendDesconhecidoError(ValueError):
    """Nome de backend nao registrado em STORE_BACKENDS."""


class BackendMalConfiguradoError(ValueError):
    """Backend registrado, mas sem a configuracao minima (URL, options)."""


class CapacidadeIndisponivelError(RuntimeError):
    """O backend declarou nao suportar a operacao pedida.

    Existe para que a ausencia de capacidade seja ALTA e nomeada, nunca
    uma lista vazia silenciosa (R0.3).
    """
