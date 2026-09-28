"""Leitura de nota em Markdown: frontmatter, wikilinks e tags inline.

Isto e o que separa "pilha de documentos" de "vault": no Obsidian o valor nao
esta so no texto, esta na TEIA - `[[links]]`, backlinks e tags. O parser aqui
e funcao pura; quem persiste a teia e `services/links.py`.

Tres cuidados que parecem detalhe e nao sao:

1. **Codigo nao e link.** `[[isso]]` dentro de bloco cercado, de `~~~` ou de
   crase simples e texto literal, e `#hashtag` dentro de codigo tambem. Sem
   mascarar essas regioes antes, todo trecho de shell script com `#` comentado
   viraria tag.
2. **Link para nota inexistente e valido.** No Obsidian apontar para uma nota
   que ainda nao existe e uso normal: marca o que falta escrever. O parser nao
   valida alvo; a resolucao acontece depois, contra o banco.
3. **Frontmatter e opcional.** Sem PyYAML instalado a nota continua sendo
   ingerida - o bloco vira metadado bruto em vez de dicionario, e nada quebra.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

LinkKind = Literal["wikilink", "embed", "markdown"]

# `[[alvo#ancora|alias]]`, com `!` na frente para transclusao.
_WIKILINK = re.compile(
    r"(?P<embed>!)?\[\["
    r"(?P<target>[^\[\]|#^]+?)"
    r"(?:(?P<anchor_kind>[#^])(?P<anchor>[^\[\]|]+?))?"
    r"(?:\|(?P<alias>[^\[\]]*?))?"
    r"\]\]"
)
# `[texto](destino)` - so interessa quando o destino e interno (sem esquema).
_MDLINK = re.compile(r"(?<!\!)\[(?P<alias>[^\]\n]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", re.I)
# Tag inline: precisa de fronteira a esquerda para nao pegar `id#5` nem cor hex.
_INLINE_TAG = re.compile(r"(?:(?<=^)|(?<=[\s(\[]))#(?P<tag>[A-Za-zÀ-ɏ][\wÀ-ɏ/-]*)")
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_H1 = re.compile(r"^#[ \t]+(?P<title>.+?)[ \t]*#*[ \t]*$", re.M)
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*$", re.M)
_INLINE_CODE = re.compile(r"`[^`\n]+`")

# Quanto texto ao redor do link vira `context` do backlink. Backlink sem
# contexto obriga a abrir a nota de origem so para saber por que ela aponta.
CONTEXT_CHARS = 160

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Nome de nota -> slug canonico, no mesmo formato que a ingestao aceita.

    `[[Minha Nota de OPEX]]` e `minha-nota-de-opex` tem de casar, senao o link
    nunca resolve. Acento e normalizado, nao removido a esmo: "Manutencao" e
    "Manutenção" viram o mesmo slug.
    """
    dobrado = unicodedata.normalize("NFKD", value.strip().casefold())
    sem_acento = "".join(c for c in dobrado if not unicodedata.combining(c))
    # Caminho vira so o nome do arquivo, como o Obsidian resolve.
    sem_acento = sem_acento.rsplit("/", 1)[-1]
    for sufixo in (".md", ".markdown", ".txt"):
        if sem_acento.endswith(sufixo):
            sem_acento = sem_acento[: -len(sufixo)]
    return _SLUG_STRIP.sub("-", sem_acento).strip("-")


@dataclass(frozen=True)
class NoteLink:
    """Uma aresta saindo desta nota. `target` ja vem em forma de slug."""

    target: str
    kind: LinkKind
    raw: str
    alias: str | None = None
    anchor: str | None = None
    position: int = 0
    context: str = ""


@dataclass(frozen=True)
class ParsedNote:
    body: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    links: list[NoteLink] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    title: str | None = None
    aliases: list[str] = field(default_factory=list)
    # De onde veio cada tag: alimenta a coluna `source` de wiki_document_tags.
    tag_sources: dict[str, str] = field(default_factory=dict)


def _mask_code(text: str) -> str:
    """Devolve `text` com todo codigo virado espaco, preservando os offsets.

    Mascarar em vez de remover mantem `position` valido contra o texto
    original, que e o que o `context` do backlink precisa.
    """
    marcado = list(text)

    def apagar(inicio: int, fim: int) -> None:
        for i in range(inicio, min(fim, len(marcado))):
            if marcado[i] != "\n":
                marcado[i] = " "

    cercas = list(_FENCE.finditer(text))
    i = 0
    while i < len(cercas):
        abre = cercas[i]
        marcador = abre.group(1)[0]
        fecha = next(
            (c for c in cercas[i + 1 :] if c.group(1)[0] == marcador),
            None,
        )
        fim = fecha.end() if fecha else len(text)
        apagar(abre.start(), fim)
        i = cercas.index(fecha) + 1 if fecha else len(cercas)

    mascarado = "".join(marcado)
    for trecho in _INLINE_CODE.finditer(mascarado):
        apagar(trecho.start(), trecho.end())
    return "".join(marcado)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Separa o bloco `---` inicial do corpo. Sem PyYAML, degrada sem quebrar."""
    casou = _FRONTMATTER.match(text)
    if casou is None:
        return {}, text
    corpo = text[casou.end() :]
    bruto = casou.group("body")
    try:
        import yaml  # import LAZY: so quem usa frontmatter paga
    except ImportError:
        logger.warning(
            "frontmatter presente mas PyYAML nao instalado; guardando como texto bruto. "
            'Instale com: pip install "orkmind[documents]"'
        )
        return {"_frontmatter_bruto": bruto}, corpo
    try:
        dados = yaml.safe_load(bruto)
    except yaml.YAMLError as e:
        logger.warning("frontmatter invalido, guardando como texto bruto: %s", e)
        return {"_frontmatter_bruto": bruto}, corpo
    return (dados if isinstance(dados, dict) else {"_frontmatter_bruto": bruto}), corpo


def _normalize_tags(valores: Any) -> list[str]:
    """Aceita as formas que o Obsidian aceita: lista, string com virgulas, `#tag`."""
    if valores is None:
        return []
    if isinstance(valores, str):
        valores = re.split(r"[,\s]+", valores)
    if not isinstance(valores, (list, tuple, set)):
        valores = [valores]
    saida: dict[str, None] = {}
    for item in valores:
        texto = str(item).strip().lstrip("#").strip().lower()
        if texto:
            saida.setdefault(texto, None)
    return list(saida)


def _context_around(texto: str, inicio: int, fim: int) -> str:
    folga = CONTEXT_CHARS // 2
    trecho = texto[max(0, inicio - folga) : fim + folga]
    return " ".join(trecho.split())


def parse_note(text: str) -> ParsedNote:
    """Le uma nota Markdown inteira: frontmatter, links, tags e titulo."""
    normalizado = text.replace("\r\n", "\n").replace("\r", "\n")
    frontmatter, corpo = _parse_frontmatter(normalizado)
    # Offset do corpo dentro do original, para `position` apontar ao lugar certo.
    deslocamento = len(normalizado) - len(corpo)
    visivel = _mask_code(corpo)

    links: list[NoteLink] = []
    vistos: set[tuple[str, str, str | None]] = set()

    for m in _WIKILINK.finditer(visivel):
        alvo = slugify(m.group("target"))
        if not alvo:
            continue
        ancora = m.group("anchor")
        tipo: LinkKind = "embed" if m.group("embed") else "wikilink"
        chave = (alvo, tipo, ancora)
        if chave in vistos:
            continue
        vistos.add(chave)
        links.append(
            NoteLink(
                target=alvo,
                kind=tipo,
                raw=corpo[m.start() : m.end()],
                alias=(m.group("alias") or None),
                anchor=(f"{m.group('anchor_kind')}{ancora}" if ancora else None),
                position=deslocamento + m.start(),
                context=_context_around(corpo, m.start(), m.end()),
            )
        )

    for m in _MDLINK.finditer(visivel):
        destino = m.group("target")
        if _EXTERNAL.match(destino):
            continue  # link externo nao e aresta da teia interna
        alvo = slugify(destino.split("#")[0])
        if not alvo or (alvo, "markdown", None) in vistos:
            continue
        vistos.add((alvo, "markdown", None))
        links.append(
            NoteLink(
                target=alvo,
                kind="markdown",
                raw=corpo[m.start() : m.end()],
                alias=(m.group("alias") or None),
                position=deslocamento + m.start(),
                context=_context_around(corpo, m.start(), m.end()),
            )
        )

    origem_tag: dict[str, str] = {}
    for t in _normalize_tags(frontmatter.get("tags")) + _normalize_tags(frontmatter.get("tag")):
        origem_tag.setdefault(t, "frontmatter")
    for m in _INLINE_TAG.finditer(visivel):
        origem_tag.setdefault(m.group("tag").lower(), "inline")

    titulo = frontmatter.get("title")
    if not isinstance(titulo, str) or not titulo.strip():
        cabecalho = _H1.search(visivel)
        titulo = cabecalho.group("title").strip() if cabecalho else None

    return ParsedNote(
        body=corpo,
        frontmatter=frontmatter,
        links=sorted(links, key=lambda x: x.position),
        tags=sorted(origem_tag),
        tag_sources=origem_tag,
        title=titulo,
        aliases=_normalize_tags(frontmatter.get("aliases") or frontmatter.get("alias")),
    )
