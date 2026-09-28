"""Configuracao do Memory Provider nativo.

Tudo que varia por ambiente mora aqui e pode vir de variaveis
`ORKMIND_PROVIDER_*`. O DSN nunca tem default com senha: ou vem explicito, ou vem
de `ORKMIND_PROVIDER_DATABASE_URL`.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ENV_PREFIX = "ORKMIND_PROVIDER_"
DATABASE_URL_ENV = f"{ENV_PREFIX}DATABASE_URL"

# Limite do indice HNSW por tipo no pgvector, medido em 0.8.6: `vector` para
# em 2000 dimensoes e `halfvec` em 4000. Modelo de 4096 (Qwen3-Embedding-8B,
# p.ex.) NAO e indexavel em nenhum dos dois no tamanho nativo - por isso o
# provider pede ao endpoint um vetor menor (ver `embedding_request_dimensions`).
HNSW_MAX_DIM = 2000
HALFVEC_MAX_DIM = 4000

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


class ChunkingOptions(BaseModel):
    """Parametros do chunking semantico estruturado."""

    model_config = ConfigDict(frozen=True)

    # Alvo de 200-400 tokens por chunk recuperado.
    max_tokens: int = Field(default=350, ge=64, le=2000)
    # Abaixo disso o chunk e fundido com o vizinho.
    min_tokens: int = Field(default=48, ge=0, le=512)
    overlap_ratio: float = Field(default=0.12, ge=0.10, le=0.15)
    # Titulos ate este nivel (`##`/`###`) sao fronteira dura de chunk.
    split_heading_level: int = Field(default=3, ge=1, le=6)


class MemoryProviderSettings(BaseModel):
    """Configuracao completa do provider."""

    model_config = ConfigDict(frozen=True)

    database_url: str = ""
    db_schema: str = "public"
    # Schema onde a extensao `vector` esta instalada (o codec do asyncpg
    # precisa saber). None = descobrir em pg_extension.
    vector_schema: str | None = None
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=10, ge=1)
    command_timeout_s: float = Field(default=30.0, gt=0)
    auto_migrate: bool = True

    default_agent_id: str = "default"
    fts_config: str = "portuguese"

    # Sem `le=` aqui: o teto e conferido no validator, que explica de onde vem.
    embedding_dim: int = Field(default=1536, ge=8)
    embedding_model: str = "qwen/qwen3-embedding-8b"
    embedding_base_url: str = "https://openrouter.ai/api/v1/embeddings"
    embedding_api_key_env: str = "OPENROUTER_API_KEY"
    embedding_batch_size: int = Field(default=64, ge=1, le=512)
    embedding_concurrency: int = Field(default=4, ge=1, le=32)
    embedding_max_attempts: int = Field(default=3, ge=1, le=8)
    # Pede o vetor ja em `embedding_dim` ao endpoint. Ligado por padrao porque
    # o modelo default e nativamente 4096 e o HNSW nao indexa isso.
    embedding_request_dimensions: bool = True
    # Curto de proposito: na consulta o embedding esta no meio do turno do
    # agente; passar disso degrada para busca lexica em vez de travar.
    query_embedding_timeout_s: float = Field(default=8.0, gt=0)

    chunking: ChunkingOptions = Field(default_factory=ChunkingOptions)

    # Recall: janela deslizante que vai para o orquestrador.
    recall_window_turns: int = Field(default=6, ge=1, le=64)
    # Mensagem maior que isso entra PAGINADA na janela (o banco guarda tudo).
    recall_max_message_tokens: int = Field(default=1200, ge=64)

    # Recuperacao hibrida.
    rrf_k: int = Field(default=60, ge=1)
    candidate_multiplier: int = Field(default=10, ge=1, le=100)
    min_candidates: int = Field(default=50, ge=1, le=1000)
    lexical_mode: Literal["any", "all"] = "any"
    # Teto de linhas que a perna lexica aceita ranquear. So lexemas raros o
    # bastante para caber aqui geram candidatos (ver services/retrieval.py).
    sparse_candidate_cap: int = Field(default=2000, ge=1, le=1_000_000)
    # None = sem corte. Depende do modelo de embedding, entao nao ha default.
    dense_max_distance: float | None = Field(default=None, gt=0, le=2)

    ingest_queue_size: int = Field(default=256, ge=1)
    ingest_workers: int = Field(default=2, ge=1, le=16)

    @field_validator("embedding_dim")
    @classmethod
    def _indexable_dim(cls, value: int) -> int:
        # A mensagem do Pydantic sozinha ("menor ou igual a 2000") nao diz de
        # onde vem o numero nem o que fazer a respeito.
        if value > HNSW_MAX_DIM:
            raise ValueError(
                f"{value} dimensoes: o indice HNSW do pgvector aceita no maximo "
                f"{HNSW_MAX_DIM} no tipo `vector`. Use um modelo Matryoshka com "
                f"`embedding_request_dimensions=True` para receber o vetor menor."
            )
        return value

    @field_validator("db_schema", "fts_config")
    @classmethod
    def _identifier(cls, value: str) -> str:
        # Os dois valores entram em DDL por substituicao de texto; so passa
        # identificador simples.
        if not _IDENTIFIER.match(value):
            raise ValueError(f"identificador invalido: {value!r}")
        return value

    @field_validator("vector_schema")
    @classmethod
    def _optional_identifier(cls, value: str | None) -> str | None:
        if value is not None and not _IDENTIFIER.match(value):
            raise ValueError(f"identificador invalido: {value!r}")
        return value

    @classmethod
    def from_env(cls, **overrides: Any) -> MemoryProviderSettings:
        """Le `ORKMIND_PROVIDER_<CAMPO>` para os campos escalares; overrides vencem."""
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            if name == "chunking":
                continue
            raw = os.environ.get(f"{ENV_PREFIX}{name.upper()}")
            if raw:
                values[name] = raw
        chunking: dict[str, Any] = {}
        for name in ChunkingOptions.model_fields:
            raw = os.environ.get(f"{ENV_PREFIX}CHUNK_{name.upper()}")
            if raw:
                chunking[name] = raw
        if chunking:
            values["chunking"] = ChunkingOptions(**chunking)
        values.update(overrides)
        return cls(**values)

    def require_database_url(self) -> str:
        if not self.database_url:
            raise ValueError(
                f"DSN ausente: passe `database_url` ou defina {DATABASE_URL_ENV} "
                f"(ex.: postgresql://usuario:senha@localhost:5433/memory_provider)."
            )
        return self.database_url
