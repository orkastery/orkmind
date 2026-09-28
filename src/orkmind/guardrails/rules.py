"""Funcoes puras de verificacao e formatacao de regras (fase G1).

Extraidas da integracao original de runtime para o core, agnosticas de
runtime: nada aqui conhece runtime nenhum, nem faz I/O. As funcoes
operam sobre dicts de regra com as chaves ja usadas na fronteira do
backend (content, collection, priority, tags, injection_risk, conflict).

Decisoes cobertas:
- D-MA7: destaque visual do bloco de regras e reforco condensado.
- D-MA9: bloco de fail-safe quando as regras ficam indisponiveis.
- D-MA10: hash SHA-256 estavel do conjunto de regras ativas.

A saida das funcoes de formatacao e byte a byte igual a que o plugin
produzia antes da extracao (testes de caracterizacao em
tests/unit/test_hermes_plugin_caracterizacao.py).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

# Estimativa grosseira usada para orcamento: 1 token ~ 4 caracteres.
CHARS_PER_TOKEN = 4

# D-MA7: separador visual isolando o bloco de regras do restante do prompt.
RULES_SEPARATOR = "---"

# D-MA7: limites do reforco condensado de regras (recencia).
REINFORCEMENT_MAX_RULES = 5
REINFORCEMENT_ESSENCE_CHARS = 200

# D-MA9: bloco de alerta exibido quando regras mandatorias ja foram
# carregadas numa sessao mas o backend ficou indisponivel. Melhor
# alertar explicitamente do que operar em silencio sem governanca.
FALLBACK_NO_RULES = (
    "\n## ALERTA: Regras de Governanca Indisponiveis\n"
    "O OrkMind nao conseguiu carregar as regras mandatorias.\n"
    "NAO prossiga com nenhuma operacao critica ate que as\n"
    "regras estejam disponiveis. Tente reconectar via orkmind_rules.\n"
)


def compute_rules_hash(contents: Iterable[str]) -> str:
    """Hash SHA-256 estavel de um conjunto de regras (D-MA10).

    Ordena os conteudos antes de concatenar, de forma que o hash dependa
    apenas do conjunto de regras ativas, nao da ordem de retorno da query.
    Retorna string vazia quando nao ha regras.
    """
    ordered = sorted(c for c in contents if c)
    if not ordered:
        return ""
    return hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()


def rules_hash_from_dicts(rules: list[dict[str, Any]]) -> str:
    """Hash D-MA10 a partir de dicts de regra (chave `content`)."""
    return compute_rules_hash(str(r.get("content", "")) for r in rules if r.get("content"))


def filtrar_regras_seguras(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclui regras com injection_risk ou conflict (D-MA1).

    Regras sob suspeita nunca entram no contexto sem revisao humana.
    """
    return [
        r for r in rules
        if not r.get("injection_risk") and not r.get("conflict")
    ]


def agrupar_por_dominio(
    rules: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Agrupa regras pela primeira tag de dominio (fallback: geral)."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        tags = rule.get("tags") or {}
        domains = tags.get("domain") or []
        domain = domains[0] if domains else "geral"
        grouped.setdefault(domain, []).append(rule)
    return grouped


def format_rules_block(rules: list[dict[str, Any]], budget_chars: int) -> str:
    """Formata as regras mandatorias com destaque visual (D-MA7).

    Aplica caixa alta no titulo, marcadores por prioridade, agrupamento
    por dominio e separadores isolando o bloco de regras do restante do
    prompt. O conteudo de cada regra e SEMPRE integral (E3); o budget so
    pode omitir regras inteiras, nunca truncar uma regra.
    """
    grouped = agrupar_por_dominio(rules)

    lines: list[str] = [
        "",
        RULES_SEPARATOR,
        "## REGRAS MANDATORIAS (OBEDECER SEMPRE)",
        "",
    ]
    used = 0
    truncated = 0
    rendered = 0

    for domain in sorted(grouped):
        group_lines: list[str] = []
        for rule in grouped[domain]:
            content = str(rule.get("content", "")).strip()
            if not content:
                continue
            collection = rule.get("collection", "")
            priority = rule.get("priority", "")
            marker = "[CRITICA]" if priority == "critical" else "[MANDATORIA]"
            line = f"- {marker} [{collection}] {content}"
            if used + len(line) > budget_chars and rendered > 0:
                truncated += 1
                continue
            group_lines.append(line)
            used += len(line)
            rendered += 1
        if group_lines:
            lines.append(f"### DOMINIO: {domain.upper()}")
            lines.extend(group_lines)
            lines.append("")

    if not rendered:
        return ""

    if truncated:
        lines.append(
            f"({truncated} regra(s) omitida(s) por budget. "
            "Use orkmind_rules para ver todas.)"
        )
    lines.append(RULES_SEPARATOR)
    lines.append("")
    return "\n".join(lines)


def format_rules_reinforcement(
    rules: list[dict[str, Any]],
    max_rules: int = REINFORCEMENT_MAX_RULES,
    essence_chars: int = REINFORCEMENT_ESSENCE_CHARS,
) -> str:
    """Reforco condensado das regras na posicao de recencia (D-MA7).

    Usa a essencia (primeiros caracteres) das regras em vez do texto
    completo: o bloco integral ja esta no inicio do contexto.
    """
    if not rules:
        return ""

    lines = ["", "## REGRAS ATIVAS (obedecer SEMPRE)"]
    for rule in rules[:max_rules]:
        essence = str(rule.get("content", "")).strip()
        if not essence:
            continue
        if len(essence) > essence_chars:
            essence = essence[:essence_chars].rstrip() + "..."
        lines.append(f"- {essence}")
    if len(lines) == 2:
        return ""
    lines.append(
        "(Regras completas no system prompt. Duvida? Use orkmind_rules.)"
    )
    return "\n".join(lines)


def formatar_bloco_reinjecao(rules: list[dict[str, Any]]) -> str:
    """Bloco de reinjecao pronto para regras criticas ausentes (G1).

    Mesmo formato D-MA7 do bloco do system prompt, com E3 integral e SEM
    corte por budget: um bloco de reinjecao incompleto seria pior do que
    nenhum, porque criaria a ilusao de conformidade.
    """
    if not rules:
        return ""
    budget = sum(
        len(str(r.get("content", ""))) + 64 for r in rules
    )
    return format_rules_block(rules, budget)
