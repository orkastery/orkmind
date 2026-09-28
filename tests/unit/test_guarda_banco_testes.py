"""Testes da guarda que impede rodar testes destrutivos em producao.

As fixtures de integracao e contrato apagam todas as linhas do banco
apontado. Esta guarda existe porque um `pytest tests/` com
ORKMIND_DATABASE_URL apontando para producao destruiu dados reais
em 2026-08-29.
"""

from __future__ import annotations

import pytest

from tests.conftest import (
    FALLBACK_DB_ENV,
    TEST_DB_ENV,
    is_test_database,
    resolve_test_database_url,
)

PRODUCAO = "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind"
TESTES = "postgresql://orkmind:senha-de-teste@localhost:5432/orkmind_test"


class TestIsTestDatabase:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p@h:5432/orkmind_test",
            "postgresql://u:p@h:5432/test_orkmind",
            "postgresql://u:p@h:5432/orkmind_ci",
            "postgresql://u:p@h:5432/orkmind-sandbox",
        ],
    )
    def test_bancos_de_teste_aceitos(self, url: str) -> None:
        assert is_test_database(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            PRODUCAO,
            "postgresql://u:p@h:5432/orkmind",
            "postgresql://u:p@h:5432/prod",
            "",
        ],
    )
    def test_bancos_de_producao_recusados(self, url: str) -> None:
        assert is_test_database(url) is False


class TestResolveTestDatabaseUrl:
    def test_sem_variaveis_devolve_vazio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TEST_DB_ENV, raising=False)
        monkeypatch.delenv(FALLBACK_DB_ENV, raising=False)
        assert resolve_test_database_url() == ""

    def test_variavel_dedicada_de_teste_e_aceita(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TEST_DB_ENV, TESTES)
        assert resolve_test_database_url() == TESTES

    def test_variavel_dedicada_apontando_para_producao_falha(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TEST_DB_ENV, PRODUCAO)
        with pytest.raises(RuntimeError, match="nao parece um banco de testes"):
            resolve_test_database_url()

    def test_fallback_de_producao_e_ignorado(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regressao do incidente: producao no fallback nunca vira alvo."""
        monkeypatch.delenv(TEST_DB_ENV, raising=False)
        monkeypatch.setenv(FALLBACK_DB_ENV, PRODUCAO)
        assert resolve_test_database_url() == ""

    def test_fallback_de_teste_e_aceito(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TEST_DB_ENV, raising=False)
        monkeypatch.setenv(FALLBACK_DB_ENV, TESTES)
        assert resolve_test_database_url() == TESTES

    def test_variavel_dedicada_tem_precedencia(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TEST_DB_ENV, TESTES)
        monkeypatch.setenv(FALLBACK_DB_ENV, PRODUCAO)
        assert resolve_test_database_url() == TESTES
