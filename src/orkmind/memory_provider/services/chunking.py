"""Chunking semantico estruturado.

O fatiamento segue a estrutura do documento em vez de contar caracteres:

- titulos Markdown ate `split_heading_level` (`##`/`###`) sao fronteira DURA:
  um chunk nunca atravessa de uma secao para outra;
- dentro da secao, a unidade e o bloco (paragrafo, lista, tabela, bloco de
  codigo). Bloco que nao cabe desce um degrau: lista vira itens, paragrafo
  vira frases, tabela/codigo viram grupos de linhas (a tabela repete o
  cabecalho, o codigo repete a cerca);
- chunks consecutivos da MESMA secao compartilham um overlap de
  `overlap_ratio` (10% a 15%) do tamanho maximo, alinhado a inicio de frase
  quando da. Nao ha overlap entre secoes (o assunto mudou) nem depois de
  tabela/codigo (meia linha de tabela so atrapalha).

Tudo aqui e funcao pura e sincrona: nao toca em banco nem em rede.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal

from orkmind.memory_provider.config import ChunkingOptions
from orkmind.memory_provider.tokens import estimate_tokens, head_by_tokens, tail_by_tokens

BlockKind = Literal["heading", "paragraph", "list", "code", "table"]

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_LIST_ITEM = re.compile(r"^([ \t]*)(?:[-*+]|\d{1,9}[.)])[ \t]+")
_TABLE_ROW = re.compile(r"^[ \t]*\|")
_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t:|-]*-[ \t:|-]*$")
_SENTENCE_END = re.compile(r"(?<=[.!?…;:])[\"”')\]]*\s+")

# Faixa contratual do overlap. O alvo (`overlap_ratio`) fica dentro dela; o
# piso vence o alinhamento a frase, e o teto e o que cada chunk reserva.
_MIN_OVERLAP_RATIO = 0.10
_MAX_OVERLAP_RATIO = 0.15


@dataclass(frozen=True)
class Chunk:
    index: int
    content: str
    heading_path: tuple[str, ...]
    section_level: int
    token_count: int
    has_code: bool = False
    has_table: bool = False
    has_list: bool = False


@dataclass
class _Block:
    kind: BlockKind
    text: str
    level: int = 0


@dataclass
class _Atom:
    kind: BlockKind
    text: str
    tokens: int
    # Como este atomo se liga ao ANTERIOR quando os dois saem do mesmo bloco.
    joiner: str = "\n\n"


@dataclass
class _Section:
    heading_path: tuple[str, ...]
    level: int
    blocks: list[_Block] = field(default_factory=list)


@dataclass
class _Draft:
    heading_path: tuple[str, ...]
    level: int
    prefix: str = ""
    prefix_joiner: str = "\n\n"
    body: str = ""
    kinds: set[BlockKind] = field(default_factory=set)
    last_kind: BlockKind | None = None

    @property
    def content(self) -> str:
        return f"{self.prefix}{self.prefix_joiner}{self.body}" if self.prefix else self.body

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.content)


# ---------------------------------------------------------------------------
# HTML -> Markdown minimo
# ---------------------------------------------------------------------------


class _HtmlToMarkdown(HTMLParser):
    """Conversao suficiente para chunking: titulos, listas, paragrafos, pre."""

    _SKIP = {"script", "style", "noscript", "template", "head"}
    _BLOCK = {"p", "div", "section", "article", "ul", "ol", "table", "blockquote", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0
        self._in_pre = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif self._skip_depth:
            return
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._out.append("\n- ")
        elif tag == "br":
            self._out.append("\n")
        elif tag == "pre":
            self._in_pre = True
            self._out.append("\n\n```\n")
        elif tag in {"td", "th"}:
            self._out.append(" | ")
        elif tag in self._BLOCK:
            self._out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif self._skip_depth:
            return
        elif tag == "pre":
            self._in_pre = False
            self._out.append("\n```\n\n")
        elif tag in self._BLOCK or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._out.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._out.append(data if self._in_pre else re.sub(r"\s+", " ", data))

    def result(self) -> str:
        text = "".join(self._out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_markdown(html: str) -> str:
    parser = _HtmlToMarkdown()
    parser.feed(html)
    parser.close()
    return parser.result()


# ---------------------------------------------------------------------------
# Blocos e secoes
# ---------------------------------------------------------------------------


def _parse_blocks(text: str) -> list[_Block]:
    lines = text.split("\n")
    blocks: list[_Block] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(_Block("paragraph", "\n".join(paragraph)))
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush_paragraph()
            marker = fence.group(1)
            j = i + 1
            while j < len(lines):
                closing = lines[j].strip()
                if closing.startswith(marker) and set(closing) == {marker[0]}:
                    break
                j += 1
            blocks.append(_Block("code", "\n".join(lines[i : j + 1])))
            i = j + 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            blocks.append(_Block("heading", line.strip(), level=len(heading.group(1))))
            i += 1
            continue
        if _TABLE_ROW.match(line):
            flush_paragraph()
            j = i
            while j < len(lines) and _TABLE_ROW.match(lines[j]):
                j += 1
            blocks.append(_Block("table", "\n".join(lines[i:j])))
            i = j
            continue
        if _LIST_ITEM.match(line):
            flush_paragraph()
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if _LIST_ITEM.match(nxt) or (nxt.strip() and nxt[:1] in " \t"):
                    j += 1
                elif (
                    not nxt.strip()
                    and j + 1 < len(lines)
                    and (_LIST_ITEM.match(lines[j + 1]) or lines[j + 1][:1] in (" ", "\t"))
                    and lines[j + 1].strip()
                ):
                    j += 1
                else:
                    break
            blocks.append(_Block("list", "\n".join(lines[i:j]).rstrip()))
            i = j
            continue
        if not line.strip():
            flush_paragraph()
        else:
            paragraph.append(line.rstrip())
        i += 1
    flush_paragraph()
    return blocks


def _heading_text(line: str) -> str:
    match = _HEADING.match(line)
    return match.group(2).strip() if match else line.strip()


def _split_sections(blocks: list[_Block], title: str, split_level: int) -> list[_Section]:
    root = (title,) if title else ()
    sections = [_Section(heading_path=root, level=0)]
    stack: list[tuple[int, str]] = []
    for block in blocks:
        if block.kind == "heading" and block.level <= split_level:
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, _heading_text(block.text)))
            names = [name for _, name in stack]
            # H1 igual ao titulo do documento nao entra duas vezes no caminho.
            if names and title and names[0].casefold() == title.casefold():
                names = names[1:]
            sections.append(_Section(heading_path=root + tuple(names), level=block.level))
        sections[-1].blocks.append(block)
    return [section for section in sections if section.blocks]


# ---------------------------------------------------------------------------
# Atomos
# ---------------------------------------------------------------------------


def _split_by_budget(text: str, budget: int, kind: BlockKind, joiner: str) -> list[_Atom]:
    atoms: list[_Atom] = []
    offset = 0
    while offset < len(text):
        piece, offset = head_by_tokens(text, budget, offset=offset)
        piece = piece.strip()
        if piece:
            atoms.append(_Atom(kind, piece, estimate_tokens(piece), joiner if atoms else "\n\n"))
    return atoms


def _sentence_atoms(text: str, budget: int, kind: BlockKind) -> list[_Atom]:
    atoms: list[_Atom] = []
    for sentence in _SENTENCE_END.split(" ".join(text.split("\n"))):
        sentence = sentence.strip()
        if not sentence:
            continue
        tokens = estimate_tokens(sentence)
        parts = (
            [_Atom(kind, sentence, tokens)]
            if tokens <= budget
            else _split_by_budget(sentence, budget, kind, " ")
        )
        for part in parts:
            part.joiner = " " if atoms else "\n\n"
            atoms.append(part)
    return atoms


def _list_items(text: str) -> list[str]:
    lines = text.split("\n")
    first = _LIST_ITEM.match(lines[0])
    base_indent = len(first.group(1)) if first else 0
    items: list[list[str]] = []
    for line in lines:
        match = _LIST_ITEM.match(line)
        if match and len(match.group(1)) <= base_indent:
            items.append([line])
        elif items:
            items[-1].append(line)
        else:
            items.append([line])
    return ["\n".join(item).rstrip() for item in items]


def _line_group_atoms(
    body: list[str], budget: int, kind: BlockKind, head: list[str], tail: list[str]
) -> list[_Atom]:
    """Agrupa linhas em partes <= budget; cada parte leva `head` e `tail`."""
    frame = estimate_tokens("\n".join(head + tail))
    room = max(8, budget - frame)
    atoms: list[_Atom] = []
    group: list[str] = []
    spent = 0

    def flush() -> None:
        nonlocal group, spent
        if group:
            text = "\n".join(head + group + tail)
            atoms.append(_Atom(kind, text, estimate_tokens(text)))
            group, spent = [], 0

    for line in body:
        cost = estimate_tokens(line)
        if cost > room:
            flush()
            for piece in _split_by_budget(line, room, kind, "\n\n"):
                text = "\n".join(head + [piece.text] + tail)
                atoms.append(_Atom(kind, text, estimate_tokens(text)))
            continue
        if spent + cost > room:
            flush()
        group.append(line)
        spent += cost
    flush()
    return atoms


def _block_atoms(block: _Block, budget: int) -> list[_Atom]:
    tokens = estimate_tokens(block.text)
    if tokens <= budget or block.kind == "heading":
        return [_Atom(block.kind, block.text, tokens)]
    if block.kind == "paragraph":
        return _sentence_atoms(block.text, budget, "paragraph")
    if block.kind == "list":
        atoms: list[_Atom] = []
        for item in _list_items(block.text):
            cost = estimate_tokens(item)
            parts = (
                [_Atom("list", item, cost)]
                if cost <= budget
                else _sentence_atoms(item, budget, "list")
            )
            for position, part in enumerate(parts):
                if atoms:
                    part.joiner = "\n" if position == 0 else " "
                atoms.append(part)
        return atoms
    lines = block.text.split("\n")
    if block.kind == "code":
        closed = len(lines) > 1 and _FENCE.match(lines[-1]) is not None
        body = lines[1:-1] if closed else lines[1:]
        fence = _FENCE.match(lines[0])
        closing = fence.group(1) if fence else "```"
        return _line_group_atoms(body, budget, "code", [lines[0]], [closing])
    has_header = len(lines) > 1 and _TABLE_RULE.match(lines[1]) is not None
    head = lines[:2] if has_header else []
    return _line_group_atoms(lines[len(head) :], budget, "table", head, [])


# ---------------------------------------------------------------------------
# Empacotamento
# ---------------------------------------------------------------------------


def _overlap_tail(previous: str, budget: int, floor: int, ceiling: int) -> str:
    """Final de `previous` com ~`budget` tokens, alinhado a inicio de frase.

    O alinhamento so vale se o que sobra ainda tiver pelo menos `floor`
    tokens; senao fica o corte em fronteira de palavra, que por sua vez
    nunca fica abaixo de `floor` nem acima de `ceiling`.
    """
    tail = tail_by_tokens(previous, budget, at_least=floor, at_most=ceiling).lstrip()
    for boundary in _SENTENCE_END.finditer(tail):
        aligned = tail[boundary.end() :]
        if estimate_tokens(aligned) >= floor:
            return aligned
        break
    return tail


def _pack_section(section: _Section, options: ChunkingOptions) -> list[_Draft]:
    limit = options.max_tokens
    overlap_budget = round(limit * options.overlap_ratio)
    overlap_floor = math.ceil(limit * _MIN_OVERLAP_RATIO)
    overlap_ceiling = math.floor(limit * _MAX_OVERLAP_RATIO)
    # O atomo precisa caber JUNTO com o maior overlap que o chunk pode herdar.
    atom_budget = limit - overlap_ceiling

    atoms: list[_Atom] = []
    for block in section.blocks:
        atoms.extend(_block_atoms(block, atom_budget))

    drafts: list[_Draft] = []
    current = _Draft(section.heading_path, section.level)
    spent = 0

    for atom in atoms:
        if current.body and spent + atom.tokens > limit:
            drafts.append(current)
            previous = current
            current = _Draft(section.heading_path, section.level)
            spent = 0
            if previous.last_kind not in ("code", "table", "heading"):
                tail = _overlap_tail(
                    previous.content, overlap_budget, overlap_floor, overlap_ceiling
                )
                if tail and tail != previous.content:
                    current.prefix = tail
                    current.prefix_joiner = " " if atom.joiner == " " else "\n\n"
                    spent = estimate_tokens(tail)
        current.body = f"{current.body}{atom.joiner}{atom.text}" if current.body else atom.text
        current.kinds.add(atom.kind)
        current.last_kind = atom.kind
        spent += atom.tokens
    if current.body:
        drafts.append(current)
    return drafts


def _common_prefix(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for left, right in zip(a, b):
        if left != right:
            break
        out.append(left)
    return tuple(out)


def _merge_tiny(drafts: list[_Draft], options: ChunkingOptions) -> list[_Draft]:
    """Funde chunk minusculo (titulo solto, secao de uma linha) com o vizinho."""
    merged: list[_Draft] = []
    pending = list(drafts)
    while pending:
        draft = pending.pop(0)
        if draft.tokens >= options.min_tokens:
            merged.append(draft)
            continue
        nxt = pending[0] if pending else None
        # Para frente, desde que o proximo nao carregue overlap deste aqui
        # (senao o texto entraria duas vezes).
        if nxt and not nxt.prefix and draft.tokens + nxt.tokens <= options.max_tokens:
            nxt.body = f"{draft.body}\n\n{nxt.body}"
            nxt.prefix, nxt.prefix_joiner = draft.prefix, draft.prefix_joiner
            nxt.heading_path = _common_prefix(draft.heading_path, nxt.heading_path)
            nxt.level = min(draft.level, nxt.level)
            nxt.kinds |= draft.kinds
            continue
        prev = merged[-1] if merged else None
        if (
            prev
            and prev.heading_path == draft.heading_path
            and prev.tokens + estimate_tokens(draft.body) <= options.max_tokens
        ):
            prev.body = f"{prev.body}\n\n{draft.body}"
            prev.kinds |= draft.kinds
            prev.last_kind = draft.last_kind
            continue
        merged.append(draft)
    return merged


def chunk_document(
    text: str,
    *,
    title: str = "",
    content_format: str = "markdown",
    options: ChunkingOptions | None = None,
) -> list[Chunk]:
    """Fatia `text` em chunks ordenados. Texto vazio devolve lista vazia."""
    options = options or ChunkingOptions()
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if content_format == "html":
        normalized = html_to_markdown(normalized)
    normalized = normalized.strip()
    if not normalized:
        return []

    drafts: list[_Draft] = []
    sections = _split_sections(_parse_blocks(normalized), title, options.split_heading_level)
    for section in sections:
        drafts.extend(_pack_section(section, options))
    drafts = _merge_tiny(drafts, options)

    return [
        Chunk(
            index=index,
            content=draft.content,
            heading_path=draft.heading_path,
            section_level=draft.level,
            token_count=draft.tokens,
            has_code="code" in draft.kinds,
            has_table="table" in draft.kinds,
            has_list="list" in draft.kinds,
        )
        for index, draft in enumerate(drafts)
    ]
