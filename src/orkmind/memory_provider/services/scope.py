"""Pre-filtering de escopo: o UNICO lugar que escreve o WHERE de RBAC.

Busca hibrida, meta-indice e leitura de documento integro importam daqui. Um
segundo lugar montando esse filtro na mao e exatamente como uma regra de
acesso diverge da outra.
"""

from __future__ import annotations

from orkmind.memory_provider.models.schemas import MemoryScopeFilter

# Quantos placeholders `document_scope_clause` consome.
SCOPE_PARAM_COUNT = 3


def document_scope_clause(alias: str, first_param: int) -> str:
    """Condicao sobre `wiki_documents`. Usa $first_param .. $first_param+2.

    Determinista: so documento ativo, de nivel permitido e - salvo
    cross_department - da empresa toda ('{}') ou de um departamento de quem le.
    """
    a, p = alias, first_param
    return (
        f"{a}.status = 'active' "
        f"AND {a}.access_level = ANY(${p}::text[]) "
        f"AND (${p + 1}::boolean "
        f"OR {a}.department_scope = '{{}}'::text[] "
        f"OR {a}.department_scope && ${p + 2}::text[])"
    )


def document_scope_args(scope: MemoryScopeFilter) -> tuple[list[str], bool, list[str]]:
    return scope.effective_access_levels, scope.cross_department, list(scope.departments)


def classification_reach_sql(first_param: int) -> str:
    """`SELECT` booleano: o escopo alcancaria um documento com esta classificacao?

    Usa $first_param .. $first_param+2 do escopo, +3 para o nivel e +4 para os
    departamentos. Serve a escrita, onde a classificacao ainda nao esta numa
    linha do banco (a do documento que vai nascer) ou nao deve ser julgada pelo
    status (a de um documento aposentado que ocupa o slug).

    Nao e uma segunda regra: a condicao e a mesma `document_scope_clause`,
    aplicada a uma linha montada na hora com status fixo em 'active'. Assim
    "quem escreve so grava o que conseguiria ler" nao tem como divergir do
    filtro de leitura.
    """
    p = first_param
    return (
        "SELECT EXISTS (SELECT 1 FROM (SELECT 'active'::text AS status, "
        f"${p + 3}::text AS access_level, ${p + 4}::text[] AS department_scope) c "
        f"WHERE {document_scope_clause('c', p)})"
    )
