"""Funcoes puras de governanca do OrkMind (F3.4).

Sem I/O, sem async, sem banco. Toda decisao de politica do produto vive
aqui e e testavel sem servico externo:

- protecao D2 (`protected` / `priority="critical"` contra `source="agent"`)
- ACL de leitura e de escrita sobre identidades ja resolvidas
- filtro de expiracao e de `injection_risk`
- ordenacao constitucional total e particao estavel por relevancia

O que era decisao dentro do `PostgresAdapter` foi promovido para ca
(DP-4). O SQL do adapter continua no lugar por um ciclo como redundancia
(divida D-7): `tests/integration/test_acl_perfis.py` prova que os dois
caminhos concordam.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Literal, Optional

from orkmind.core.models import MemoryEntry
from orkmind.core.ontology import (
    PERMISSION_MSG_WRITE,
    PROTECTION_MSG_DELETE,
    PROTECTION_MSG_UPDATE,
    ProtectionError,
)

Operacao = Literal["update", "delete"]

# Ordem constitucional de prioridade. Espelha o CASE do SQL em
# postgres_adapter.py, para que a ordem seja identica entre backends.
ORDEM_PRIORIDADE = {"critical": 0, "high": 1, "medium": 2, "low": 3}

_MENSAGEM_POR_OPERACAO = {
    "update": PROTECTION_MSG_UPDATE,
    "delete": PROTECTION_MSG_DELETE,
}


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _com_tz(valor: Optional[datetime]) -> Optional[datetime]:
    if valor is None:
        return None
    if valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


# --- Protecao D2 ------------------------------------------------------------


def e_protegida(entry: MemoryEntry) -> bool:
    """Entry sob protecao D2: `protected=True` ou `priority='critical'`."""
    return bool(entry.protected) or entry.priority == "critical"


def assert_nao_protegida(
    entry: MemoryEntry, source: str, operacao: Operacao
) -> None:
    """I1: agente nunca altera nem apaga entry protegida.

    A mensagem e a canonica de `core/ontology.py`, identica em todos os
    backends (prova A5 da equivalencia).
    """
    if e_protegida(entry) and source == "agent":
        raise ProtectionError(_MENSAGEM_POR_OPERACAO[operacao].format(id=entry.id))


# --- ACL --------------------------------------------------------------------


def pode_ler(
    entry: MemoryEntry,
    requester_id: Optional[str],
    identidades: Optional[Iterable[str]] = None,
) -> bool:
    """Regra de leitura, promovida de `PostgresAdapter.can_read`.

    Sem `requester_id` o comportamento anterior a F2 e preservado: tudo e
    visivel (I3, segunda metade).
    """
    if not requester_id:
        return True
    if entry.visibility == "public":
        return True
    efetivas = set(identidades or [requester_id])
    if entry.author_id and entry.author_id in efetivas:
        return True
    if entry.visibility == "restricted":
        audience = entry.tags.get("audience") or []
        if efetivas & set(audience):
            return True
    return False


def assert_pode_escrever(
    entry: MemoryEntry,
    requester_id: Optional[str],
    identidades: Optional[Iterable[str]] = None,
) -> None:
    """Regra de escrita, promovida de `PostgresAdapter._assert_can_write`.

    O autor sempre pode escrever; quem estiver na dimensao de tag
    `editors` tambem pode; o coringa '*' libera para todos. Sem
    `requester_id` o comportamento anterior e preservado.

    I2: esta funcao e chamada DEPOIS de `assert_nao_protegida`, que
    prevalece. Estar em `editors` nao autoriza alterar entry protegida.
    """
    if not requester_id:
        return
    efetivas = set(identidades or [requester_id])
    if entry.author_id and entry.author_id in efetivas:
        return
    editors = entry.tags.get("editors") or []
    if "*" in editors or efetivas & set(editors):
        return
    raise PermissionError(
        PERMISSION_MSG_WRITE.format(requester=requester_id, id=entry.id)
    )


# --- Filtros ----------------------------------------------------------------


def filtrar_visiveis(
    entries: Iterable[MemoryEntry],
    requester_id: Optional[str],
    identidades: Optional[Iterable[str]] = None,
) -> list[MemoryEntry]:
    """I3: com `requester_id`, so passa o que ele pode ler."""
    if not requester_id:
        return list(entries)
    efetivas = list(identidades or [requester_id])
    return [e for e in entries if pode_ler(e, requester_id, efetivas)]


def filtrar_ativas(
    entries: Iterable[MemoryEntry], agora: Optional[datetime] = None
) -> list[MemoryEntry]:
    """I4: entry expirada nunca aparece em busca."""
    referencia = agora or _agora()
    ativas: list[MemoryEntry] = []
    for entry in entries:
        expira = _com_tz(entry.expires_at)
        if expira is not None and expira <= referencia:
            continue
        ativas.append(entry)
    return ativas


def filtrar_sem_injection(entries: Iterable[MemoryEntry]) -> list[MemoryEntry]:
    """I5: `injection_risk=True` nunca aparece em `search_by_tags`."""
    return [e for e in entries if not e.injection_risk]


# --- Ordenacao --------------------------------------------------------------


def ordenar_constitucional(entries: Iterable[MemoryEntry]) -> list[MemoryEntry]:
    """I9: ordem TOTAL, identica entre backends.

    `mandatory` primeiro, depois prioridade constitucional, depois
    `updated_at` mais recente, e `id` como desempate final (DD-3). O
    desempate por id e o que torna a ordem total: sem ele, empates
    ficavam sem criterio e a comparacao entre backends seria instavel.
    """
    referencia = _agora()

    def chave(entry: MemoryEntry) -> tuple:
        atualizada = _com_tz(entry.updated_at) or referencia
        return (
            0 if entry.mandatory else 1,
            ORDEM_PRIORIDADE.get(entry.priority, 99),
            -atualizada.timestamp(),
            entry.id,
        )

    return sorted(entries, key=chave)


def particionar_mandatory_primeiro(
    entries: Iterable[MemoryEntry],
) -> list[MemoryEntry]:
    """DD-4: particao estavel, preservando a ordem de relevancia.

    As `mandatory` vao para a frente; dentro de cada grupo, a ordem
    devolvida pelo backend e mantida intacta. Reproduz o que o SQL faz
    hoje (`ORDER BY mandatory DESC, rank DESC`) sem que a camada precise
    conhecer o score.
    """
    obrigatorias: list[MemoryEntry] = []
    demais: list[MemoryEntry] = []
    for entry in entries:
        (obrigatorias if entry.mandatory else demais).append(entry)
    return obrigatorias + demais


# --- Over-fetch (DD-5) ------------------------------------------------------


def limite_de_candidatos(
    limit: int,
    ordena_nativamente: bool,
    filtra_acl_nativamente: bool,
    tem_requester: bool,
    teto: int = 500,
) -> int:
    """Quantos candidatos pedir ao backend antes de filtrar e ordenar.

    Quando o backend ja ordena `mandatory` primeiro e ja aplica o filtro
    de leitura no motor, nao ha over-fetch: o `limit` enviado e o `limit`
    pedido. Essa e a garantia mecanica de nao-regressao do pgvector
    (DP-9). Caso contrario, truncar no `limit` do backend poderia perder
    uma entry `mandatory`, entao pedimos uma janela maior.
    """
    if limit <= 0:
        return limit
    precisa_ordenar = not ordena_nativamente
    precisa_filtrar_acl = tem_requester and not filtra_acl_nativamente
    if not precisa_ordenar and not precisa_filtrar_acl:
        return limit
    return min(max(limit * 4, limit + 50), max(teto, limit))
