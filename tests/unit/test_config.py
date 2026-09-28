"""Tests for configuration loading."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from orkmind.core.config import OrkMindConfig, load_config


class TestOrkMindConfig:
    def test_defaults(self) -> None:
        cfg = OrkMindConfig()
        assert cfg.database_url == ""
        assert cfg.log_level == "INFO"
        assert cfg.token_budget == 4000
        assert cfg.embedding_dim == 1024
        assert cfg.has_database is False

    def test_has_database(self) -> None:
        cfg = OrkMindConfig(database_url="postgresql://localhost/test")
        assert cfg.has_database is True


class TestLoadConfig:
    def test_defaults_no_file(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config(tmp_path / "nonexistent.toml")
        assert cfg.database_url == ""
        assert cfg.token_budget == 4000

    def test_env_override(self, tmp_path: Path) -> None:
        env = {
            "ORKMIND_DATABASE_URL": "postgresql://test/db",
            "ORKMIND_LOG_LEVEL": "DEBUG",
            "ORKMIND_TOKEN_BUDGET": "8000",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(tmp_path / "nonexistent.toml")
        assert cfg.database_url == "postgresql://test/db"
        assert cfg.log_level == "DEBUG"
        assert cfg.token_budget == 8000

    def test_toml_file(self, tmp_path: Path) -> None:
        toml_content = b"""
[store]
database_url = "postgresql://toml/db"
embedding_dim = 768

[server]
log_level = "WARNING"
token_budget = 2000
"""
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(toml_content)

        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config(config_file)
        assert cfg.database_url == "postgresql://toml/db"
        assert cfg.embedding_dim == 768
        assert cfg.log_level == "WARNING"
        assert cfg.token_budget == 2000

    def test_env_overrides_toml(self, tmp_path: Path) -> None:
        toml_content = b"""
[store]
database_url = "postgresql://toml/db"
"""
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(toml_content)

        env = {"ORKMIND_DATABASE_URL": "postgresql://env/db"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(config_file)
        assert cfg.database_url == "postgresql://env/db"


class TestEmbeddingTimeoutConfig:
    """B2: chave [embedding] timeout_s."""

    def test_default_e_8_segundos(self) -> None:
        cfg = load_config(Path("/caminho/que/nao/existe.toml"))
        assert cfg.embedding_timeout_s == 8.0

    def test_le_do_toml(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "config.toml"
        arquivo.write_text(
            '[embedding]\nprovider = "openrouter"\ntimeout_s = 2.5\n'
        )
        cfg = load_config(arquivo)
        assert cfg.embedding_timeout_s == 2.5


class TestStoreBackendConfig:
    """F3.1: backend de storage plugavel e bag de opcoes por backend."""

    def test_default_e_pgvector(self) -> None:
        cfg = OrkMindConfig()
        assert cfg.store_backend == "pgvector"
        assert cfg.store_options == {}

    def test_has_store_com_backend_nomeado(self) -> None:
        assert OrkMindConfig().has_store is True
        assert OrkMindConfig(store_backend="").has_store is False

    def test_has_database_nao_muda(self) -> None:
        """Nao-regressao: has_store nasce ao lado, nao no lugar de has_database."""
        assert OrkMindConfig().has_database is False
        assert OrkMindConfig(database_url="postgresql://localhost/x").has_database is True
        # backend nomeado sem URL nao faz has_database virar True
        assert OrkMindConfig(store_backend="qdrant").has_database is False

    def test_toml_sem_secao_store_resolve_pgvector(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "config.toml"
        arquivo.write_bytes(b'[server]\nlog_level = "DEBUG"\n')
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config(arquivo)
        assert cfg.store_backend == "pgvector"
        assert cfg.store_options == {}

    def test_le_backend_e_options_do_toml(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "config.toml"
        arquivo.write_bytes(
            b'[store]\n'
            b'backend = "qdrant"\n'
            b'database_url = "postgresql://toml/db"\n'
            b'\n'
            b'[store.options]\n'
            b'url = "http://localhost:6333"\n'
            b'collection = "orkmind_memories"\n'
            b'candidate_overfetch = 500\n'
        )
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config(arquivo)
        assert cfg.store_backend == "qdrant"
        assert cfg.store_options["url"] == "http://localhost:6333"
        assert cfg.store_options["collection"] == "orkmind_memories"
        assert cfg.store_options["candidate_overfetch"] == 500
        # database_url continua sendo lido normalmente
        assert cfg.database_url == "postgresql://toml/db"

    def test_env_sobrescreve_backend(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "config.toml"
        arquivo.write_bytes(b'[store]\nbackend = "pgvector"\n')
        with patch.dict(os.environ, {"ORKMIND_STORE_BACKEND": "memory"}, clear=True):
            cfg = load_config(arquivo)
        assert cfg.store_backend == "memory"

    def test_env_options_faz_merge_raso(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "config.toml"
        arquivo.write_bytes(
            b'[store.options]\nurl = "http://toml:6333"\ncollection = "do_toml"\n'
        )
        env = {"ORKMIND_STORE_OPTIONS": '{"url": "http://env:6333", "api_key_env": "Q_KEY"}'}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config(arquivo)
        assert cfg.store_options["url"] == "http://env:6333"
        assert cfg.store_options["collection"] == "do_toml"
        assert cfg.store_options["api_key_env"] == "Q_KEY"

    def test_env_options_invalido_falha_alto_citando_a_variavel(self, tmp_path: Path) -> None:
        env = {"ORKMIND_STORE_OPTIONS": "{isso nao e json}"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="ORKMIND_STORE_OPTIONS"):
                load_config(tmp_path / "inexistente.toml")

    def test_env_options_que_nao_e_objeto_falha_alto(self, tmp_path: Path) -> None:
        env = {"ORKMIND_STORE_OPTIONS": '["lista", "nao", "serve"]'}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="ORKMIND_STORE_OPTIONS"):
                load_config(tmp_path / "inexistente.toml")


class TestSessionConfig:
    """G2: limiares do sinal de janela na secao [session] (0.65/0.85)."""

    def test_defaults_do_roadmap(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config(tmp_path / "inexistente.toml")
        assert cfg.session_rotate_soon == 0.65
        assert cfg.session_rotate_now == 0.85
        assert cfg.session_adaptive_mode is False
        assert cfg.session_context_window == 200_000

    def test_limiares_configuraveis_no_toml(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "config.toml"
        arquivo.write_bytes(
            b"[session]\n"
            b"rotation_threshold = 0.5\n"
            b"rotate_now_threshold = 0.75\n"
            b"adaptive_mode = true\n"
            b"context_window = 128000\n"
        )
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config(arquivo)
        assert cfg.session_rotate_soon == 0.5
        assert cfg.session_rotate_now == 0.75
        assert cfg.session_adaptive_mode is True
        assert cfg.session_context_window == 128_000

    def test_valores_invalidos_caem_no_default_com_clamp(self, tmp_path: Path) -> None:
        arquivo = tmp_path / "config.toml"
        arquivo.write_bytes(
            b'[session]\nrotation_threshold = "muito"\nrotate_now_threshold = 5.0\n'
        )
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config(arquivo)
        assert cfg.session_rotate_soon == 0.65
        assert cfg.session_rotate_now == 0.99
