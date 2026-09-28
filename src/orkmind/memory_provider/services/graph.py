"""A vizinhanca de um documento: o grafo que responde pergunta.

Aresta declarada, nao inferida
------------------------------
Ligar dois documentos porque compartilham uma tag e adjacencia *inferida*: duas
notas com `opex` aparecem juntas mesmo que ninguem jamais as tenha relacionado.
`wiki_links` guarda relacao *autorada*, com o trecho que a justifica. Por isso o
grafo daqui le arestas que alguem escreveu - o mapa deixa de ser de temas e vira
de dependencias, e "quem mais depende desta politica?" passa a ter resposta.

Local por padrao
----------------
Grafo global de milhares de nos e bonito e nao responde nada. O que responde e a
vizinhanca: um documento e N saltos. A caminhada acontece no banco, e o
navegador recebe dezenas de nos em vez de milhares.

O escopo vale nos DOIS lados da aresta
--------------------------------------
Um no so entra se o leitor pode ve-lo, e uma aresta so existe se ele pode ver as
duas pontas. Filtrar so uma ponta protegeria o conteudo e entregaria a relacao -
o mesmo vazamento que `links.py` fecha nos backlinks, aqui multiplicado por
salto: bastaria um documento publico vizinho para um confidencial aparecer no
desenho. Por isso a caminhada anda sobre `visivel`, nunca sobre `wiki_documents`.

Fantasmas
---------
O que foi citado e nunca escrito (`target_document_id IS NULL`) vira no proprio,
marcado `existe = false`, para a tela desenhar tracejado. Ele nao e defeito: e a
pauta do que falta escrever, e so aparece a partir de uma origem visivel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from orkmind.memory_provider.models.schemas import MemoryScopeFilter
from orkmind.memory_provider.services.scope import document_scope_args, document_scope_clause

if TYPE_CHECKING:  # pragma: no cover - so para anotacao
    import asyncpg

# Teto de saltos. Acima disso a "vizinhanca" ja e o acervo inteiro e a tela volta
# a ser o hairball que este modulo existe para evitar.
MAX_DEPTH = 4

# $1 slug de origem, $2..$4 escopo, $5 profundidade, $6 teto de nos.
#
# `visivel` e a unica porta de entrada em `wiki_documents`: a caminhada nunca
# toca a tabela crua. `aresta` exige as duas pontas em `visivel`, entao um
# documento fora do escopo nem serve de ponte entre dois que estao dentro.
#
# Os dois filtros sao REDUNDANTES de proposito, e isso foi medido: trocar so o
# de `aresta` por `wiki_documents`, ou so o da caminhada, nao vaza nada - cada um
# cobre o outro. Trocar os dois vaza. E o vazamento e justamente o sutil: o no
# invisivel continua fora do resultado (o SELECT final tambem passa por
# `visivel`), mas o documento do OUTRO LADO dele aparece, entregando que existe
# uma relacao ali. Quem for "simplificar" um destes joins por achar que e copia
# do outro esta removendo metade de uma trava dupla, nao codigo morto.
#
# A recursao usa UNION (nao UNION ALL) e para em `salto < $5`: um no pode repetir
# por profundidade, nunca infinitamente, e o `min(salto)` depois fica com a
# distancia mais curta.
_VIZINHANCA_SQL = f"""
WITH RECURSIVE visivel AS (
    SELECT d.id, d.slug, d.title, d.doc_type, d.access_level, d.updated_at
    FROM wiki_documents d
    WHERE {document_scope_clause("d", 2)}
),
aresta AS (
    SELECT l.source_document_id AS origem, l.target_document_id AS destino
    FROM wiki_links l
    JOIN visivel o ON o.id = l.source_document_id
    JOIN visivel t ON t.id = l.target_document_id
),
caminhada AS (
    SELECT v.id, 0 AS salto
    FROM visivel v
    WHERE v.slug = $1::text
  UNION
    SELECT vizinho.id, c.salto + 1
    FROM caminhada c
    JOIN aresta a ON a.origem = c.id OR a.destino = c.id
    JOIN visivel vizinho
      ON vizinho.id = CASE WHEN a.origem = c.id THEN a.destino ELSE a.origem END
    WHERE c.salto < $5::int
)
SELECT v.slug, v.title, v.doc_type, v.access_level, v.updated_at, min(c.salto) AS salto
FROM caminhada c
JOIN visivel v ON v.id = c.id
GROUP BY v.slug, v.title, v.doc_type, v.access_level, v.updated_at
ORDER BY min(c.salto), v.slug
LIMIT $6
"""

# Arestas entre os nos ja escolhidos. `= ANY($5)` em vez de refazer a caminhada:
# o conjunto de nos ja foi cortado pelo teto, e uma aresta para um no que nao
# entrou desenharia uma ponta solta.
_ARESTAS_SQL = f"""
SELECT origem.slug AS origem, alvo.slug AS destino, l.kind, min(l.context) AS contexto,
       count(*) AS peso
FROM wiki_links l
JOIN wiki_documents origem ON origem.id = l.source_document_id
JOIN wiki_documents alvo ON alvo.id = l.target_document_id
WHERE {document_scope_clause("origem", 1)}
  AND {document_scope_clause("alvo", 1)}
  AND origem.slug = ANY($4::text[])
  AND alvo.slug = ANY($4::text[])
GROUP BY origem.slug, alvo.slug, l.kind
ORDER BY origem.slug, alvo.slug
"""

# Fantasmas: citados por um no visivel do recorte e nunca escritos.
_FANTASMAS_SQL = f"""
SELECT origem.slug AS origem, l.target_slug AS destino, l.kind,
       min(l.context) AS contexto, count(*) AS peso
FROM wiki_links l
JOIN wiki_documents origem ON origem.id = l.source_document_id
WHERE l.target_document_id IS NULL
  AND {document_scope_clause("origem", 1)}
  AND origem.slug = ANY($4::text[])
GROUP BY origem.slug, l.target_slug, l.kind
ORDER BY count(*) DESC, l.target_slug
LIMIT $5
"""


class GraphService:
    """A vizinhanca de um documento, sempre sob o escopo de quem pergunta."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def neighborhood(
        self,
        slug: str,
        scope: MemoryScopeFilter,
        *,
        depth: int = 1,
        limit: int = 120,
        include_ghosts: bool = True,
    ) -> dict[str, Any]:
        """Nos e arestas ao redor de `slug`, ate `depth` saltos.

        Devolve `{"origem", "nos", "arestas", "truncado"}`. `origem` e None
        quando o documento nao existe OU o escopo nao deixa ve-lo - a mesma
        resposta para os dois casos, de proposito: distinguir ja entregaria a
        existencia de um documento fora do alcance do papel.
        """
        salto_max = max(0, min(int(depth), MAX_DEPTH))
        escopo = document_scope_args(scope)

        async with self._pool.acquire() as conn:
            nos = await conn.fetch(_VIZINHANCA_SQL, slug, *escopo, salto_max, limit + 1)
            if not nos:
                return {"origem": None, "nos": [], "arestas": [], "truncado": False}

            # O teto foi pedido com +1 so para saber se cortou.
            truncado = len(nos) > limit
            nos = nos[:limit]
            slugs = [r["slug"] for r in nos]

            reais = await conn.fetch(_ARESTAS_SQL, *escopo, slugs)
            arestas = [dict(r) | {"existe": True} for r in reais]
            if include_ghosts:
                fantasmas = await conn.fetch(_FANTASMAS_SQL, *escopo, slugs, limit)
                arestas += [dict(r) | {"existe": False} for r in fantasmas]

        return {
            "origem": slug,
            "nos": [dict(r) for r in nos],
            "arestas": arestas,
            "truncado": truncado,
        }
