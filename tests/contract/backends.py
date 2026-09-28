"""Registro dos backends exercitados pela suite de contrato (F3.3).

Cada backend declara como construir o store, como limpar o alvo e o que
espera de `capabilities`. A limpeza NUNCA usa `adapter._get_conn()` e
chama `assert_destrutivo_permitido` na primeira linha: a resolucao do
alvo ja filtrou, e a assercao volta a checar no momento do apagamento
(cinto e suspensorio, regra 2 de 7.3.1).

Nenhuma funcao daqui chama `load_config()`. Os alvos vem exclusivamente
das variaveis `ORKMIND_TEST_*`, resolvidas em `tests/conftest.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from orkmind.core.config import OrkMindConfig
from orkmind.store.base import MemoryStore
from orkmind.store.capabilities import StoreCapabilities
from orkmind.store.factory import create_store
from orkmind.store.memory_adapter import MEMORY_CAPABILITIES
from orkmind.store.postgres_adapter import PGVECTOR_CAPABILITIES
from tests.conftest import AlvoDeTeste, assert_destrutivo_permitido, resolve_alvo_de_teste

# F3.4 subiu a governanca: `create_store` devolve um GovernedStore e todo
# backend passa pela mesma camada. Os xfail(strict=True) que existiram
# no commit anterior sao apagados por esta constante. Voltar isso para
# False sem remover a camada faria a suite falhar por XPASS, que e
# exatamente o alarme desejado.
GOVERNANCA_ATIVA = True

# Backends cujo adapter cru ja carrega a governanca em SQL como
# redundancia (D-7). Para eles nao existe xfail, nem antes nem depois.
BACKENDS_COM_GOVERNANCA_NO_ADAPTER = frozenset({"pgvector"})

# Alvo de Qdrant que roda dentro do proprio processo, pelo modo local do
# qdrant-client, sem servidor nenhum. Continua exigindo as duas variaveis
# ORKMIND_TEST_QDRANT_* com marcador: a guarda nao afrouxa, apenas
# reconhece que aqui nao existe alvo compartilhado a apagar.
ALVO_QDRANT_LOCAL = ":memory:"

# Tabela 3.2 do plano, mantida aqui como expectativa INDEPENDENTE da
# implementacao: se o adapter mudar o que declara, o contrato pega.
QDRANT_CAPABILITIES_ESPERADAS = StoreCapabilities(
    backend="qdrant",
    vector_search=True,
    accepts_external_vectors=True,
    stores_embedding=True,
    native_text_semantic=False,
    text_search="match",
    tag_filter=True,
    unique_content_hash=False,
    versioning=True,
    snapshots=True,
    parent_id=True,
    ttl_gc=True,
    durable=True,
    native_acl_filter=False,
    native_constitutional_order=False,
    backfill=True,
)


@dataclass(frozen=True)
class BackendDeTeste:
    """Um backend registrado na suite de contrato."""

    nome: str
    alvo: AlvoDeTeste
    construir: Callable[[], MemoryStore]
    limpar: Callable[[MemoryStore], None]
    capabilities_esperadas: StoreCapabilities

    @property
    def disponivel(self) -> bool:
        return self.alvo.destrutivo_ok

    @property
    def motivo_skip(self) -> str:
        return self.alvo.motivo_skip or f"backend {self.nome} indisponivel"


# --- pgvector ---------------------------------------------------------------


def _construir_pgvector(alvo: AlvoDeTeste) -> MemoryStore:
    return create_store(
        OrkMindConfig(store_backend="pgvector", database_url=alvo.alvo)
    )


def _limpar_pgvector(alvo: AlvoDeTeste, store: MemoryStore) -> None:
    """Apaga as tabelas do banco de teste APROVADO.

    Inclui `profiles` porque, a partir de F3.7, a suite de contrato tambem
    exercita identidades. Abre conexao propria a partir do alvo aprovado:
    nada de pescar a conexao interna do adapter.
    """
    assert_destrutivo_permitido(alvo)
    import psycopg

    with psycopg.connect(alvo.alvo, autocommit=True) as conn:
        conn.execute("DELETE FROM snapshot_entries")
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM memory_versions")
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM profiles")


# --- memory -----------------------------------------------------------------


def _construir_memory(alvo: AlvoDeTeste) -> MemoryStore:
    return create_store(OrkMindConfig(store_backend="memory"))


def _limpar_memory(alvo: AlvoDeTeste, store: MemoryStore) -> None:
    """Descarta os dicts do proprio processo. Risco zero, guarda mesmo assim."""
    assert_destrutivo_permitido(alvo)
    interno = getattr(store, "inner", store)
    for atributo in ("_entries", "_versions", "_version_acesso", "_snapshots",
                     "_snapshot_entries"):
        alvo_dict = getattr(interno, atributo, None)
        if isinstance(alvo_dict, dict):
            alvo_dict.clear()


# --- qdrant -----------------------------------------------------------------


def _construir_qdrant(alvo: AlvoDeTeste) -> MemoryStore:
    return create_store(
        OrkMindConfig(
            store_backend="qdrant",
            store_options={
                "url": alvo.alvo,
                "collection": f"{alvo.prefixo}memories",
                "prefix": alvo.prefixo,
            },
        )
    )


def _limpar_qdrant(alvo: AlvoDeTeste, store: MemoryStore) -> None:
    """Apaga SOMENTE as colecoes cujo nome comeca pelo prefixo aprovado.

    Um `delete_collection` sem prefixo checado seria a versao Qdrant do
    incidente de 29/08. A enumeracao e explicita e o filtro e por prefixo,
    nunca "apagar tudo".
    """
    assert_destrutivo_permitido(alvo)
    if not alvo.prefixo:
        raise RuntimeError(
            "Limpeza de Qdrant sem prefixo aprovado. Operacao recusada."
        )
    if alvo.alvo.strip() == ALVO_QDRANT_LOCAL:
        # Instancia local do proprio processo (modo local do
        # qdrant-client, sem servidor). Nao ha nada compartilhado para
        # apagar: cada store nasce vazio e morre com o processo. A
        # assercao acima roda antes mesmo assim, de proposito.
        return
    from qdrant_client import QdrantClient

    cliente = QdrantClient(location=alvo.alvo)
    try:
        for colecao in cliente.get_collections().collections:
            if colecao.name.startswith(alvo.prefixo):
                cliente.delete_collection(colecao.name)
    finally:
        cliente.close()


_REGISTRO: dict[str, tuple] = {
    "pgvector": (_construir_pgvector, _limpar_pgvector, PGVECTOR_CAPABILITIES),
    "memory": (_construir_memory, _limpar_memory, MEMORY_CAPABILITIES),
    "qdrant": (_construir_qdrant, _limpar_qdrant, QDRANT_CAPABILITIES_ESPERADAS),
}

NOMES_REGISTRADOS: tuple[str, ...] = ("memory", "pgvector", "qdrant")


def obter_backend(nome: str) -> BackendDeTeste:
    """Backend de teste pronto para uso, com o alvo ja resolvido."""
    construir, limpar, capabilities = _REGISTRO[nome]
    alvo = resolve_alvo_de_teste(nome)
    return BackendDeTeste(
        nome=nome,
        alvo=alvo,
        construir=lambda: construir(alvo),
        limpar=lambda store: limpar(alvo, store),
        capabilities_esperadas=capabilities,
    )


def backends_disponiveis() -> list[str]:
    """Nomes dos backends que tem alvo aprovado nesta maquina."""
    disponiveis = []
    for nome in NOMES_REGISTRADOS:
        try:
            if obter_backend(nome).disponivel:
                disponiveis.append(nome)
        except RuntimeError:
            # Alvo mal configurado falha alto no proprio teste, nao aqui.
            continue
    return disponiveis


def exige_capability(
    store: MemoryStore, campo: str, esperado: object = True
) -> Optional[str]:
    """Motivo de skip quando o backend nao declara a capacidade pedida.

    Devolve None quando pode rodar. O texto sempre nomeia a capability:
    skip silencioso e proibido (8.1).
    """
    cap = store.capabilities
    atual = getattr(cap, campo)
    if atual == esperado:
        return None
    return (
        f"backend {cap.backend} declara {campo}={atual!r} "
        f"(esperado {esperado!r} para este teste)"
    )
