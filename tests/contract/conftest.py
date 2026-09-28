"""Fixtures e marcadores da suite de contrato parametrizada (F3.3)."""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest
import pytest_asyncio

from orkmind.store.base import MemoryStore
from tests.contract.backends import (
    BACKENDS_COM_GOVERNANCA_NO_ADAPTER,
    GOVERNANCA_ATIVA,
    NOMES_REGISTRADOS,
    obter_backend,
)


@pytest_asyncio.fixture(params=NOMES_REGISTRADOS, ids=NOMES_REGISTRADOS)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[MemoryStore]:
    """Store do backend parametrizado, limpo antes e depois.

    Quando o backend nao tem alvo aprovado nesta maquina, o skip carrega
    o motivo por extenso: `pytest -rs` mostra exatamente o que faltou.
    """
    backend = obter_backend(request.param)
    if not backend.disponivel:
        pytest.skip(backend.motivo_skip)

    instancia = backend.construir()
    await instancia.initialize()
    backend.limpar(instancia)
    if request.param == "qdrant":
        # A limpeza do Qdrant apaga colecoes inteiras; recriar e parte da
        # limpeza, nao do teste.
        await instancia.initialize()
    try:
        yield instancia
    finally:
        backend.limpar(instancia)
        await instancia.close()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[Any]
) -> None:
    """Marca xfail(strict=True) os testes de governanca sem governanca.

    Enquanto `GOVERNANCA_ATIVA` for False (ate F3.4), os backends cujo
    adapter nao carrega politica nenhuma FALHAM nos testes marcados com
    `@pytest.mark.governanca`. Isso e declarado, nao escondido: se um
    desses testes passar antes da hora, o `strict` derruba a suite. E o
    que impede alguem de resolver o problema colocando politica dentro do
    adapter (R0.2).
    """
    if GOVERNANCA_ATIVA:
        return
    for item in items:
        if item.get_closest_marker("governanca") is None:
            continue
        parametros = getattr(item, "callspec", None)
        backend = parametros.params.get("store") if parametros else None
        if backend is None or backend in BACKENDS_COM_GOVERNANCA_NO_ADAPTER:
            continue
        item.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=(
                    f"governanca sobe em F3.4: o adapter '{backend}' nao "
                    f"contem politica por desenho (R0.2)"
                ),
            )
        )
