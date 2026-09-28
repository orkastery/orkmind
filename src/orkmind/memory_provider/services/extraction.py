"""Extracao de texto: PDF, DOCX, CSV, TXT, Markdown, HTML.

O provider guarda o documento INTEIRO, nao um ponteiro para ele. Para formato
textual isso e literal: `raw_content` recebe o arquivo byte a byte. Para
binario (PDF, DOCX) o que se guarda e o texto extraido, e `metadata` registra
de que formato ele veio - o binario original continua onde estava, apontado
por `source_url_or_path`.

E uma escolha, nao um descuido: o que a memoria precisa servir ao agente e
texto pesquisavel e citavel. Guardar o PDF em si no Postgres so faz sentido
quando o objetivo for reimprimir o original, e ai o lugar certo e um bucket
de objetos com o hash no banco.

Planilha vira TABELA MARKDOWN de proposito. CSV cru quebra mal em chunk e
some no full-text; tabela markdown o chunker ja sabe fatiar repetindo o
cabecalho, entao cada pedaco continua legivel sozinho.

As dependencias binarias sao opcionais (`orkmind[documents]`): sem elas os
formatos de texto seguem funcionando e o binario falha com mensagem dizendo
o que instalar.
"""

from __future__ import annotations

import csv
import io
import logging
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orkmind.memory_provider.errors import MemoryProviderError

logger = logging.getLogger(__name__)

# Extensao -> (content_format gravado, rotulo de origem)
TEXT_FORMATS: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    ".txt": "text",
    ".text": "text",
    ".log": "text",
    ".rst": "text",
    ".html": "html",
    ".htm": "html",
}
BINARY_FORMATS = {".pdf": "pdf_text", ".docx": "docx_text"}
TABLE_FORMATS = {".csv": ",", ".tsv": "\t"}

SUPPORTED_SUFFIXES = frozenset(TEXT_FORMATS) | frozenset(BINARY_FORMATS) | frozenset(TABLE_FORMATS)

# Teto de linhas convertidas de planilha. Acima disso o documento deixa de ser
# documento e vira base de dados - e o lugar dela nao e a memoria semantica.
MAX_TABLE_ROWS = 5000


class UnsupportedFormatError(MemoryProviderError):
    """Extensao fora do conjunto suportado."""


class ExtractionDependencyError(MemoryProviderError):
    """Formato binario sem a biblioteca correspondente instalada."""


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    content_format: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _decode(data: bytes) -> str:
    """Bytes -> str tolerante. Vault real tem arquivo legado em latin-1 no meio.

    UTF-8 estrito primeiro (o caso normal); o que falhar cai em cp1252, que e
    superconjunto do latin-1, cobre o legado ocidental e nunca levanta erro.

    Deteccao estatistica de codificacao foi DESCARTADA de proposito: medida
    aqui com `charset_normalizer`, ela errou "manutencao" em latin-1 nos dois
    tamanhos testados - em amostra curta devolveu alfabeto arabe, em amostra
    longa devolveu outro mapa ocidental errado. Para um corpus em portugues,
    cp1252 acerta sempre e erra de forma previsivel. Texto em codificacao
    oriental precisa ser convertido antes de ingerir.
    """
    for codec in ("utf-8", "utf-8-sig"):
        try:
            return data.decode(codec)
        except UnicodeDecodeError:
            continue
    logger.info("arquivo nao e UTF-8; decodificando como cp1252")
    return data.decode("cp1252", errors="replace")


def _normalize(text: str) -> str:
    # NFC junta acento combinante: no macOS o nome/conteudo costuma vir NFD, e
    # "ção" em duas formas diferentes nao casa nem na busca nem no hash.
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _extract_pdf(data: bytes) -> ExtractedDocument:
    try:
        from pypdf import PdfReader  # import LAZY
    except ImportError as e:
        raise ExtractionDependencyError(
            'PDF exige o extra: pip install "orkmind[documents]"'
        ) from e
    leitor = PdfReader(io.BytesIO(data))
    if leitor.is_encrypted:
        try:
            leitor.decrypt("")  # PDF so "protegido contra impressao" abre com senha vazia
        except Exception as e:  # noqa: BLE001
            raise ExtractionDependencyError(f"PDF protegido por senha: {e}") from e
    paginas = []
    for numero, pagina in enumerate(leitor.pages, start=1):
        # Marcador de pagina viravel em citacao: "segundo o contrato, pagina 7".
        conteudo = (pagina.extract_text() or "").strip()
        if conteudo:
            paginas.append(f"## Pagina {numero}\n\n{conteudo}")
    texto = "\n\n".join(paginas)
    if not texto.strip():
        raise ExtractionDependencyError(
            "PDF sem camada de texto (provavelmente digitalizado). Passe por OCR antes de ingerir."
        )
    meta = {"source_format": "pdf", "pages": len(leitor.pages)}
    if leitor.metadata:
        for chave in ("title", "author", "subject"):
            valor = getattr(leitor.metadata, chave, None)
            if valor:
                meta[chave] = str(valor)
    return ExtractedDocument(_normalize(texto), "pdf_text", meta)


def _extract_docx(data: bytes) -> ExtractedDocument:
    try:
        import docx  # import LAZY
    except ImportError as e:
        raise ExtractionDependencyError(
            'DOCX exige o extra: pip install "orkmind[documents]"'
        ) from e
    documento = docx.Document(io.BytesIO(data))
    partes: list[str] = []
    for paragrafo in documento.paragraphs:
        texto = paragrafo.text.strip()
        if not texto:
            continue
        estilo = (paragrafo.style.name or "").lower() if paragrafo.style else ""
        if estilo.startswith("heading"):
            # Titulo do Word vira titulo Markdown: e o que da fronteira de
            # chunk e caminho de secao no embedding.
            nivel = "".join(c for c in estilo if c.isdigit()) or "1"
            partes.append(f"{'#' * min(int(nivel), 6)} {texto}")
        else:
            partes.append(texto)
    for tabela in documento.tables:
        linhas = [[celula.text.strip() for celula in linha.cells] for linha in tabela.rows]
        if linhas:
            partes.append(_rows_to_markdown(linhas))
    return ExtractedDocument(
        _normalize("\n\n".join(partes)),
        "docx_text",
        {"source_format": "docx", "paragraphs": len(documento.paragraphs)},
    )


def _rows_to_markdown(linhas: list[list[str]]) -> str:
    if not linhas:
        return ""
    largura = max(len(linha) for linha in linhas)

    def formatar(linha: list[str]) -> str:
        celulas = [(linha[i] if i < len(linha) else "").replace("|", "\\|") for i in range(largura)]
        return "| " + " | ".join(celulas) + " |"

    cabecalho, corpo = linhas[0], linhas[1:]
    separador = "|" + "|".join(["---"] * largura) + "|"
    return "\n".join([formatar(cabecalho), separador, *(formatar(linha) for linha in corpo)])


def _extract_table(data: bytes, delimitador: str, nome: str) -> ExtractedDocument:
    texto = _decode(data)
    try:
        dialeto: Any = csv.Sniffer().sniff(texto[:4096], delimiters=",;\t|")
    except csv.Error:
        dialeto = csv.excel
        dialeto.delimiter = delimitador
    linhas = list(csv.reader(io.StringIO(texto), dialeto))
    linhas = [linha for linha in linhas if any(c.strip() for c in linha)]
    truncada = len(linhas) > MAX_TABLE_ROWS + 1
    if truncada:
        linhas = linhas[: MAX_TABLE_ROWS + 1]
    corpo = _rows_to_markdown(linhas)
    if truncada:
        corpo += f"\n\n> Tabela truncada em {MAX_TABLE_ROWS} linhas na ingestao."
    return ExtractedDocument(
        _normalize(f"# {nome}\n\n{corpo}"),
        "markdown",
        {
            "source_format": "csv",
            "rows": max(0, len(linhas) - 1),
            "columns": len(linhas[0]) if linhas else 0,
            "truncated": truncada,
        },
    )


def extract_bytes(data: bytes, filename: str) -> ExtractedDocument:
    """Extrai texto de `data`, escolhendo o tratamento pela extensao de `filename`."""
    sufixo = Path(filename).suffix.lower()
    if sufixo in BINARY_FORMATS:
        return _extract_pdf(data) if sufixo == ".pdf" else _extract_docx(data)
    if sufixo in TABLE_FORMATS:
        return _extract_table(data, TABLE_FORMATS[sufixo], Path(filename).stem)
    if sufixo in TEXT_FORMATS:
        return ExtractedDocument(
            _normalize(_decode(data)),
            TEXT_FORMATS[sufixo],
            {"source_format": sufixo.lstrip(".")},
        )
    raise UnsupportedFormatError(
        f"formato nao suportado: {sufixo or filename!r}. "
        f"Suportados: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
    )


def extract_file(path: str | Path) -> ExtractedDocument:
    """Le e extrai um arquivo do disco, registrando o caminho de origem."""
    caminho = Path(path)
    extraido = extract_bytes(caminho.read_bytes(), caminho.name)
    return ExtractedDocument(
        extraido.text,
        extraido.content_format,
        {**extraido.metadata, "source_filename": caminho.name, "bytes": caminho.stat().st_size},
    )
