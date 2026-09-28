"""Funcoes puras de governanca (F3.4).

Sem banco, sem servico, sem async. Estas sao as decisoes de politica do
produto: se elas estiverem certas aqui, estao certas em todos os
backends, porque so existe um lugar onde sao aplicadas.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orkmind.core.models import MemoryEntry
from orkmind.core.ontology import ProtectionError
from orkmind.store.governance import (
    assert_nao_protegida,
    assert_pode_escrever,
    e_protegida,
    filtrar_ativas,
    filtrar_sem_injection,
    filtrar_visiveis,
    limite_de_candidatos,
    ordenar_constitucional,
    particionar_mandatory_primeiro,
    pode_ler,
)


def entrada(**kwargs) -> MemoryEntry:
    base = {"content": "conteudo", "collection": "fact"}
    base.update(kwargs)
    return MemoryEntry(**base)  # type: ignore[arg-type]


class TestProtecaoD2:
    """I1: agente nunca altera nem apaga entry protegida."""

    def test_protected_true_e_protegida(self) -> None:
        assert e_protegida(entrada(protected=True)) is True

    def test_priority_critical_e_protegida(self) -> None:
        assert e_protegida(entrada(priority="critical")) is True

    def test_entry_comum_nao_e_protegida(self) -> None:
        assert e_protegida(entrada()) is False

    @pytest.mark.parametrize("operacao", ["update", "delete"])
    def test_agente_e_barrado(self, operacao: str) -> None:
        with pytest.raises(ProtectionError, match="protegida"):
            assert_nao_protegida(entrada(protected=True), "agent", operacao)  # type: ignore[arg-type]

    @pytest.mark.parametrize("source", ["human", "system", "bootstrap"])
    def test_nao_agente_passa(self, source: str) -> None:
        assert_nao_protegida(entrada(protected=True), source, "update")

    def test_entry_comum_aceita_agente(self) -> None:
        assert_nao_protegida(entrada(), "agent", "update")

    def test_mensagem_de_update_e_a_canonica(self) -> None:
        from orkmind.core.ontology import PROTECTION_MSG_UPDATE

        e = entrada(protected=True, id="abc")
        with pytest.raises(ProtectionError) as exc:
            assert_nao_protegida(e, "agent", "update")
        assert str(exc.value) == PROTECTION_MSG_UPDATE.format(id="abc")

    def test_mensagem_de_delete_e_a_canonica(self) -> None:
        from orkmind.core.ontology import PROTECTION_MSG_DELETE

        e = entrada(priority="critical", id="xyz")
        with pytest.raises(ProtectionError) as exc:
            assert_nao_protegida(e, "agent", "delete")
        assert str(exc.value) == PROTECTION_MSG_DELETE.format(id="xyz")


class TestPodeLer:
    """Tabela-verdade completa de I3."""

    def test_sem_requester_ve_tudo(self) -> None:
        assert pode_ler(entrada(visibility="private", author_id="alice"), None) is True

    def test_publica_e_visivel_para_qualquer_um(self) -> None:
        assert pode_ler(entrada(visibility="public"), "bob") is True

    def test_privada_e_visivel_para_o_autor(self) -> None:
        e = entrada(visibility="private", author_id="alice")
        assert pode_ler(e, "alice") is True

    def test_privada_nao_e_visivel_para_estranho(self) -> None:
        e = entrada(visibility="private", author_id="alice")
        assert pode_ler(e, "bob") is False

    def test_restrita_e_visivel_para_quem_esta_na_audience(self) -> None:
        e = entrada(visibility="restricted", author_id="alice",
                    tags={"audience": ["bob"]})
        assert pode_ler(e, "bob") is True

    def test_restrita_nao_e_visivel_para_quem_esta_fora(self) -> None:
        e = entrada(visibility="restricted", author_id="alice",
                    tags={"audience": ["bob"]})
        assert pode_ler(e, "carol") is False

    def test_restrita_e_visivel_por_grupo(self) -> None:
        e = entrada(visibility="restricted", author_id="alice",
                    tags={"audience": ["equipe"]})
        assert pode_ler(e, "bob", ["bob", "equipe"]) is True

    def test_privada_e_visivel_quando_o_autor_e_um_grupo_do_requester(self) -> None:
        e = entrada(visibility="private", author_id="equipe")
        assert pode_ler(e, "bob", ["bob", "equipe"]) is True

    def test_privada_sem_autor_nao_vaza(self) -> None:
        assert pode_ler(entrada(visibility="private", author_id=None), "bob") is False

    def test_espelha_o_can_read_do_adapter(self) -> None:
        """A funcao promovida e o gate de equivalencia com o SQL (DP-4)."""
        from orkmind.store.postgres_adapter import PostgresAdapter

        adapter = PostgresAdapter("postgresql://localhost/nunca-conecta")
        casos = [
            entrada(visibility="public"),
            entrada(visibility="private", author_id="alice"),
            entrada(visibility="restricted", author_id="alice",
                    tags={"audience": ["bob"]}),
            entrada(visibility="restricted", author_id="alice",
                    tags={"audience": ["equipe"]}),
        ]
        for e in casos:
            for requester, identidades in [
                (None, None), ("alice", ["alice"]), ("bob", ["bob"]),
                ("bob", ["bob", "equipe"]),
            ]:
                assert pode_ler(e, requester, identidades) == adapter.can_read(
                    e, requester, identidades
                )


class TestPodeEscrever:
    def test_sem_requester_preserva_o_comportamento_antigo(self) -> None:
        assert_pode_escrever(entrada(author_id="alice"), None)

    def test_autor_pode(self) -> None:
        assert_pode_escrever(entrada(author_id="alice"), "alice", ["alice"])

    def test_estranho_nao_pode(self) -> None:
        with pytest.raises(PermissionError, match="permissao de escrita"):
            assert_pode_escrever(entrada(author_id="alice"), "bob", ["bob"])

    def test_editor_pode(self) -> None:
        e = entrada(author_id="alice", tags={"editors": ["bob"]})
        assert_pode_escrever(e, "bob", ["bob"])

    def test_coringa_libera(self) -> None:
        e = entrada(author_id="alice", tags={"editors": ["*"]})
        assert_pode_escrever(e, "quem-quer-que-seja", ["quem-quer-que-seja"])

    def test_grupo_em_editors_libera(self) -> None:
        e = entrada(author_id="alice", tags={"editors": ["equipe"]})
        assert_pode_escrever(e, "bob", ["bob", "equipe"])

    def test_i2_protecao_prevalece_sobre_editors(self) -> None:
        """A protecao D2 e avaliada ANTES e nao e vencida por editors."""
        e = entrada(protected=True, author_id="alice", tags={"editors": ["*"]})
        with pytest.raises(ProtectionError):
            assert_nao_protegida(e, "agent", "update")
        # e, se o chamador inverter a ordem, a escrita seria liberada:
        assert_pode_escrever(e, "bob", ["bob"])


class TestFiltros:
    def test_i4_expirada_sai(self) -> None:
        agora = datetime.now(timezone.utc)
        viva = entrada(id="viva")
        expirada = entrada(id="expirada", expires_at=agora - timedelta(hours=1))
        futura = entrada(id="futura", expires_at=agora + timedelta(hours=1))
        restantes = filtrar_ativas([viva, expirada, futura])
        assert [e.id for e in restantes] == ["viva", "futura"]

    def test_expiracao_ingenua_e_tratada_como_utc(self) -> None:
        passado = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        assert filtrar_ativas([entrada(expires_at=passado)]) == []

    def test_i5_injection_risk_sai(self) -> None:
        limpa = entrada(id="limpa")
        suja = entrada(id="suja", injection_risk=True)
        assert [e.id for e in filtrar_sem_injection([limpa, suja])] == ["limpa"]

    def test_filtrar_visiveis_sem_requester_nao_mexe(self) -> None:
        entries = [entrada(visibility="private", author_id="alice")]
        assert filtrar_visiveis(entries, None) == entries

    def test_filtrar_visiveis_aplica_pode_ler(self) -> None:
        publica = entrada(id="pub", visibility="public")
        privada = entrada(id="priv", visibility="private", author_id="alice")
        visiveis = filtrar_visiveis([publica, privada], "bob", ["bob"])
        assert [e.id for e in visiveis] == ["pub"]


class TestOrdenacaoConstitucional:
    """I9: ordem TOTAL e identica entre backends."""

    def test_mandatory_vem_primeiro(self) -> None:
        comum = entrada(id="b", priority="critical")
        obrigatoria = entrada(id="a", mandatory=True, priority="low")
        assert [e.id for e in ordenar_constitucional([comum, obrigatoria])] == ["a", "b"]

    def test_prioridade_ordena_dentro_do_grupo(self) -> None:
        entries = [
            entrada(id="baixa", priority="low"),
            entrada(id="critica", priority="critical"),
            entrada(id="media", priority="medium"),
            entrada(id="alta", priority="high"),
        ]
        ids = [e.id for e in ordenar_constitucional(entries)]
        assert ids == ["critica", "alta", "media", "baixa"]

    def test_updated_at_mais_recente_primeiro(self) -> None:
        agora = datetime.now(timezone.utc)
        velha = entrada(id="velha", updated_at=agora - timedelta(days=1))
        nova = entrada(id="nova", updated_at=agora)
        assert [e.id for e in ordenar_constitucional([velha, nova])] == ["nova", "velha"]

    def test_desempate_final_por_id(self) -> None:
        """DD-3: o desempate por id e o que torna a ordem TOTAL."""
        instante = datetime.now(timezone.utc)
        entries = [
            entrada(id="zzz", updated_at=instante),
            entrada(id="aaa", updated_at=instante),
            entrada(id="mmm", updated_at=instante),
        ]
        assert [e.id for e in ordenar_constitucional(entries)] == ["aaa", "mmm", "zzz"]

    def test_ordem_e_estavel_entre_chamadas(self) -> None:
        instante = datetime.now(timezone.utc)
        entries = [entrada(id=f"id-{i}", updated_at=instante) for i in range(10)]
        primeira = [e.id for e in ordenar_constitucional(entries)]
        segunda = [e.id for e in ordenar_constitucional(list(reversed(entries)))]
        assert primeira == segunda

    def test_nao_muta_a_lista_de_entrada(self) -> None:
        entries = [entrada(id="b"), entrada(id="a", mandatory=True)]
        ordenar_constitucional(entries)
        assert [e.id for e in entries] == ["b", "a"]


class TestParticaoEstavel:
    """DD-4: mandatory na frente, relevancia preservada dentro do grupo."""

    def test_mandatory_na_frente(self) -> None:
        entries = [
            entrada(id="r1"),
            entrada(id="m1", mandatory=True),
            entrada(id="r2"),
            entrada(id="m2", mandatory=True),
        ]
        ids = [e.id for e in particionar_mandatory_primeiro(entries)]
        assert ids == ["m1", "m2", "r1", "r2"]

    def test_preserva_a_ordem_de_relevancia_do_backend(self) -> None:
        """A camada nao reordena por prioridade aqui: nao conhece o score."""
        entries = [
            entrada(id="terceira", priority="critical"),
            entrada(id="primeira", priority="low"),
            entrada(id="segunda", priority="high"),
        ]
        ids = [e.id for e in particionar_mandatory_primeiro(entries)]
        assert ids == ["terceira", "primeira", "segunda"]

    def test_lista_vazia(self) -> None:
        assert particionar_mandatory_primeiro([]) == []


class TestLimiteDeCandidatos:
    """DD-5: over-fetch declarado apenas para quem precisa."""

    def test_backend_nativo_nao_faz_overfetch(self) -> None:
        """Garantia mecanica de nao-regressao do pgvector (DP-9)."""
        assert limite_de_candidatos(
            50, ordena_nativamente=True, filtra_acl_nativamente=True,
            tem_requester=True,
        ) == 50

    def test_backend_sem_ordenacao_pede_mais(self) -> None:
        assert limite_de_candidatos(
            10, ordena_nativamente=False, filtra_acl_nativamente=True,
            tem_requester=False,
        ) == 60

    def test_multiplica_por_quatro_quando_compensa(self) -> None:
        assert limite_de_candidatos(
            100, ordena_nativamente=False, filtra_acl_nativamente=False,
            tem_requester=True,
        ) == 400

    def test_respeita_o_teto(self) -> None:
        assert limite_de_candidatos(
            200, ordena_nativamente=False, filtra_acl_nativamente=True,
            tem_requester=False, teto=500,
        ) == 500

    def test_teto_nunca_corta_abaixo_do_limit_pedido(self) -> None:
        assert limite_de_candidatos(
            900, ordena_nativamente=False, filtra_acl_nativamente=True,
            tem_requester=False, teto=500,
        ) == 900

    def test_sem_requester_a_acl_nao_motiva_overfetch(self) -> None:
        assert limite_de_candidatos(
            30, ordena_nativamente=True, filtra_acl_nativamente=False,
            tem_requester=False,
        ) == 30

    def test_com_requester_a_acl_motiva_overfetch(self) -> None:
        assert limite_de_candidatos(
            30, ordena_nativamente=True, filtra_acl_nativamente=False,
            tem_requester=True,
        ) == 120
