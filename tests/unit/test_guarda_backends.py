"""Guarda destrutiva generalizada por backend (F3.3, regra R0.1).

A guarda original (`tests/unit/test_guarda_banco_testes.py`) continua
valendo sem alteracao: ela e a regressao do incidente de 29/08/2026, em
que uma execucao de `pytest tests/` apontada para a base de producao
destruiu 18 entries reais.

Este arquivo cobre as cinco regras nao negociaveis de 7.3.1 do plano,
que valem para os backends que nasceram neste ciclo.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from tests.conftest import (
    QDRANT_PROD_URL_ENV,
    QDRANT_TEST_PREFIX_ENV,
    QDRANT_TEST_URL_ENV,
    SENTINELA_MEMORY,
    TEST_DB_ENV,
    AlvoDeTeste,
    assert_destrutivo_permitido,
    resolve_alvo_de_teste,
)

URL_QDRANT_TESTE = "http://localhost:7333"
PREFIXO_OK = "orkmind_test_"


class TestRegra1SemFallbackDeProducao:
    """Backend novo nao herda fallback de variavel de producao."""

    def test_qdrant_sem_variaveis_dedicadas_e_recusado(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            alvo = resolve_alvo_de_teste("qdrant")
        assert alvo.destrutivo_ok is False
        assert alvo.alvo == ""

    def test_url_de_producao_do_qdrant_nunca_vira_alvo(self) -> None:
        env = {QDRANT_PROD_URL_ENV: "http://producao:6333"}
        with patch.dict(os.environ, env, clear=True):
            alvo = resolve_alvo_de_teste("qdrant")
        assert alvo.destrutivo_ok is False
        assert "producao" not in alvo.alvo

    def test_url_de_teste_sem_prefixo_nao_basta(self) -> None:
        env = {QDRANT_TEST_URL_ENV: URL_QDRANT_TESTE}
        with patch.dict(os.environ, env, clear=True):
            alvo = resolve_alvo_de_teste("qdrant")
        assert alvo.destrutivo_ok is False

    def test_prefixo_sem_url_nao_basta(self) -> None:
        env = {QDRANT_TEST_PREFIX_ENV: PREFIXO_OK}
        with patch.dict(os.environ, env, clear=True):
            alvo = resolve_alvo_de_teste("qdrant")
        assert alvo.destrutivo_ok is False

    def test_motivo_de_skip_e_ruidoso_e_nomeia_as_variaveis(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            alvo = resolve_alvo_de_teste("qdrant")
        assert QDRANT_TEST_URL_ENV in alvo.motivo_skip
        assert QDRANT_TEST_PREFIX_ENV in alvo.motivo_skip
        assert QDRANT_PROD_URL_ENV in alvo.motivo_skip


class TestRegra2PrefixoComMarcador:
    def test_prefixo_sem_marcador_e_recusado_com_erro(self) -> None:
        env = {
            QDRANT_TEST_URL_ENV: URL_QDRANT_TESTE,
            QDRANT_TEST_PREFIX_ENV: "orkmind_",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="marcador"):
                resolve_alvo_de_teste("qdrant")

    @pytest.mark.parametrize("prefixo", ["orkmind_test_", "ork_ci_", "sandbox_ork_"])
    def test_prefixos_com_marcador_sao_aceitos(self, prefixo: str) -> None:
        env = {
            QDRANT_TEST_URL_ENV: URL_QDRANT_TESTE,
            QDRANT_TEST_PREFIX_ENV: prefixo,
        }
        with patch.dict(os.environ, env, clear=True):
            alvo = resolve_alvo_de_teste("qdrant")
        assert alvo.destrutivo_ok is True
        assert alvo.prefixo == prefixo

    def test_prefixo_errado_falha_alto_em_vez_de_pular(self) -> None:
        """Variavel explicita e errada e erro, nao skip silencioso."""
        env = {
            QDRANT_TEST_URL_ENV: URL_QDRANT_TESTE,
            QDRANT_TEST_PREFIX_ENV: "producao_",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError):
                resolve_alvo_de_teste("qdrant")


class TestRegra4CoincidenciaComProducao:
    def test_mesma_url_de_producao_levanta_em_vez_de_pular(self) -> None:
        env = {
            QDRANT_TEST_URL_ENV: URL_QDRANT_TESTE,
            QDRANT_TEST_PREFIX_ENV: PREFIXO_OK,
            QDRANT_PROD_URL_ENV: URL_QDRANT_TESTE,
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="Coincidencia suspeita"):
                resolve_alvo_de_teste("qdrant")

    def test_coincidencia_ignora_barra_final(self) -> None:
        env = {
            QDRANT_TEST_URL_ENV: URL_QDRANT_TESTE + "/",
            QDRANT_TEST_PREFIX_ENV: PREFIXO_OK,
            QDRANT_PROD_URL_ENV: URL_QDRANT_TESTE,
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError):
                resolve_alvo_de_teste("qdrant")

    def test_instancias_distintas_sao_aceitas(self) -> None:
        env = {
            QDRANT_TEST_URL_ENV: URL_QDRANT_TESTE,
            QDRANT_TEST_PREFIX_ENV: PREFIXO_OK,
            QDRANT_PROD_URL_ENV: "http://producao:6333",
        }
        with patch.dict(os.environ, env, clear=True):
            alvo = resolve_alvo_de_teste("qdrant")
        assert alvo.destrutivo_ok is True


class TestAssertDestrutivoPermitido:
    def test_levanta_quando_nao_ha_alvo_aprovado(self) -> None:
        alvo = AlvoDeTeste(
            backend="qdrant", alvo="", destrutivo_ok=False, motivo_skip="sem alvo"
        )
        with pytest.raises(RuntimeError, match="bloqueada"):
            assert_destrutivo_permitido(alvo)

    def test_nao_faz_skip_apenas_levanta(self) -> None:
        """Skip e decisao da fixture, ANTES; aqui so pode explodir."""
        alvo = AlvoDeTeste(
            backend="pgvector", alvo="", destrutivo_ok=False, motivo_skip="x"
        )
        with pytest.raises(RuntimeError):
            assert_destrutivo_permitido(alvo)

    def test_recheca_prefixo_do_qdrant_mesmo_com_flag_ligada(self) -> None:
        """Cinto e suspensorio: a flag sozinha nao autoriza."""
        alvo = AlvoDeTeste(
            backend="qdrant",
            alvo=URL_QDRANT_TESTE,
            destrutivo_ok=True,
            motivo_skip="",
            prefixo="producao_",
        )
        with pytest.raises(RuntimeError, match="prefixo"):
            assert_destrutivo_permitido(alvo)

    def test_recheca_nome_do_banco_do_pgvector(self) -> None:
        alvo = AlvoDeTeste(
            backend="pgvector",
            alvo="postgresql://user:pw@localhost/orkmind",
            destrutivo_ok=True,
            motivo_skip="",
        )
        with pytest.raises(RuntimeError, match="banco de testes"):
            assert_destrutivo_permitido(alvo)

    def test_alvo_aprovado_passa(self) -> None:
        alvo = AlvoDeTeste(
            backend="pgvector",
            alvo="postgresql://user:pw@localhost/orkmind_test",
            destrutivo_ok=True,
            motivo_skip="",
        )
        assert_destrutivo_permitido(alvo) is None


class TestBackendMemory:
    def test_memory_e_sempre_seguro(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            alvo = resolve_alvo_de_teste("memory")
        assert alvo.destrutivo_ok is True
        assert alvo.alvo == SENTINELA_MEMORY
        assert_destrutivo_permitido(alvo)

    def test_memory_nao_depende_de_variavel_nenhuma(self) -> None:
        env = {"ORKMIND_DATABASE_URL": "postgresql://localhost/orkmind"}
        with patch.dict(os.environ, env, clear=True):
            alvo = resolve_alvo_de_teste("memory")
        assert alvo.alvo == SENTINELA_MEMORY


class TestBackendPgvectorPreservado:
    """A guarda historica continua valendo byte a byte."""

    def test_banco_de_producao_nao_vira_alvo(self) -> None:
        env = {"ORKMIND_DATABASE_URL": "postgresql://localhost/orkmind"}
        with patch.dict(os.environ, env, clear=True):
            alvo = resolve_alvo_de_teste("pgvector")
        assert alvo.destrutivo_ok is False

    def test_banco_com_marcador_e_aceito(self) -> None:
        env = {TEST_DB_ENV: "postgresql://localhost/orkmind_test"}
        with patch.dict(os.environ, env, clear=True):
            alvo = resolve_alvo_de_teste("pgvector")
        assert alvo.destrutivo_ok is True
        assert_destrutivo_permitido(alvo)

    def test_variavel_de_teste_apontando_para_producao_levanta(self) -> None:
        env = {TEST_DB_ENV: "postgresql://localhost/orkmind"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="nao parece um banco de testes"):
                resolve_alvo_de_teste("pgvector")


class TestBackendDesconhecido:
    def test_backend_sem_resolvedor_falha_alto(self) -> None:
        with pytest.raises(RuntimeError, match="desconhecido"):
            resolve_alvo_de_teste("cassandra")


class TestRegistroDaSuiteDeContrato:
    def test_memory_esta_sempre_disponivel(self) -> None:
        from tests.contract.backends import backends_disponiveis

        with patch.dict(os.environ, {}, clear=True):
            assert "memory" in backends_disponiveis()

    def test_limpeza_do_qdrant_exige_prefixo(self) -> None:
        """Regra 3: nunca 'apagar tudo'."""
        from tests.contract.backends import _limpar_qdrant

        alvo = AlvoDeTeste(
            backend="qdrant",
            alvo=URL_QDRANT_TESTE,
            destrutivo_ok=True,
            motivo_skip="",
            prefixo="",
        )
        with pytest.raises(RuntimeError):
            _limpar_qdrant(alvo, object())  # type: ignore[arg-type]

    def test_limpeza_do_pgvector_bloqueia_alvo_nao_aprovado(self) -> None:
        from tests.contract.backends import _limpar_pgvector

        alvo = AlvoDeTeste(
            backend="pgvector",
            alvo="postgresql://localhost/orkmind",
            destrutivo_ok=False,
            motivo_skip="sem alvo",
        )
        with pytest.raises(RuntimeError, match="bloqueada"):
            _limpar_pgvector(alvo, object())  # type: ignore[arg-type]
