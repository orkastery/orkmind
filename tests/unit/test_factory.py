"""Tests for store factory (registry de backends, F3.2)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from orkmind.core.config import OrkMindConfig
from orkmind.store.errors import BackendDesconhecidoError, BackendMalConfiguradoError
from orkmind.store.factory import STORE_BACKENDS, create_store
from orkmind.store.postgres_adapter import PostgresAdapter


def _adapter(store: object) -> object:
    """O adapter cru por tras de um store, seja ele governado ou nao.

    Ate F3.3 `create_store` devolve o adapter direto; de F3.4 em diante
    devolve um GovernedStore que expoe o adapter em `.inner`.
    """
    return getattr(store, "inner", store)


class TestCreateStore:
    def test_default_e_pgvector(self) -> None:
        config = OrkMindConfig(database_url="postgresql://localhost/test")
        store = create_store(config)
        assert config.store_backend == "pgvector"
        assert isinstance(_adapter(store), PostgresAdapter)

    def test_alias_postgres_aponta_para_pgvector(self) -> None:
        config = OrkMindConfig(
            database_url="postgresql://localhost/test", store_backend="postgres"
        )
        store = create_store(config)
        assert isinstance(_adapter(store), PostgresAdapter)

    def test_nome_do_backend_e_normalizado(self) -> None:
        config = OrkMindConfig(
            database_url="postgresql://localhost/test", store_backend="  PGVector "
        )
        store = create_store(config)
        assert isinstance(_adapter(store), PostgresAdapter)

    def test_pgvector_sem_url_falha_citando_o_backend(self) -> None:
        config = OrkMindConfig(database_url="")
        with pytest.raises(BackendMalConfiguradoError, match="pgvector"):
            create_store(config)

    def test_erro_de_configuracao_continua_sendo_value_error(self) -> None:
        """DD-7: quem captura por tipo nao quebra."""
        config = OrkMindConfig(database_url="")
        with pytest.raises(ValueError):
            create_store(config)

    def test_backend_desconhecido_lista_os_disponiveis(self) -> None:
        config = OrkMindConfig(store_backend="cassandra")
        with pytest.raises(BackendDesconhecidoError) as exc:
            create_store(config)
        msg = str(exc.value)
        assert "cassandra" in msg
        for nome in ("pgvector", "memory", "qdrant", "postgres"):
            assert nome in msg
        assert "ORKMIND_STORE_BACKEND" in msg

    def test_backend_desconhecido_tambem_e_value_error(self) -> None:
        with pytest.raises(ValueError):
            create_store(OrkMindConfig(store_backend="cassandra"))

    def test_sem_fallback_silencioso_para_pgvector(self) -> None:
        """Escolher errado tem que doer na hora, nunca virar pgvector."""
        config = OrkMindConfig(
            database_url="postgresql://localhost/test", store_backend="qdrand"
        )
        with pytest.raises(BackendDesconhecidoError):
            create_store(config)

    def test_registry_tem_exatamente_os_backends_da_dp1(self) -> None:
        assert set(STORE_BACKENDS) == {"pgvector", "postgres", "memory", "qdrant"}


class TestImportLazy:
    """Importar o factory nao pode arrastar driver de banco nenhum."""

    def test_importar_factory_nao_importa_psycopg(self) -> None:
        codigo = (
            "import sys; import orkmind.store.factory; "
            "print('psycopg' in sys.modules, 'pgvector' in sys.modules)"
        )
        saida = subprocess.run(
            [sys.executable, "-c", codigo], capture_output=True, text=True, check=True
        )
        assert saida.stdout.strip() == "False False"

    def test_importar_factory_nao_importa_qdrant_client(self) -> None:
        codigo = (
            "import sys; import orkmind.store.factory; "
            "print('qdrant_client' in sys.modules)"
        )
        saida = subprocess.run(
            [sys.executable, "-c", codigo], capture_output=True, text=True, check=True
        )
        assert saida.stdout.strip() == "False"


class TestBackendQdrantSemCliente:
    def test_falha_com_instrucao_de_instalacao(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sem qdrant-client, o erro diz exatamente o que instalar."""
        import builtins

        real_import = builtins.__import__

        def fake_import(nome: str, *args: object, **kwargs: object) -> object:
            if nome.startswith("qdrant_client") or nome.endswith("qdrant_adapter"):
                raise ImportError("No module named 'qdrant_client'")
            return real_import(nome, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(BackendMalConfiguradoError, match=r"orkmind\[qdrant\]"):
            create_store(OrkMindConfig(store_backend="qdrant"))
