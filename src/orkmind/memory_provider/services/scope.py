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
