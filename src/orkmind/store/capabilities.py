"""Declaracao de capacidades de um backend de storage do OrkMind."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

# "ranked"    : full-text com ranking (pgvector com ts_rank)
# "match"     : casamento por token/substring, sem ranking (Qdrant MatchText)
# "none"      : o backend nao faz busca textual
TextSearchKind = Literal["ranked", "match", "none"]


@dataclass(frozen=True)
class StoreCapabilities:
    """O que este backend sabe fazer. Default otimista = comportamento atual.

    Regra R0.3: toda degradacao passa por aqui. Um campo False obriga a
    camada acima a avisar, nunca a silenciar.
    """

    backend: str = "desconhecido"

    # --- busca ---
    vector_search: bool = True             # ANN nativo sobre vetor externo
    accepts_external_vectors: bool = True  # aceita o vetor que o OrkMind ja gerou
    stores_embedding: bool = True          # guarda o vetor mesmo que nao indexe
    native_text_semantic: bool = False     # implementa search_semantic_text nativamente
    text_search: TextSearchKind = "ranked"
    tag_filter: bool = True                # filtro exato por dimensao de tag

    # --- persistencia e integridade ---
    unique_content_hash: bool = False      # indice unico (collection, content_hash)
    versioning: bool = True                # guarda versoes anteriores
    snapshots: bool = True                 # guarda snapshots logicos
    parent_id: bool = True                 # hierarquia (get_children)
    ttl_gc: bool = True                    # sabe apagar por expires_at
    durable: bool = True                   # sobrevive ao fim do processo

    # --- otimizacoes que a camada pode confiar (nunca politica) ---
    native_acl_filter: bool = False        # aplica o filtro de leitura no proprio motor
    native_constitutional_order: bool = False  # ja devolve mandatory primeiro

    # --- manutencao ---
    backfill: bool = True                  # implementa list_entries_without_embedding/set_embedding

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def avisos(self) -> list[str]:
        """Degradacoes declaradas por este backend, em pt-BR.

        Usado por `orkmind store info` e pelos logs de inicializacao para
        que nenhuma perda de garantia fique invisivel (R0.3).
        """
        avisos: list[str] = []
        if not self.durable:
            avisos.append(
                f"backend '{self.backend}' nao persiste entre processos "
                f"(durable=False); use apenas para teste e desenvolvimento"
            )
        if not self.unique_content_hash:
            avisos.append(
                f"backend '{self.backend}' nao tem indice unico "
                f"(collection, content_hash); a protecao contra duplicata "
                f"concorrente e best-effort e mais fraca"
            )
        if not self.vector_search:
            avisos.append(
                f"backend '{self.backend}' nao faz busca vetorial; a busca "
                f"semantica cai para o lado textual apenas"
            )
        if self.text_search == "none":
            avisos.append(
                f"backend '{self.backend}' nao faz busca textual"
            )
        elif self.text_search == "match":
            avisos.append(
                f"backend '{self.backend}' faz busca textual por casamento "
                f"sem ranking (text_search='match'); a qualidade difere do "
                f"full-text com ts_rank"
            )
        if not self.versioning:
            avisos.append(f"backend '{self.backend}' nao guarda versoes anteriores")
        if not self.snapshots:
            avisos.append(f"backend '{self.backend}' nao guarda snapshots")
        if not self.backfill:
            avisos.append(
                f"backend '{self.backend}' nao suporta backfill de embeddings"
            )
        return avisos
