"""Equivalencia de governanca entre backends (prova 8.2 do plano).

Este e o teste que protege o produto. Um corpus fixo (ids fixos, datas
fixas, perfis fixos) e carregado em TODOS os backends disponiveis na
maquina. As mesmas consultas, com o mesmo `requester_id`, rodam em todos,
e o resultado tem que coincidir.

A pergunta que ele mantem respondida com "sim": se eu marcar uma regra
como `mandatory` e `protected`, ela entra no contexto do agente e um
agente nao consegue apaga-la - **em qualquer backend**.

Quando so um backend esta disponivel, o teste pula com aviso ruidoso. Ele
nao pode ser dado como aprovado por vacuidade.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio

from orkmind.core.injection import compute_content_hash
from orkmind.core.models import MemoryEntry, Profile
from orkmind.core.ontology import ProtectionError
from orkmind.store.base import MemoryStore
from tests.contract.backends import backends_disponiveis, obter_backend

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# Termo distintivo e sem sufixo, para que o casamento textual coincida
# entre o full-text com stemming do pgvector e o casamento de token dos
# demais backends.
TERMO = "kubernetes"

GRUPO = Profile(
    id="equipe-infra",
    display_name="Equipe Infra",
    profile_type="group",
    metadata={"members": ["bob"]},
    created_at=BASE,
)


def _entry(
    ident: str,
    conteudo: str,
    minutos: int,
    **kwargs,
) -> MemoryEntry:
    base = {
        "id": ident,
        "content": conteudo,
        "collection": "fact",
        "created_at": BASE + timedelta(minutes=minutos),
        "updated_at": BASE + timedelta(minutes=minutos),
        "content_hash": compute_content_hash(f"{ident}:{conteudo}"),
        "tags": {"skill": ["equivalencia"]},
        "embedding": [0.1 * (minutos + 1)] + [0.0] * 1023,
    }
    base.update(kwargs)
    return MemoryEntry(**base)  # type: ignore[arg-type]


def corpus() -> list[MemoryEntry]:
    """Corpus fixo. Nada de aleatorio, nada de `now()`."""
    return [
        _entry(
            "eq-01-mandatory-critical",
            f"regra obrigatoria de {TERMO} em producao",
            0,
            collection="rule",
            mandatory=True,
            priority="critical",
            protected=True,
            visibility="public",
            author_id="alice",
        ),
        _entry(
            "eq-02-mandatory-low",
            f"segunda regra obrigatoria de {TERMO}",
            1,
            collection="rule",
            mandatory=True,
            priority="low",
            visibility="public",
            author_id="alice",
        ),
        _entry(
            "eq-03-high-publica",
            f"fato importante sobre {TERMO}",
            2,
            priority="high",
            visibility="public",
            author_id="alice",
        ),
        _entry(
            "eq-04-medium-privada-alice",
            f"nota privada da alice sobre {TERMO}",
            3,
            priority="medium",
            visibility="private",
            author_id="alice",
        ),
        _entry(
            "eq-05-low-restrita-grupo",
            f"nota restrita ao grupo sobre {TERMO}",
            4,
            priority="low",
            visibility="restricted",
            author_id="alice",
            tags={"skill": ["equivalencia"], "audience": ["equipe-infra"]},
        ),
        _entry(
            "eq-06-expirada",
            f"nota expirada sobre {TERMO}",
            5,
            visibility="public",
            author_id="alice",
            expires_at=BASE - timedelta(days=1),
        ),
        _entry(
            "eq-07-injection",
            f"conteudo suspeito sobre {TERMO}",
            6,
            visibility="public",
            author_id="alice",
            injection_risk=True,
        ),
        # Mesmo instante de eq-03, para exercitar o desempate por id (DD-3).
        _entry(
            "eq-08-high-empate",
            f"outro fato importante sobre {TERMO}",
            2,
            priority="high",
            visibility="public",
            author_id="alice",
        ),
    ]


REQUESTERS = [None, "alice", "bob", "carol"]


@pytest_asyncio.fixture
async def backends() -> AsyncIterator[dict[str, MemoryStore]]:
    """Todos os backends disponiveis, com o mesmo corpus carregado."""
    nomes = backends_disponiveis()
    if len(nomes) < 2:
        pytest.skip(
            "PROVA 8.2 PENDENTE: so ha o backend "
            f"{nomes or ['nenhum']} disponivel nesta maquina, e a equivalencia "
            "entre backends nao pode ser dada como aprovada por vacuidade. "
            "Defina ORKMIND_TEST_DATABASE_URL e/ou ORKMIND_TEST_QDRANT_URL "
            "com ORKMIND_TEST_QDRANT_PREFIX."
        )

    stores: dict[str, MemoryStore] = {}
    registros = []
    for nome in nomes:
        backend = obter_backend(nome)
        store = backend.construir()
        await store.initialize()
        backend.limpar(store)
        if nome == "qdrant":
            await store.initialize()
        await store.profile_create(GRUPO)
        for entry in corpus():
            await store.store(entry.model_copy(deep=True))
        stores[nome] = store
        registros.append((backend, store))
    try:
        yield stores
    finally:
        for backend, store in registros:
            backend.limpar(store)
            await store.close()


def _ids(entries: list[MemoryEntry]) -> list[str]:
    return [e.id for e in entries]


class TestA1OrdemDeBuscaPorTags:
    """A sequencia de ids de `search_by_tags` e identica entre backends."""

    @pytest.mark.asyncio
    async def test_sequencia_identica(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        resultados = {}
        for nome, store in backends.items():
            achadas = await store.search_by_tags(tags={"skill": ["equivalencia"]})
            resultados[nome] = _ids(achadas)

        referencia_nome, referencia = next(iter(resultados.items()))
        for nome, ids in resultados.items():
            assert ids == referencia, (
                f"ordem divergente entre '{referencia_nome}' e '{nome}':\n"
                f"  {referencia_nome}: {referencia}\n"
                f"  {nome}: {ids}"
            )

    @pytest.mark.asyncio
    async def test_ordem_e_a_constitucional_esperada(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        """Mandatory, depois prioridade, depois recencia, depois id."""
        esperada = [
            "eq-01-mandatory-critical",
            "eq-02-mandatory-low",
            "eq-03-high-publica",
            "eq-08-high-empate",
            "eq-04-medium-privada-alice",
            "eq-05-low-restrita-grupo",
        ]
        for nome, store in backends.items():
            achadas = await store.search_by_tags(tags={"skill": ["equivalencia"]})
            assert _ids(achadas) == esperada, f"backend {nome}"


class TestA2BuscaPorRelevancia:
    """Conjunto identico, e `mandatory` sempre antes de nao-`mandatory`."""

    @pytest.mark.asyncio
    async def test_busca_textual_devolve_o_mesmo_conjunto(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        conjuntos = {}
        for nome, store in backends.items():
            achadas = await store.search_by_text(TERMO, limit=50)
            conjuntos[nome] = set(_ids(achadas))
        referencia_nome, referencia = next(iter(conjuntos.items()))
        for nome, ids in conjuntos.items():
            assert ids == referencia, (
                f"conjunto divergente entre '{referencia_nome}' e '{nome}': "
                f"so em {referencia_nome}={referencia - ids}, "
                f"so em {nome}={ids - referencia}"
            )

    @pytest.mark.asyncio
    async def test_busca_textual_poe_mandatory_na_frente(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        for nome, store in backends.items():
            achadas = await store.search_by_text(TERMO, limit=50)
            visto_comum = False
            for entry in achadas:
                if not entry.mandatory:
                    visto_comum = True
                elif visto_comum:
                    pytest.fail(
                        f"backend {nome}: entry mandatory '{entry.id}' apareceu "
                        f"depois de uma nao-mandatory"
                    )

    @pytest.mark.asyncio
    async def test_busca_semantica_devolve_o_mesmo_conjunto(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        consulta = [0.1] + [0.0] * 1023
        conjuntos = {}
        for nome, store in backends.items():
            achadas = await store.search_semantic(consulta, limit=50)
            conjuntos[nome] = set(_ids(achadas))
        referencia_nome, referencia = next(iter(conjuntos.items()))
        for nome, ids in conjuntos.items():
            assert ids == referencia, (
                f"conjunto divergente entre '{referencia_nome}' e '{nome}': "
                f"so em {referencia_nome}={referencia - ids}, "
                f"so em {nome}={ids - referencia}"
            )

    @pytest.mark.asyncio
    async def test_busca_semantica_poe_mandatory_na_frente(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        consulta = [0.1] + [0.0] * 1023
        for nome, store in backends.items():
            achadas = await store.search_semantic(consulta, limit=50)
            visto_comum = False
            for entry in achadas:
                if not entry.mandatory:
                    visto_comum = True
                elif visto_comum:
                    pytest.fail(f"backend {nome}: mandatory depois de comum")


class TestA3NenhumaMandatoryFalta:
    """A afirmacao mais importante do teste."""

    MANDATORY = {"eq-01-mandatory-critical", "eq-02-mandatory-low"}

    @pytest.mark.asyncio
    async def test_busca_por_tags(self, backends: dict[str, MemoryStore]) -> None:
        for nome, store in backends.items():
            achadas = set(_ids(await store.search_by_tags(
                tags={"skill": ["equivalencia"]}
            )))
            faltando = self.MANDATORY - achadas
            assert not faltando, f"backend {nome}: mandatory ausentes {faltando}"

    @pytest.mark.asyncio
    async def test_busca_textual(self, backends: dict[str, MemoryStore]) -> None:
        for nome, store in backends.items():
            achadas = set(_ids(await store.search_by_text(TERMO, limit=50)))
            faltando = self.MANDATORY - achadas
            assert not faltando, f"backend {nome}: mandatory ausentes {faltando}"

    @pytest.mark.asyncio
    async def test_sobrevive_ao_limite_apertado(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        """I6: com limit=2, as duas que sobram tem que ser as mandatory."""
        for nome, store in backends.items():
            achadas = set(_ids(await store.search_by_tags(
                tags={"skill": ["equivalencia"]}, limit=2
            )))
            assert achadas == self.MANDATORY, f"backend {nome}: {achadas}"


class TestA4AclProduzOMesmoConjunto:
    ESPERADO = {
        None: {
            "eq-01-mandatory-critical", "eq-02-mandatory-low",
            "eq-03-high-publica", "eq-04-medium-privada-alice",
            "eq-05-low-restrita-grupo", "eq-08-high-empate",
        },
        "alice": {
            "eq-01-mandatory-critical", "eq-02-mandatory-low",
            "eq-03-high-publica", "eq-04-medium-privada-alice",
            "eq-05-low-restrita-grupo", "eq-08-high-empate",
        },
        "bob": {
            "eq-01-mandatory-critical", "eq-02-mandatory-low",
            "eq-03-high-publica", "eq-05-low-restrita-grupo",
            "eq-08-high-empate",
        },
        "carol": {
            "eq-01-mandatory-critical", "eq-02-mandatory-low",
            "eq-03-high-publica", "eq-08-high-empate",
        },
    }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("requester", REQUESTERS)
    async def test_mesmo_conjunto_visivel_em_todos(
        self, backends: dict[str, MemoryStore], requester: str | None
    ) -> None:
        for nome, store in backends.items():
            achadas = set(_ids(await store.search_by_tags(
                tags={"skill": ["equivalencia"]}, requester_id=requester
            )))
            assert achadas == self.ESPERADO[requester], (
                f"backend {nome}, requester {requester}: "
                f"faltando={self.ESPERADO[requester] - achadas}, "
                f"sobrando={achadas - self.ESPERADO[requester]}"
            )


class TestA5ProtecaoTemAMesmaMensagem:
    @pytest.mark.asyncio
    async def test_update_por_agente(self, backends: dict[str, MemoryStore]) -> None:
        mensagens = {}
        for nome, store in backends.items():
            with pytest.raises(ProtectionError) as exc:
                await store.update(
                    "eq-01-mandatory-critical",
                    MemoryEntry(content="tentativa", collection="rule", source="agent"),
                )
            mensagens[nome] = str(exc.value)
        assert len(set(mensagens.values())) == 1, mensagens

    @pytest.mark.asyncio
    async def test_delete_por_agente(self, backends: dict[str, MemoryStore]) -> None:
        mensagens = {}
        for nome, store in backends.items():
            with pytest.raises(ProtectionError) as exc:
                await store.delete("eq-01-mandatory-critical", source="agent")
            mensagens[nome] = str(exc.value)
        assert len(set(mensagens.values())) == 1, mensagens

    @pytest.mark.asyncio
    async def test_entry_protegida_continua_la(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        for nome, store in backends.items():
            with pytest.raises(ProtectionError):
                await store.delete("eq-01-mandatory-critical", source="agent")
            assert await store.retrieve("eq-01-mandatory-critical") is not None, nome


class TestA6VersionamentoEquivalente:
    @pytest.mark.asyncio
    async def test_update_incrementa_uma_vez_e_arquiva_uma_versao(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        for nome, store in backends.items():
            alvo = "eq-03-high-publica"
            antes = await store.retrieve(alvo)
            assert antes is not None
            await store.update(
                alvo,
                MemoryEntry(
                    content=f"fato revisado sobre {TERMO}",
                    collection="fact",
                    source="human",
                ),
            )
            depois = await store.retrieve(alvo)
            assert depois is not None
            assert depois.version == antes.version + 1, nome
            historico = await store.get_history(alvo)
            assert len(historico) == 1, f"backend {nome}: {len(historico)} versoes"
            assert historico[0].version == antes.version, nome


class TestA7EmpateNaFronteiraDeTruncagem:
    """I9 no ponto onde ele quase se perdeu (achado do F4).

    Quando varias entries empatam em `mandatory`, `priority` e
    `updated_at`, so o desempate por id (DD-3) define quem fica. O
    pgvector declara `native_constitutional_order=True`, entao a camada
    de governanca NAO faz over-fetch: o `LIMIT` do SQL e quem corta. Se o
    `ORDER BY` do SQL nao for uma ordem TOTAL, o corte escolhe um
    subconjunto arbitrario e o resultado diverge dos backends que
    ordenam na camada.

    Este teste roda com o limite CORTANDO em cima do empate, que e
    exatamente o cenario que o corpus principal nao alcanca.
    """

    TAG = {"skill": ["empate-fronteira"]}

    @staticmethod
    def _empatadas() -> list[MemoryEntry]:
        """Ordem de insercao ao contrario da ordem por id, de proposito."""
        return [
            _entry(
                f"emp-{sufixo}",
                f"entry empatada {sufixo}",
                0,
                priority="high",
                visibility="public",
                author_id="alice",
                tags={"skill": ["empate-fronteira"]},
            )
            for sufixo in ("04", "03", "02", "01")
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limite", [1, 2, 3, 4])
    async def test_mesmo_subconjunto_em_todos_os_backends(
        self, backends: dict[str, MemoryStore], limite: int
    ) -> None:
        for store in backends.values():
            for entry in self._empatadas():
                await store.store(entry.model_copy(deep=True))

        resultados = {}
        for nome, store in backends.items():
            achadas = await store.search_by_tags(tags=self.TAG, limit=limite)
            resultados[nome] = _ids(achadas)

        esperado = [f"emp-{s}" for s in ("01", "02", "03", "04")][:limite]
        for nome, ids in resultados.items():
            assert ids == esperado, (
                f"backend {nome} com limit={limite} devolveu {ids}, esperado "
                f"{esperado}. Em empate total so o id desempata (DD-3): o "
                f"ORDER BY do backend precisa ser uma ordem TOTAL, senao o "
                f"corte no LIMIT escolhe um subconjunto arbitrario."
            )


class TestA8MandatorySobreviveAJanelaDeCandidatos:
    """I6 sob volume: achado do F4, o mais grave do ciclo.

    Backends sem `native_constitutional_order` recebem uma janela de
    candidatos (`limite_de_candidatos`, DD-5) e devolvem essa janela na
    ordem NEUTRA deles (`updated_at DESC, id ASC`). Quando ha mais
    entries casando a consulta do que cabe na janela, uma regra
    obrigatoria ANTIGA cai fora dela e nunca chega a camada que ordena.

    O contrato de `search_by_tags` em `store/base.py` diz que entries
    `mandatory` que casam as tags sao SEMPRE incluidas. Este teste poe a
    unica `mandatory` como a entry MAIS ANTIGA do corpus, que e o pior
    caso possivel para a janela.

    O corpus principal tem 8 entries e nunca alcanca esse regime.
    """

    VOLUME = 120  # acima da janela de 60 candidatos de um limit=10
    TAG = {"skill": ["volume-mandatory"]}
    ALVO = "aaa-regra-mais-antiga"

    @classmethod
    def _corpus_de_volume(cls) -> list[MemoryEntry]:
        entries = [
            _entry(
                f"vol-{i:04d}",
                f"entry de volume numero {i}",
                10 + i,
                visibility="public",
                author_id="alice",
                tags={"skill": ["volume-mandatory"]},
            )
            for i in range(cls.VOLUME)
        ]
        entries.append(
            _entry(
                cls.ALVO,
                "regra obrigatoria antiga que nao pode sumir",
                -10_000,
                collection="rule",
                mandatory=True,
                priority="critical",
                visibility="public",
                author_id="alice",
                tags={"skill": ["volume-mandatory"]},
            )
        )
        return entries

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limite", [5, 10])
    async def test_mandatory_antiga_sobrevive_em_todos_os_backends(
        self, backends: dict[str, MemoryStore], limite: int
    ) -> None:
        for store in backends.values():
            for entry in self._corpus_de_volume():
                await store.store(entry.model_copy(deep=True))

        for nome, store in backends.items():
            achadas = await store.search_by_tags(tags=self.TAG, limit=limite)
            ids = _ids(achadas)
            assert self.ALVO in ids, (
                f"backend {nome} com limit={limite} perdeu a entry mandatory "
                f"'{self.ALVO}' entre {self.VOLUME + 1} entries que casam a "
                f"consulta. Devolveu: {ids}. A janela de candidatos nao pode "
                f"engolir uma regra obrigatoria (I6)."
            )
            assert ids[0] == self.ALVO, (
                f"backend {nome}: a mandatory critical tem que vir primeiro, "
                f"veio {ids[0]}"
            )


class TestA9IdentidadeNaoEForjavelPelaApiDeMemoria:
    """Achado de seguranca do F4, ainda ABERTO. Nao apague este teste.

    Em backends que usam `EntryProfileStore` (DD-6: tudo que nao e
    `pgvector`), as identidades sao entries comuns da colecao `users`,
    com `visibility="public"` e sem `protected`. A API normal de memoria
    alcanca essas entries, entao um agente consegue:

      A. criar um "grupo" novo com ele proprio na lista de membros;
      B. dar `update` no grupo real e se incluir;
      C. dar `delete` no grupo real e derrubar a ACL de todo mundo.

    `resolve_identities` le exatamente esse campo, entao A e B viram
    leitura de entries `visibility="restricted"` daquele grupo. Em
    `pgvector` o caminho nao existe: os perfis vivem na tabela
    `profiles`, fora do namespace de entries.

    O teste abaixo descreve o comportamento CORRETO e esta marcado
    `xfail(strict=True)`: ele falha hoje de proposito. No dia em que a
    correcao entrar, ele passa, o `strict` derruba a suite e obriga a
    remover o marcador. E o mesmo mecanismo que o F3 usou para subir a
    governanca sem deixar buraco silencioso.
    """

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "ACHADO F4 (CRITICO, ABERTO): em backends com EntryProfileStore "
            "a identidade e forjavel pela API de memoria (achado S1 da "
            "auditoria de seguranca do F4)."
        ),
    )
    async def test_agente_nao_forja_pertencimento_a_grupo(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        segredo = _entry(
            "sec-restrita-ao-grupo",
            "conteudo restrito a equipe-infra",
            20,
            visibility="restricted",
            author_id="alice",
            tags={"skill": ["forja"], "audience": ["equipe-infra"]},
        )
        forjado = MemoryEntry(
            id="grupo-forjado",
            content="Grupo forjado por um agente",
            collection="users",
            source="agent",
            metadata={
                "orkmind_profile": True,
                "profile_type": "group",
                "profile_metadata": {"members": ["mallory"]},
                "members": ["mallory"],
            },
        )
        for nome, store in backends.items():
            await store.store(segredo.model_copy(deep=True))
            await store.store(forjado.model_copy(deep=True))

            identidades = await store.resolve_identities("mallory")
            assert identidades == ["mallory"], (
                f"backend {nome}: um agente forjou a identidade "
                f"{set(identidades) - {'mallory'}} gravando uma entry na "
                f"colecao 'users' pela API normal de memoria"
            )

            visiveis = _ids(
                await store.search_by_tags(
                    tags={"skill": ["forja"]}, requester_id="mallory"
                )
            )
            assert "sec-restrita-ao-grupo" not in visiveis, (
                f"backend {nome}: escalada de privilegio confirmada, mallory "
                f"passou a ler entry restrita a um grupo do qual nao e membro"
            )


class TestCoberturaDaProva:
    def test_relata_quais_backends_participaram(
        self, backends: dict[str, MemoryStore]
    ) -> None:
        """Deixa no relatorio quais backends foram comparados de fato."""
        assert len(backends) >= 2
        print(f"\nPROVA 8.2 comparou: {', '.join(sorted(backends))}")
