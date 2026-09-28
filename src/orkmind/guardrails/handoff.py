"""Handoff de conteudo governado (fase G3, D-MA12 reescopado).

O OrkMind VALIDA e ARMAZENA handoff; nunca o dispara. Fora de escopo
permanente: rotacao forcada, encerramento de sessao, criacao de sessao
filha, qualquer chamada do OrkMind PARA um runtime. O fluxo e sempre
puxado pelo runtime:

1. O runtime decide rotacionar e monta o payload conforme as
   handoff-rules (entries `rule` com situation ["handoff-rules"]).
2. `submeter_handoff` valida o payload (secoes obrigatorias, conteudo
   minimo, ate MAX_REFACOES refacoes) e, aceito, grava a entry
   `handoff` (resumo compacto, origin/destination) e o pacote completo
   em `semantic_log` (package_id), ambos encadeados por parent_id a
   entry `session` da sessao de origem.
3. A sessao seguinte recebe o resumo e o package_id via
   `handoff_para_sessao` (o runtime injeta no primeiro turno); o
   conteudo integral fica em semantic_log, acessivel por busca.

O payload e um objeto JSON qualquer cujo conjunto de chaves cubra as
secoes obrigatorias; chaves extras sao preservadas no pacote. Nada aqui
referencia campos de runtime ou orquestrador especifico: adaptacao de
formato, quando necessaria, vive no lado de quem submete.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from orkmind.core.models import MemoryEntry
    from orkmind.core.semantic_layer import SemanticLayer

logger = logging.getLogger(__name__)

# Tag de situacao que marca as regras de handoff governadas.
SITUACAO_HANDOFF_RULES = "handoff-rules"

# Secoes obrigatorias default do pacote (mesmo conteudo do seed). Usadas
# como fallback declarado quando as handoff-rules nao estao seedadas.
SECOES_OBRIGATORIAS_DEFAULT = (
    "progresso",
    "decisoes",
    "referencias_criticas",
    "proximos_passos",
)

# Conteudo minimo por secao (caracteres do texto da secao).
MIN_CHARS_SECAO_DEFAULT = 40

# Refacoes permitidas: payload invalido volta com o que falta ate este
# limite; depois o pacote e aceito com avisos, porque perder o conteudo
# do handoff seria pior do que armazena-lo incompleto.
MAX_REFACOES = 2

# Tamanho do trecho de cada secao no resumo compacto injetado na sessao
# seguinte (o pacote integral fica em semantic_log).
RESUMO_CHARS_POR_SECAO = 300

_SEPARADORES = re.compile(r"[\s\-]+")


class HandoffValidation(BaseModel):
    """Resultado da validacao de um payload contra as handoff-rules."""

    valido: bool
    secoes_obrigatorias: list[str]
    secoes_faltantes: list[str] = Field(default_factory=list)
    secoes_curtas: list[str] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)


def normalizar_secao(nome: str) -> str:
    """Nome canonico de secao: minusculo, sem acento, com underscore."""
    normalizado = unicodedata.normalize("NFKD", str(nome or ""))
    ascii_only = normalizado.encode("ascii", "ignore").decode("ascii").lower()
    return _SEPARADORES.sub("_", ascii_only.strip())


def _texto_da_secao(valor: Any) -> str:
    """Conteudo textual de uma secao (objetos viram JSON)."""
    if isinstance(valor, str):
        return valor
    if valor is None:
        return ""
    return json.dumps(valor, ensure_ascii=False, default=str)


async def carregar_handoff_rules(
    layer: "SemanticLayer", requester_id: Optional[str] = None
) -> list["MemoryEntry"]:
    """Handoff-rules governadas visiveis para o solicitante."""
    return await layer.store.search_by_tags(
        tags={"situation": [SITUACAO_HANDOFF_RULES]},
        collection="rule",
        requester_id=requester_id,
    )


def secoes_das_rules(
    rules: list["MemoryEntry"],
) -> tuple[list[str], int, bool]:
    """Secoes obrigatorias e conteudo minimo definidos pelas rules.

    Retorna (secoes, min_chars, veio_de_seed). Sem rules seedadas, o
    fallback declarado sao os defaults do modulo: fail-safe de conteudo,
    nunca validacao vazia.
    """
    secoes: list[str] = []
    min_chars = MIN_CHARS_SECAO_DEFAULT
    for rule in rules:
        secao = rule.metadata.get("handoff_section")
        if secao:
            nome = normalizar_secao(str(secao))
            if nome and nome not in secoes:
                secoes.append(nome)
        try:
            min_da_rule = int(rule.metadata.get("handoff_min_chars", 0))
        except (TypeError, ValueError):
            min_da_rule = 0
        if min_da_rule > 0:
            min_chars = min_da_rule
    if not secoes:
        return list(SECOES_OBRIGATORIAS_DEFAULT), MIN_CHARS_SECAO_DEFAULT, False
    return secoes, min_chars, True


def validar_payload(
    payload: dict[str, Any],
    secoes_obrigatorias: list[str],
    min_chars: int = MIN_CHARS_SECAO_DEFAULT,
) -> HandoffValidation:
    """Valida o payload: secoes obrigatorias presentes e com conteudo."""
    presentes = {
        normalizar_secao(chave): _texto_da_secao(valor)
        for chave, valor in payload.items()
    }
    faltantes: list[str] = []
    curtas: list[str] = []
    for secao in secoes_obrigatorias:
        texto = presentes.get(secao)
        if texto is None:
            faltantes.append(secao)
        elif len(texto.strip()) < min_chars:
            curtas.append(secao)

    avisos: list[str] = []
    if faltantes:
        avisos.append(
            "Secoes obrigatorias ausentes: " + ", ".join(faltantes) + "."
        )
    if curtas:
        avisos.append(
            "Secoes com conteudo abaixo do minimo de "
            f"{min_chars} caracteres: " + ", ".join(curtas) + "."
        )
    return HandoffValidation(
        valido=not faltantes and not curtas,
        secoes_obrigatorias=list(secoes_obrigatorias),
        secoes_faltantes=faltantes,
        secoes_curtas=curtas,
        avisos=avisos,
    )


def resumo_compacto(
    payload: dict[str, Any],
    secoes_obrigatorias: list[str],
    chars_por_secao: int = RESUMO_CHARS_POR_SECAO,
) -> str:
    """Resumo compacto do pacote, na ordem das secoes obrigatorias.

    E o que a sessao seguinte carrega no primeiro turno; o conteudo
    integral fica no semantic_log, acessivel pelo package_id.
    """
    presentes = {
        normalizar_secao(chave): _texto_da_secao(valor)
        for chave, valor in payload.items()
    }
    ordem = list(secoes_obrigatorias) + [
        s for s in presentes if s not in secoes_obrigatorias
    ]
    partes: list[str] = []
    for secao in ordem:
        texto = (presentes.get(secao) or "").strip()
        if not texto:
            continue
        if len(texto) > chars_por_secao:
            texto = texto[:chars_por_secao].rstrip() + "..."
        partes.append(f"### {secao}\n{texto}")
    return "\n".join(partes)


async def _entry_da_sessao(
    layer: "SemanticLayer",
    session_id: str,
    requester_id: Optional[str],
) -> "MemoryEntry":
    """Entry `session` da sessao, criada se ainda nao existir.

    E o no pai da cadeia: handoff e pacote apontam para ela via
    parent_id.
    """
    from orkmind.core.models import MemoryEntry

    existentes = await layer.store.search_by_tags(
        tags={},
        collection="session",
        limit=1000,
        requester_id=requester_id,
    )
    for entry in existentes:
        if entry.metadata.get("session_id") == session_id:
            return entry

    nova = MemoryEntry(
        content=f"Sessao {session_id}",
        collection="session",
        metadata={"session_id": session_id},
        source="agent",
        visibility="public",
        author_id=requester_id,
    )
    entry_id, _ = await layer.add_memory(nova)
    nova.id = entry_id
    return nova


async def submeter_handoff(
    layer: "SemanticLayer",
    *,
    session_id: str,
    payload: dict[str, Any],
    requester_id: Optional[str] = None,
    origin: Optional[str] = None,
    destination: Optional[str] = None,
    refacao: int = 0,
) -> dict[str, Any]:
    """Valida e armazena um pacote de handoff submetido pelo runtime.

    Payload invalido com refacoes disponiveis volta com
    status='refazer' e a lista exata do que falta. Esgotadas as
    refacoes, o pacote e aceito com avisos (conteudo nunca se perde).
    Aceito, grava `handoff` + `semantic_log` encadeados por parent_id a
    entry `session` e devolve handoff_id/package_id.
    """
    from orkmind.core.models import MemoryEntry

    rules = await carregar_handoff_rules(layer, requester_id)
    secoes, min_chars, de_seed = secoes_das_rules(rules)
    validacao = validar_payload(payload, secoes, min_chars)

    avisos = list(validacao.avisos)
    if not de_seed:
        avisos.append(
            "handoff-rules nao seedadas no store; validacao usou as "
            "secoes default do modulo."
        )

    if not validacao.valido and refacao < MAX_REFACOES:
        return {
            "status": "refazer",
            "refacao": refacao + 1,
            "max_refacoes": MAX_REFACOES,
            "secoes_obrigatorias": validacao.secoes_obrigatorias,
            "secoes_faltantes": validacao.secoes_faltantes,
            "secoes_curtas": validacao.secoes_curtas,
            "message": (
                "Payload de handoff incompleto. Refaca cobrindo as secoes "
                "apontadas (minimo de "
                f"{min_chars} caracteres por secao) e submeta novamente. "
                f"Tentativa {refacao + 1} de {MAX_REFACOES}."
            ),
        }

    if not validacao.valido:
        avisos.append(
            f"Pacote aceito com pendencias apos {MAX_REFACOES} refacoes: "
            "conteudo preservado, mas incompleto."
        )

    sessao = await _entry_da_sessao(layer, session_id, requester_id)
    package_id = str(uuid.uuid4())
    origem = origin or session_id

    pacote = MemoryEntry(
        content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        collection="semantic_log",
        tags={"situation": ["handoff"]},
        metadata={
            "package_id": package_id,
            "session_id": session_id,
            "origin": origem,
        },
        parent_id=sessao.id,
        source="agent",
        visibility="public",
        author_id=requester_id,
    )
    pacote_id, pacote_warnings = await layer.add_memory(pacote)

    entry_handoff = MemoryEntry(
        content=resumo_compacto(payload, secoes),
        collection="handoff",
        # editors '*' permite que a sessao seguinte (mesmo em outro
        # runtime, com outra identidade) marque o destination ao
        # consumir o handoff. O pacote em semantic_log nao leva o
        # coringa: ele e o registro integral e fica imutavel.
        tags={"situation": ["handoff"], "editors": ["*"]},
        metadata={
            "origin": origem,
            "destination": destination or "",
            "package_id": package_id,
            "session_id": session_id,
        },
        parent_id=sessao.id,
        source="agent",
        visibility="public",
        author_id=requester_id,
    )
    handoff_id, handoff_warnings = await layer.add_memory(entry_handoff)

    avisos.extend(pacote_warnings)
    avisos.extend(handoff_warnings)

    return {
        "status": "armazenado",
        "handoff_id": handoff_id,
        "package_id": package_id,
        "package_entry_id": pacote_id,
        "session_entry_id": sessao.id,
        "origin": origem,
        "destination": destination or "",
        "secoes_obrigatorias": validacao.secoes_obrigatorias,
        "avisos": avisos,
        "message": (
            "Handoff validado e armazenado. A proxima sessao recebe o "
            "resumo e o package_id no primeiro turno."
        ),
    }


async def handoff_para_sessao(
    layer: "SemanticLayer",
    session_id: str,
    requester_id: Optional[str] = None,
    claim: bool = True,
) -> Optional[dict[str, Any]]:
    """Handoff destinado a esta sessao, se houver.

    Prioridade: entry com destination == session_id; senao, a mais
    recente sem destination cuja origem nao seja a propria sessao. Com
    claim=True, o consumo marca o destination (melhor esforco; falha de
    ACL nao impede a entrega do resumo).
    """
    entries = await layer.store.search_by_tags(
        tags={"situation": ["handoff"]},
        collection="handoff",
        limit=200,
        requester_id=requester_id,
    )
    candidatas = sorted(entries, key=lambda e: e.created_at, reverse=True)

    alvo = next(
        (e for e in candidatas if e.metadata.get("destination") == session_id),
        None,
    )
    if alvo is None:
        alvo = next(
            (
                e for e in candidatas
                if not e.metadata.get("destination")
                and e.metadata.get("origin") != session_id
            ),
            None,
        )
    if alvo is None:
        return None

    if claim and not alvo.metadata.get("destination"):
        alvo.metadata = {**alvo.metadata, "destination": session_id}
        try:
            await layer.store.update(alvo.id, alvo, requester_id=requester_id)
        except Exception as e:
            logger.debug(
                "Nao foi possivel marcar o destination do handoff %s: %s",
                alvo.id,
                e,
            )

    return {
        "handoff_id": alvo.id,
        "package_id": alvo.metadata.get("package_id", ""),
        "origin": alvo.metadata.get("origin", ""),
        "resumo": alvo.content,
    }
