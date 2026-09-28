"""Configuration management for OrkMind."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]


CONFIG_DIR = Path.home() / ".orkmind"
CONFIG_FILE = CONFIG_DIR / "config.toml"

_ENV_PREFIX = "ORKMIND_"


@dataclass
class OrkMindConfig:
    """OrkMind configuration."""

    database_url: str = ""
    # F3.1: backend de storage plugavel. O default preserva o comportamento
    # historico (Postgres + pgvector) para instalacoes que nunca editaram
    # o config.toml (DP-9).
    store_backend: str = "pgvector"
    # Bag livre de opcoes por backend (url do Qdrant, nome de colecao,
    # candidate_overfetch). Cada builder de store/factory.py valida o que
    # o seu backend exige; o dataclass nao conhece backend nenhum.
    store_options: dict[str, Any] = field(default_factory=dict)
    log_level: str = "INFO"
    token_budget: int = 4000
    embedding_dim: int = 1024
    embedding_provider: str = ""
    embedding_model: str = "perplexity/pplx-embed-v1-0.6b"
    embedding_base_url: str = "https://openrouter.ai/api/v1/embeddings"
    embedding_api_key_env: str = "OPENROUTER_API_KEY"
    embedding_timeout_s: float = 8.0
    keyword_detector_enabled: bool = True
    file_path_detector_enabled: bool = True
    layer_strategy: str = "truncate"
    encryption_enabled: bool = False
    encryption_provider: str = "local"
    encryption_collections: list[str] | None = None
    encryption_root_key_path: str = "~/.orkmind/keys/root.key"
    extraction_enabled: bool = False
    extraction_provider: str = "anthropic"
    extraction_model: str = ""
    extraction_api_key_env: str = ""
    extraction_max_entries: int = 10
    search_semantic_enabled: bool = False
    search_rerank_strategy: str = "rrf"
    # G2: sinal de janela agnostico (D-MA11 movido para orkmind.guardrails).
    # Limiares de AVISO e URGENCIA do advisory de rotacao; a execucao da
    # rotacao e sempre do runtime. Chaves na secao [session] do config.toml.
    session_rotate_soon: float = 0.65
    session_rotate_now: float = 0.85
    session_adaptive_mode: bool = False
    session_context_window: int = 200_000

    @property
    def has_database(self) -> bool:
        return bool(self.database_url)

    @property
    def has_store(self) -> bool:
        """True quando ha backend de storage nomeado.

        A validacao do que aquele backend exige (URL, options) e do
        builder correspondente em store/factory.py, nao daqui.
        """
        return bool(self.store_backend)


def _limiar(valor: Any, default: float) -> float:
    """Fracao de limiar valida no intervalo [0.05, 0.99]."""
    try:
        limiar = float(valor)
    except (TypeError, ValueError):
        limiar = default
    return min(max(limiar, 0.05), 0.99)


def _janela(valor: Any, default: int) -> int:
    """Janela de contexto em tokens, sempre positiva."""
    try:
        janela = int(valor)
    except (TypeError, ValueError):
        janela = default
    return max(janela, 1)


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(config_path: Path | None = None) -> OrkMindConfig:
    """Load configuration from file and environment variables.

    Priority: env vars > config file > defaults.
    """
    toml_path = config_path or CONFIG_FILE
    toml_data = _load_toml(toml_path)
    store = toml_data.get("store", {})
    server = toml_data.get("server", {})
    detectors = toml_data.get("detectors", {})
    layers = toml_data.get("layers", {})
    encryption = toml_data.get("encryption", {})
    extraction = toml_data.get("extraction", {})
    embedding = toml_data.get("embedding", {})
    search = toml_data.get("search", {})
    session = toml_data.get("session", {}) or {}

    cfg = OrkMindConfig(
        database_url=store.get("database_url", ""),
        store_backend=store.get("backend", "pgvector"),
        store_options=dict(store.get("options", {}) or {}),
        log_level=server.get("log_level", "INFO"),
        token_budget=server.get("token_budget", 4000),
        embedding_dim=embedding.get("dim", store.get("embedding_dim", 1024)),
        embedding_provider=embedding.get("provider", ""),
        embedding_model=embedding.get("model", "perplexity/pplx-embed-v1-0.6b"),
        embedding_base_url=embedding.get(
            "base_url", "https://openrouter.ai/api/v1/embeddings"
        ),
        embedding_api_key_env=embedding.get("api_key_env", "OPENROUTER_API_KEY"),
        embedding_timeout_s=float(embedding.get("timeout_s", 8.0)),
        keyword_detector_enabled=detectors.get("keyword_enabled", True),
        file_path_detector_enabled=detectors.get("file_path_enabled", True),
        layer_strategy=layers.get("strategy", "truncate"),
        encryption_enabled=encryption.get("enabled", False),
        encryption_provider=encryption.get("provider", "local"),
        encryption_collections=encryption.get("collections"),
        encryption_root_key_path=encryption.get("root_key_path", "~/.orkmind/keys/root.key"),
        extraction_enabled=extraction.get("enabled", False),
        extraction_provider=extraction.get("provider", "anthropic"),
        extraction_model=extraction.get("model", ""),
        extraction_api_key_env=extraction.get("api_key_env", ""),
        extraction_max_entries=extraction.get("max_entries_per_session", 10),
        search_semantic_enabled=search.get("semantic_enabled", False),
        search_rerank_strategy=search.get("rerank_strategy", "rrf"),
        session_rotate_soon=_limiar(session.get("rotation_threshold"), 0.65),
        session_rotate_now=_limiar(session.get("rotate_now_threshold"), 0.85),
        session_adaptive_mode=bool(session.get("adaptive_mode", False)),
        session_context_window=_janela(session.get("context_window"), 200_000),
    )

    # Environment variables override
    if env_url := os.environ.get(f"{_ENV_PREFIX}DATABASE_URL"):
        cfg.database_url = env_url
    if env_log := os.environ.get(f"{_ENV_PREFIX}LOG_LEVEL"):
        cfg.log_level = env_log
    if env_budget := os.environ.get(f"{_ENV_PREFIX}TOKEN_BUDGET"):
        cfg.token_budget = int(env_budget)
    if env_backend := os.environ.get(f"{_ENV_PREFIX}STORE_BACKEND"):
        cfg.store_backend = env_backend
    if env_opts := os.environ.get(f"{_ENV_PREFIX}STORE_OPTIONS"):
        # Falha alta e proposital: config silenciosamente ignorada e a
        # origem classica de "o backend nao era o que eu pensava".
        try:
            extras = json.loads(env_opts)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{_ENV_PREFIX}STORE_OPTIONS nao e um JSON valido: {e}. "
                f"Use algo como '{{\"url\": \"http://localhost:6333\"}}'."
            ) from e
        if not isinstance(extras, dict):
            raise ValueError(
                f"{_ENV_PREFIX}STORE_OPTIONS deve ser um objeto JSON "
                f"(dicionario), nao {type(extras).__name__}."
            )
        cfg.store_options = {**cfg.store_options, **extras}

    return cfg
