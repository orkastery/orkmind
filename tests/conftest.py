"""Shared test fixtures for OrkMind."""

from __future__ import annotations

import os
from typing import NamedTuple
from urllib.parse import urlparse

import pytest

from orkmind.core.models import MemoryEntry

# --- Guarda de seguranca do banco de testes ---------------------------------
#
# As fixtures de integracao e contrato APAGAM todas as linhas do banco
# apontado. Em 2026-08-29 uma execucao de `pytest tests/` com
# ORKMIND_DATABASE_URL apontando para a base de producao destruiu 18
# entries reais. A guarda abaixo torna isso impossivel: o banco so e
# aceito se o nome identificar claramente uma base de testes.

TEST_DB_MARKERS = ("test", "_ci", "sandbox")

TEST_DB_ENV = "ORKMIND_TEST_DATABASE_URL"
FALLBACK_DB_ENV = "ORKMIND_DATABASE_URL"


def _database_name(url: str) -> str:
    return urlparse(url).path.lstrip("/").lower()


def is_test_database(url: str) -> bool:
    """True quando o nome do banco identifica uma base descartavel."""
    if not url:
        return False
    nome = _database_name(url)
    return any(marker in nome for marker in TEST_DB_MARKERS)


def resolve_test_database_url() -> str:
    """URL do banco onde e seguro rodar testes destrutivos.

    Prioriza ORKMIND_TEST_DATABASE_URL. Aceita ORKMIND_DATABASE_URL
    apenas se o nome do banco tiver um marcador de teste. Caso
    contrario devolve string vazia e os testes sao pulados.
    """
    explicito = os.environ.get(TEST_DB_ENV, "")
    if explicito:
        if not is_test_database(explicito):
            raise RuntimeError(
                f"{TEST_DB_ENV} aponta para '{_database_name(explicito)}', "
                f"que nao parece um banco de testes. Os testes de integracao "
                f"APAGAM todos os dados. Use um banco com 'test' no nome."
            )
        return explicito

    fallback = os.environ.get(FALLBACK_DB_ENV, "")
    if fallback and is_test_database(fallback):
        return fallback
    return ""


SKIP_NO_TEST_DB = (
    f"Sem banco de testes. Defina {TEST_DB_ENV} apontando para uma base "
    f"descartavel (nome contendo 'test'). {FALLBACK_DB_ENV} so e aceito "
    f"se o nome do banco tiver marcador de teste - os testes de "
    f"integracao apagam todos os dados."
)


# --- Guarda destrutiva generalizada por backend (F3.3) -----------------------
#
# A parametrizacao da suite de contrato multiplica os caminhos
# destrutivos: agora ha mais de um tipo de alvo (banco Postgres, dicts do
# processo, colecoes de Qdrant). O bloco acima NAO foi alterado: ele
# continua sendo a guarda do pgvector, byte a byte. O que vem abaixo
# generaliza a mesma ideia para os outros backends, sempre por adicao e
# sempre MAIS rigido:
#
#   1. Backend novo nao herda fallback de variavel de producao. O
#      fallback de ORKMIND_DATABASE_URL existe por legado no pgvector e
#      so la.
#   2. assert_destrutivo_permitido e chamada no inicio de TODA fixture
#      que apaga qualquer coisa. Cinto e suspensorio.
#   3. A limpeza do Qdrant nunca apaga tudo: enumera e apaga somente as
#      colecoes cujo nome comeca pelo prefixo aprovado.
#   4. Coincidencia com o alvo de producao e ERRO, nao aviso.
#   5. A suite nunca chama load_config() para descobrir ONDE ESCREVER. A
#      unica leitura de config de producao permitida e a da regra 4, que
#      serve exclusivamente para RECUSAR um alvo.

QDRANT_TEST_URL_ENV = "ORKMIND_TEST_QDRANT_URL"
QDRANT_TEST_PREFIX_ENV = "ORKMIND_TEST_QDRANT_PREFIX"
QDRANT_PROD_URL_ENV = "ORKMIND_QDRANT_URL"

SENTINELA_MEMORY = "processo-local"

SKIP_NO_TEST_QDRANT = (
    f"Sem instancia de teste do Qdrant. Defina {QDRANT_TEST_URL_ENV} e "
    f"{QDRANT_TEST_PREFIX_ENV} (prefixo contendo 'test', '_ci' ou "
    f"'sandbox'). Nao ha fallback a partir de {QDRANT_PROD_URL_ENV}: os "
    f"testes apagam todas as colecoes com o prefixo aprovado."
)


class AlvoDeTeste(NamedTuple):
    """Onde um backend pode ser exercitado nesta maquina.

    `destrutivo_ok` so e True quando a guarda do backend aprovou o alvo.
    `motivo_skip` e o texto ruidoso que aparece no relatorio quando a
    aprovacao nao veio. Skip silencioso e proibido.
    """

    backend: str
    alvo: str
    destrutivo_ok: bool
    motivo_skip: str
    prefixo: str = ""


def _tem_marcador(texto: str) -> bool:
    return any(marker in texto.lower() for marker in TEST_DB_MARKERS)


def _url_qdrant_de_producao() -> str:
    """URL de Qdrant declarada como producao, se houver.

    Usada SOMENTE para recusar um alvo (regra 4), nunca como destino de
    escrita. A leitura do config real e best-effort e nunca derruba a
    coleta de testes.
    """
    da_env = os.environ.get(QDRANT_PROD_URL_ENV, "")
    if da_env:
        return da_env
    try:
        from orkmind.core.config import load_config

        return str(load_config().store_options.get("url", "") or "")
    except Exception:
        return ""


def _alvo_pgvector() -> AlvoDeTeste:
    url = resolve_test_database_url()
    return AlvoDeTeste(
        backend="pgvector",
        alvo=url,
        destrutivo_ok=bool(url),
        motivo_skip="" if url else SKIP_NO_TEST_DB,
    )


def _alvo_memory() -> AlvoDeTeste:
    # Risco zero: o alvo e um dict do proprio processo de teste.
    return AlvoDeTeste(
        backend="memory",
        alvo=SENTINELA_MEMORY,
        destrutivo_ok=True,
        motivo_skip="",
    )


def _alvo_qdrant() -> AlvoDeTeste:
    url = os.environ.get(QDRANT_TEST_URL_ENV, "")
    prefixo = os.environ.get(QDRANT_TEST_PREFIX_ENV, "")
    recusado = AlvoDeTeste(
        backend="qdrant",
        alvo="",
        destrutivo_ok=False,
        motivo_skip=SKIP_NO_TEST_QDRANT,
    )

    if not url or not prefixo:
        return recusado

    if not _tem_marcador(prefixo):
        raise RuntimeError(
            f"{QDRANT_TEST_PREFIX_ENV} vale '{prefixo}', que nao tem marcador "
            f"de teste ({', '.join(TEST_DB_MARKERS)}). Os testes APAGAM todas "
            f"as colecoes com esse prefixo. Use um prefixo como "
            f"'orkmind_test_'."
        )

    producao = _url_qdrant_de_producao()
    if producao and producao.strip().rstrip("/") == url.strip().rstrip("/"):
        raise RuntimeError(
            f"{QDRANT_TEST_URL_ENV} aponta para a mesma instancia declarada "
            f"em producao ('{producao}'). Coincidencia suspeita e erro, nao "
            f"aviso: use uma instancia de Qdrant separada para teste."
        )

    return AlvoDeTeste(
        backend="qdrant",
        alvo=url,
        destrutivo_ok=True,
        motivo_skip="",
        prefixo=prefixo,
    )


_RESOLVEDORES = {
    "pgvector": _alvo_pgvector,
    "memory": _alvo_memory,
    "qdrant": _alvo_qdrant,
}


def resolve_alvo_de_teste(backend: str) -> AlvoDeTeste:
    """Alvo aprovado (ou recusado, com motivo) para um backend."""
    resolvedor = _RESOLVEDORES.get(backend)
    if resolvedor is None:
        raise RuntimeError(
            f"Backend de teste '{backend}' desconhecido. Conhecidos: "
            f"{', '.join(sorted(_RESOLVEDORES))}."
        )
    return resolvedor()


def assert_destrutivo_permitido(alvo: AlvoDeTeste) -> None:
    """Ultima barreira antes de apagar qualquer coisa.

    Chamada OBRIGATORIA na primeira linha de toda fixture destrutiva.
    Levanta RuntimeError se destrutivo_ok for False. Nunca faz skip
    aqui: skip e decisao da fixture, ANTES.
    """
    if not alvo.destrutivo_ok:
        raise RuntimeError(
            f"Operacao destrutiva bloqueada no backend '{alvo.backend}': "
            f"nenhum alvo de teste aprovado. {alvo.motivo_skip}"
        )
    if alvo.backend == "qdrant" and not _tem_marcador(alvo.prefixo):
        raise RuntimeError(
            f"Operacao destrutiva bloqueada no backend 'qdrant': o prefixo "
            f"'{alvo.prefixo}' nao tem marcador de teste."
        )
    if alvo.backend == "pgvector" and not is_test_database(alvo.alvo):
        raise RuntimeError(
            f"Operacao destrutiva bloqueada no backend 'pgvector': "
            f"'{_database_name(alvo.alvo)}' nao parece um banco de testes."
        )


@pytest.fixture
def sample_rule() -> MemoryEntry:
    return MemoryEntry(
        content="Never use rm -rf in production",
        collection="rule",
        tags={"skill": ["deploy"], "domain": ["infra"]},
        priority="critical",
        mandatory=True,
        scope="global",
        source="human",
    )


@pytest.fixture
def sample_instruction() -> MemoryEntry:
    return MemoryEntry(
        content="Use the CI pipeline for all deployments",
        collection="instruction",
        tags={"skill": ["deploy", "ci-cd"], "situation": ["deploy"]},
        priority="high",
        mandatory=False,
    )


@pytest.fixture
def sample_fact() -> MemoryEntry:
    return MemoryEntry(
        content="Production database is PostgreSQL 16",
        collection="fact",
        tags={"domain": ["database", "infra"]},
        priority="medium",
    )


@pytest.fixture
def sample_learning() -> MemoryEntry:
    return MemoryEntry(
        content="Auth tests failed with mocks; use real DB for auth integration tests",
        collection="learning",
        tags={"skill": ["testing", "auth"], "domain": ["security"]},
        priority="high",
    )


@pytest.fixture
def sample_entries(
    sample_rule: MemoryEntry,
    sample_instruction: MemoryEntry,
    sample_fact: MemoryEntry,
    sample_learning: MemoryEntry,
) -> list[MemoryEntry]:
    return [sample_rule, sample_instruction, sample_fact, sample_learning]


@pytest.fixture
def all_16_collections() -> list[MemoryEntry]:
    """One entry per collection for coverage.

    NOME HISTORICO: a ontologia tem 20 colecoes desde a expansao do F1
    (`VALID_COLLECTIONS` em `src/orkmind/core/models.py`), mas esta fixture
    cobre as 16 originais e mantem o nome antigo. Renomear tocaria varios
    testes sem ganho funcional, entao a divergencia fica registrada aqui e
    no log do ciclo F3, nao escondida. As 4 colecoes novas (`session`,
    `artifact`, `compliance`, `semantic_log`) exigem metadata proprio e sao
    exercitadas por testes especificos, nao por esta cobertura generica.
    """
    collections = [
        ("rule", "Do not push to main directly", True),
        ("instruction", "Run tests before merging", False),
        ("fact", "API runs on port 8080", False),
        ("learning", "Mock DB caused test failures", False),
        ("preference", "Use atomic commits", False),
        ("decision", "Chose React for frontend", False),
        ("content", "API v2 spec document", False),
        ("agenda", "Weekly standup Monday 2pm", False),
        ("contacts", "Alice - DevOps lead", False),
        ("handoff", "Module X handoff to agent Y", False),
        ("roadmap", "MVP phase 2: DAG engine", False),
        ("files", "src/orkmind/core/models.py", False),
        ("docs", "docs/ontologia.md defines schema", False),
        ("dags", "deploy DAG: ci -> test -> deploy", False),
        ("tools", "orkmind CLI: orkmind add", False),
        ("users", "tomas - admin of orkmind workspace", False),
    ]
    return [
        MemoryEntry(
            content=content,
            collection=coll,  # type: ignore[arg-type]
            tags={"skill": ["general"]},
            mandatory=mand,
        )
        for coll, content, mand in collections
    ]
