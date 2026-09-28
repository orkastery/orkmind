"""Modelos Pydantic v2 do Memory Provider nativo.

Tres ideias atravessam este arquivo:

1. Identidade vem do CANAL, nunca do agente. `UserContext` e montado pela
   integracao (Teams, Slack, sua API) a partir do usuario autenticado. O LLM
   nao escolhe papel.
2. Fail-closed. `MemoryScopeFilter` sem papel informado e OPERATIONAL, e um
   pedido que tenta ler acima do proprio papel e ERRO de validacao, nao um
   filtro silenciosamente reduzido: tentativa de escalada tem que aparecer.
3. O filtro e deterministico. `effective_*` devolvem exatamente os valores
   que entram no WHERE, antes de qualquer busca vetorial.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Vocabulario
# ---------------------------------------------------------------------------


class AccessLevel(str, Enum):
    """Classificacao de um documento e, do outro lado, papel de quem le."""

    OPERATIONAL = "OPERATIONAL"
    EXECUTIVE = "EXECUTIVE"
    SYSTEM_ADMIN = "SYSTEM_ADMIN"

    @property
    def rank(self) -> int:
        return _ACCESS_RANK[self]


_ACCESS_RANK: dict[AccessLevel, int] = {
    AccessLevel.OPERATIONAL: 0,
    AccessLevel.EXECUTIVE: 1,
    AccessLevel.SYSTEM_ADMIN: 2,
}


def readable_levels(role: AccessLevel) -> tuple[AccessLevel, ...]:
    """Niveis que um papel pode ler: o proprio e os abaixo (hierarquia linear)."""
    return tuple(level for level in AccessLevel if level.rank <= role.rank)


class CoreBlockLabel(str, Enum):
    PERSONA = "persona"
    SYSTEM_INVARIANTS = "system_invariants"
    USER_PROFILE = "user_profile"


# Ordem de renderizacao no prompt e blocos sem os quais o agente nao roda.
CORE_BLOCK_ORDER: tuple[CoreBlockLabel, ...] = (
    CoreBlockLabel.PERSONA,
    CoreBlockLabel.SYSTEM_INVARIANTS,
    CoreBlockLabel.USER_PROFILE,
)
REQUIRED_CORE_BLOCKS: tuple[CoreBlockLabel, ...] = (
    CoreBlockLabel.PERSONA,
    CoreBlockLabel.SYSTEM_INVARIANTS,
)


class ChatRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


class DocType:
    """Tipologias conhecidas. O conjunto e ABERTO: qualquer `snake_case` vale."""

    DEEP_RESEARCH = "deep_research"
    PLAYBOOK = "playbook"
    GUIDELINE = "guideline"
    POLICY = "policy"
    OPEX_ANALYSIS = "opex_analysis"
    ARTICLE = "article"
    MANUAL = "manual"
    MEETING_MINUTES = "meeting_minutes"
    NOTE = "note"


# Os sufixos `_text` dizem que o conteudo foi EXTRAIDO daquele formato: o
# binario original fica onde estava, apontado por `source_url_or_path`.
ContentFormat = Literal["markdown", "text", "html", "pdf_text", "docx_text"]

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DOC_TYPE = re.compile(r"^[a-z][a-z0-9_]*$")
_INDEX_KEY = re.compile(r"^[A-Z0-9][A-Z0-9_]*$")

# Chaves de `metadata_tags` que o pipeline preenche; o chamador nao sobrescreve.
RESERVED_TAG_KEYS = frozenset(
    {
        "slug",
        "doc_type",
        "heading_path",
        "section_level",
        "chunk_index",
        "total_chunks",
        "has_code",
        "has_table",
        "has_list",
    }
)


def normalize_department(value: str) -> str:
    return value.strip().upper()


def _normalize_departments(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        normalized = normalize_department(value)
        if normalized:
            seen.setdefault(normalized, None)
    return list(seen)


def validate_index_path(path: str) -> str:
    """`DOMINIO` ou `DOMINIO/NO_TEMATICO`, sempre em maiusculas."""
    parts = path.strip("/").split("/")
    if not 1 <= len(parts) <= 2 or not all(_INDEX_KEY.match(part) for part in parts):
        raise ValueError(
            f"caminho de meta-indice invalido: {path!r} "
            f"(esperado DOMINIO ou DOMINIO/NO_TEMATICO, A-Z 0-9 _)"
        )
    return "/".join(parts)


def normalize_content(raw: str) -> str:
    """Forma canonica usada SO para o hash; `raw_content` e gravado intacto."""
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip()


def content_sha256(raw: str) -> str:
    return hashlib.sha256(normalize_content(raw).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Identidade e escopo (RBAC)
# ---------------------------------------------------------------------------


class UserContext(BaseModel):
    """Quem esta falando com o agente, segundo o canal autenticado."""

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(min_length=1)
    role: AccessLevel = AccessLevel.OPERATIONAL
    departments: list[str] = Field(default_factory=list)
    display_name: str | None = None
    channel: str = "api"

    @field_validator("departments")
    @classmethod
    def _departments(cls, values: list[str]) -> list[str]:
        return _normalize_departments(values)


class MemoryScopeFilter(BaseModel):
    """Filtro de escopo aplicado ANTES da busca (pre-filtering).

    `user_role` e `departments` dizem quem le; o resto so ESTREITA o que essa
    pessoa ja poderia ver. Prefira `MemoryScopeFilter.for_user(...)`, que
    amarra o filtro a identidade do canal via `ExecutiveScopeGuard`.
    """

    model_config = ConfigDict(frozen=True)

    user_role: AccessLevel = AccessLevel.OPERATIONAL
    departments: list[str] = Field(default_factory=list)
    # Ver todos os departamentos (consolidacoes cross-department). So EXECUTIVE+.
    cross_department: bool = False
    # Estreitamento opcional; pedir nivel acima do papel e erro.
    access_levels: list[AccessLevel] | None = None
    doc_types: list[str] | None = None
    # Contencao JSONB sobre wiki_chunks.metadata_tags (usa o indice GIN).
    tags: dict[str, Any] | None = None
    # Restringe a busca a uma subarvore do meta-indice.
    index_path: str | None = None
    document_ids: list[UUID] | None = None

    @field_validator("departments")
    @classmethod
    def _departments(cls, values: list[str]) -> list[str]:
        return _normalize_departments(values)

    @field_validator("doc_types")
    @classmethod
    def _doc_types(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        for value in values:
            if not _DOC_TYPE.match(value):
                raise ValueError(f"doc_type invalido: {value!r}")
        return values

    @field_validator("index_path")
    @classmethod
    def _index_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_index_path(value)

    @model_validator(mode="after")
    def _no_escalation(self) -> MemoryScopeFilter:
        allowed = readable_levels(self.user_role)
        if self.access_levels is not None:
            if not self.access_levels:
                raise ValueError("access_levels vazio: omita o campo para usar o padrao do papel")
            above = [level.value for level in self.access_levels if level not in allowed]
            if above:
                raise ValueError(
                    f"papel {self.user_role.value} nao pode ler nivel(is) {', '.join(above)}"
                )
        if self.cross_department and self.user_role.rank < AccessLevel.EXECUTIVE.rank:
            raise ValueError("cross_department exige papel EXECUTIVE ou superior")
        return self

    @property
    def effective_access_levels(self) -> list[str]:
        """Valores exatos do `access_level = ANY(...)` do WHERE."""
        levels = self.access_levels or list(readable_levels(self.user_role))
        return [level.value for level in levels]

    @classmethod
    def for_user(cls, user: UserContext, **narrowing: Any) -> MemoryScopeFilter:
        """Filtro amarrado a identidade do canal, ja validado pelo guard.

        `narrowing` aceita os campos de estreitamento (`doc_types`, `tags`,
        `index_path`, `departments`...). O papel vem SEMPRE de `user`.
        """
        if "user_role" in narrowing:
            raise TypeError("user_role vem de `user`; nao e parametro de for_user")
        departments = narrowing.pop("departments", None)
        scope = cls(
            user_role=user.role,
            departments=user.departments if departments is None else departments,
            **narrowing,
        )
        return ExecutiveScopeGuard(user=user, scope=scope).scope


class ExecutiveScopeGuard(BaseModel):
    """Confere um `MemoryScopeFilter` contra a identidade vinda do canal.

    O filtro sozinho so garante coerencia interna (nao pede acima do papel
    que ELE declara). O guard fecha a outra metade: o papel e os departamentos
    declarados no filtro nao podem exceder os do usuario autenticado. Um
    operacional que chegue pelo canal com filtro EXECUTIVE para aqui, com
    `ValidationError`, antes de qualquer SQL.
    """

    model_config = ConfigDict(frozen=True)

    user: UserContext
    scope: MemoryScopeFilter

    @model_validator(mode="after")
    def _bind_to_identity(self) -> ExecutiveScopeGuard:
        if self.scope.user_role.rank > self.user.role.rank:
            raise ValueError(
                f"escopo declara papel {self.scope.user_role.value}, mas o usuario "
                f"{self.user.user_id!r} e {self.user.role.value}"
            )
        if self.user.role.rank < AccessLevel.EXECUTIVE.rank:
            foreign = sorted(set(self.scope.departments) - set(self.user.departments))
            if foreign:
                raise ValueError(
                    f"usuario {self.user.user_id!r} nao pertence a: {', '.join(foreign)}"
                )
        return self


# ---------------------------------------------------------------------------
# Tier 1 - Core Memory
# ---------------------------------------------------------------------------


class CoreMemoryBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    agent_id: str
    label: CoreBlockLabel
    scope_key: str = ""
    content: str
    content_hash: str
    version: int
    created_by: str
    created_at: datetime


class SystemContext(BaseModel):
    """Core Memory de um turno: blocos pinados + identidade do canal."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    user: UserContext
    blocks: list[CoreMemoryBlock]
    token_estimate: int

    def block(self, label: CoreBlockLabel) -> CoreMemoryBlock | None:
        return next((b for b in self.blocks if b.label == label), None)

    def render(self) -> str:
        """Texto pronto para o system prompt. Deterministico para um mesmo estado."""
        parts = ["<core_memory>"]
        for block in self.blocks:
            parts.append(f"<{block.label.value}>\n{block.content.strip()}\n</{block.label.value}>")
        identity = [
            f"user_id: {self.user.user_id}",
            f"role: {self.user.role.value}",
            f"departments: {', '.join(self.user.departments) or '-'}",
            f"channel: {self.user.channel}",
        ]
        if self.user.display_name:
            identity.insert(1, f"display_name: {self.user.display_name}")
        parts.append("<session_identity>\n" + "\n".join(identity) + "\n</session_identity>")
        parts.append("</core_memory>")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tier 2 - Recall Memory
# ---------------------------------------------------------------------------


class HistoryMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    seq: int
    session_id: str
    agent_id: str
    user_id: str | None = None
    turn_index: int
    role: ChatRole
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    token_estimate: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    # True quando `content` e uma PAGINA do original (janela ou leitura
    # paginada). O original continua integro no banco.
    truncated: bool = False
    next_offset: int | None = None


class HistoryPage(BaseModel):
    """Leitura paginada de uma mensagem grande (ex.: tool_result extenso)."""

    model_config = ConfigDict(frozen=True)

    message_id: UUID
    content: str
    offset: int
    next_offset: int | None
    total_chars: int
    total_tokens: int


# ---------------------------------------------------------------------------
# Tier 3 - Wiki Memory
# ---------------------------------------------------------------------------


class ChunkMetadata(BaseModel):
    """Conteudo de `wiki_chunks.metadata_tags`.

    As chaves de primeiro nivel sao filtraveis por contencao JSONB
    (`metadata_tags @> '{"tags": ["rede_neutra"]}'`). Chaves extras do
    chamador entram no mesmo nivel.
    """

    model_config = ConfigDict(extra="allow")

    slug: str
    doc_type: str
    heading_path: list[str] = Field(default_factory=list)
    section_level: int = 0
    chunk_index: int
    total_chunks: int
    has_code: bool = False
    has_table: bool = False
    has_list: bool = False
    tags: list[str] = Field(default_factory=list)


class DocumentIngestRequest(BaseModel):
    """Pedido de ingestao de um documento integro."""

    model_config = ConfigDict(frozen=True)

    slug: str
    title: str = Field(min_length=1)
    doc_type: str
    raw_content: str
    content_format: ContentFormat = "markdown"
    source_url_or_path: str | None = None
    # Obrigatorio, sem default: quem ingere classifica o documento.
    access_level: AccessLevel
    department_scope: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata_tags: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Caminhos do meta-indice onde o documento entra (`DOMINIO/NO_TEMATICO`).
    index_paths: list[str] = Field(default_factory=list)
    # Reprocessa mesmo com hash identico (ex.: troca do modelo de embedding).
    force_update: bool = False
    ingested_by: str = "system"

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not _SLUG.match(value):
            raise ValueError(f"slug invalido: {value!r} (use a-z 0-9 _ -)")
        return value

    @field_validator("doc_type")
    @classmethod
    def _doc_type(cls, value: str) -> str:
        if not _DOC_TYPE.match(value):
            raise ValueError(f"doc_type invalido: {value!r} (use snake_case)")
        return value

    @field_validator("raw_content")
    @classmethod
    def _raw_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("raw_content vazio")
        return value

    @field_validator("department_scope")
    @classmethod
    def _departments(cls, values: list[str]) -> list[str]:
        return _normalize_departments(values)

    @field_validator("tags")
    @classmethod
    def _tags(cls, values: list[str]) -> list[str]:
        return sorted({v.strip().lower() for v in values if v.strip()})

    @field_validator("metadata_tags")
    @classmethod
    def _metadata_tags(cls, value: dict[str, Any]) -> dict[str, Any]:
        clash = sorted((RESERVED_TAG_KEYS | {"tags"}) & value.keys())
        if clash:
            raise ValueError(f"metadata_tags usa chave reservada: {', '.join(clash)}")
        return value

    @field_validator("index_paths")
    @classmethod
    def _index_paths(cls, values: list[str]) -> list[str]:
        paths = [validate_index_path(v) for v in values]
        for path in paths:
            if "/" not in path:
                raise ValueError(
                    f"index_paths exige DOMINIO/NO_TEMATICO, recebeu {path!r}: "
                    f"documento entra no nivel 2, abaixo de um no tematico"
                )
        return list(dict.fromkeys(paths))

    @property
    def content_hash(self) -> str:
        return content_sha256(self.raw_content)


class IngestStatus(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    # Conteudo identico, mas classificacao/escopo/titulo mudaram: nova revisao
    # SEM reembedar.
    METADATA_UPDATED = "metadata_updated"
    # Hash ja presente e sem force_update: nada foi gravado.
    DUPLICATE = "duplicate"


class IngestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: IngestStatus
    document_id: UUID
    slug: str
    version: int
    content_hash: str
    chunk_count: int
    # Em DUPLICATE por outro slug, o documento que ja tinha este conteudo.
    duplicate_of: str | None = None
    elapsed_ms: float = 0.0


class IngestJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class IngestJob(BaseModel):
    """Estado de um pedido enfileirado por `submit`."""

    job_id: UUID
    slug: str
    state: IngestJobState = IngestJobState.QUEUED
    result: IngestResult | None = None
    error: str | None = None
    submitted_at: datetime
    finished_at: datetime | None = None


class SearchResult(BaseModel):
    """Um chunk recuperado, com a trilha de como ele chegou ao topo."""

    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    document_id: UUID
    slug: str
    title: str
    doc_type: str
    access_level: AccessLevel
    department_scope: list[str]
    document_version: int
    source_url_or_path: str | None = None
    chunk_index: int
    heading_path: str
    content: str
    token_count: int
    metadata_tags: dict[str, Any] = Field(default_factory=dict)
    rrf_score: float
    # Posicao em cada lista (1 = melhor); None = nao apareceu naquela lista.
    dense_rank: int | None = None
    sparse_rank: int | None = None
    cosine_distance: float | None = None
    lexical_score: float | None = None


class SearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    results: list[SearchResult]
    token_total: int
    # None = hibrida completa. Preenchido = uma das pernas nao rodou e o
    # motivo, para que degradacao nunca aconteca em silencio.
    degraded: str | None = None
    elapsed_ms: float = 0.0


class DocumentView(BaseModel):
    """Documento integro, ja conferido contra o escopo de quem pediu."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    slug: str
    title: str
    doc_type: str
    source_url_or_path: str | None = None
    content_format: str
    raw_content: str
    content_hash: str
    version: int
    access_level: AccessLevel
    department_scope: list[str]
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_count: int
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Meta-indice
# ---------------------------------------------------------------------------


class DocumentPointer(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: UUID
    slug: str
    title: str
    doc_type: str
    version: int
    updated_at: datetime


class MetaIndexNode(BaseModel):
    id: UUID
    level: int
    key: str
    path: str
    title: str = ""
    description: str = ""
    # Documentos VISIVEIS para quem navegou, na subarvore deste no.
    document_count: int = 0
    document: DocumentPointer | None = None
    children: list[MetaIndexNode] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Montagem JIT do turno
# ---------------------------------------------------------------------------


class TurnContext(BaseModel):
    """Contexto de um turno, montado na hora e LIMITADO por construcao.

    Core fixo + janela curta de turnos + poucos chunks. Nao existe caminho
    pelo qual este objeto cresca com a idade da conversa, entao nao existe
    "memoria em 99%" para compactar.
    """

    model_config = ConfigDict(frozen=True)

    system: SystemContext
    history: list[HistoryMessage]
    wiki: SearchResponse | None
    tokens: dict[str, int]

    @property
    def token_total(self) -> int:
        return sum(self.tokens.values())
