"""Deteccao de prompt injection e integridade de conteudo para OrkMind."""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Caracteres Unicode invisiveis suspeitos
_INVISIBLE_CODEPOINTS = {
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\u2060",  # word joiner
    "\ufeff",  # BOM / zero-width no-break space
}

# Padroes de prompt injection classico (EN + PT)
_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|prompts)",
            re.IGNORECASE,
        ),
        "Prompt injection classico: tentativa de ignorar instrucoes anteriores",
    ),
    (
        re.compile(r"disregard\s+(all\s+)?(above|previous|prior)", re.IGNORECASE),
        "Prompt injection classico: tentativa de descartar contexto anterior",
    ),
    (
        re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
        "Prompt injection classico: tentativa de redefinir identidade",
    ),
    (
        re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
        "Prompt injection classico: tentativa de injetar novas instrucoes",
    ),
    (
        re.compile(r"system\s+prompt", re.IGNORECASE),
        "Prompt injection classico: referencia a system prompt",
    ),
    (
        re.compile(r"override\s+(all\s+)?(rules|instructions|settings)", re.IGNORECASE),
        "Prompt injection classico: tentativa de sobrescrita de regras",
    ),
    (
        re.compile(r"forget\s+everything", re.IGNORECASE),
        "Prompt injection classico: tentativa de apagar contexto",
    ),
    # Variantes em portugues
    (
        re.compile(
            r"ignore\s+(todas?\s+)?(instruc|regra|comando)",
            re.IGNORECASE,
        ),
        "Prompt injection (PT): tentativa de ignorar instrucoes",
    ),
    (
        re.compile(
            r"desconsider[ea]\s+(tudo|todas?\s+as\s+(instruc|regra))",
            re.IGNORECASE,
        ),
        "Prompt injection (PT): tentativa de desconsiderar regras",
    ),
    (
        re.compile(
            r"esquec[ea]\s+(tudo|todas?\s+as\s+(instruc|regra))",
            re.IGNORECASE,
        ),
        "Prompt injection (PT): tentativa de apagar contexto",
    ),
]

# Delimitadores de prompt conhecidos
_DELIMITER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<\|im_start\|>"), "Delimitador de prompt detectado: <|im_start|>"),
    (re.compile(r"\[INST\]"), "Delimitador de prompt detectado: [INST]"),
    (re.compile(r"<<SYS>>"), "Delimitador de prompt detectado: <<SYS>>"),
    (re.compile(r"</s>"), "Delimitador de prompt detectado: </s>"),
    (re.compile(r"---\s*SYSTEM\s*---"), "Delimitador de prompt detectado: ---SYSTEM---"),
]

# Comandos destrutivos
_DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"delete\s+all\s+(rules|memories|entries|data)", re.IGNORECASE),
        "Comando destrutivo detectado: tentativa de deletar tudo",
    ),
    (
        re.compile(r"remove\s+all\s+(memories|rules|entries)", re.IGNORECASE),
        "Comando destrutivo detectado: tentativa de remover tudo",
    ),
    (
        re.compile(r"clear\s+all", re.IGNORECASE),
        "Comando destrutivo detectado: tentativa de limpar tudo",
    ),
    (
        re.compile(r"drop\s+table", re.IGNORECASE),
        "Comando destrutivo detectado: tentativa de drop table",
    ),
]

# Base64: bloco longo (>=40 chars) que parece payload escondido
_BASE64_PATTERN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def detect_injection(content: str) -> tuple[bool, list[str]]:
    """Detecta padroes de prompt injection no conteudo.

    Retorna (is_suspect, reasons) onde reasons e a lista de padroes encontrados.
    """
    reasons: list[str] = []

    # 1. Unicode invisivel
    for i, char in enumerate(content):
        if char in _INVISIBLE_CODEPOINTS:
            cp = f"U+{ord(char):04X}"
            name = unicodedata.name(char, "desconhecido")
            reasons.append(
                f"Unicode invisivel detectado: {cp} ({name}) na posicao {i}"
            )
        elif unicodedata.category(char) == "Cf" and char not in _INVISIBLE_CODEPOINTS:
            cp = f"U+{ord(char):04X}"
            reasons.append(
                f"Caractere de formatacao suspeito: {cp} na posicao {i}"
            )

    # 2. Base64 embutido
    for match in _BASE64_PATTERN.finditer(content):
        reasons.append(
            f"Bloco base64 suspeito detectado (posicao {match.start()}, "
            f"tamanho {len(match.group())})"
        )

    # 3. Prompt injection classico
    for pattern, reason in _INJECTION_PATTERNS:
        if pattern.search(content):
            reasons.append(reason)

    # 4. Delimitadores de prompt
    for pattern, reason in _DELIMITER_PATTERNS:
        if pattern.search(content):
            reasons.append(reason)

    # 5. Comandos destrutivos
    for pattern, reason in _DESTRUCTIVE_PATTERNS:
        if pattern.search(content):
            reasons.append(reason)

    return (len(reasons) > 0, reasons)


def compute_content_hash(content: str) -> str:
    """Computa SHA-256 do conteudo (UTF-8 encoded)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
