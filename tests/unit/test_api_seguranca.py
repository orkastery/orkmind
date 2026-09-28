"""Testes de seguranca e idempotencia da API local do OrkMind.

A API e o ponto unico de escrita usado pelo drainer. Ela escuta em
loopback, mas isso nao e postura de seguranca: os testes abaixo fixam
que a escrita exige token, que o hash e conferido pelo servidor e que o
lookup nao vira oraculo de conteudo.

Usam uma SemanticLayer falsa: nenhuma conexao de banco e aberta.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from starlette.testclient import TestClient

from orkmind.api.app import API_TOKEN_ENV, create_app
from orkmind.core.injection import compute_content_hash
from orkmind.core.models import MemoryEntry

TOKEN = "token-de-teste-do-orkmind"


class FakeStore:
    """Store em memoria com a semantica do indice unico parcial."""

    def __init__(self) -> None:
        self.entries: list[MemoryEntry] = []
        self.explode_no_store = False

    async def find_by_content_hash(
        self, content_hash: str, collection: Optional[str] = None
    ) -> Optional[MemoryEntry]:
        for e in self.entries:
            if e.content_hash == content_hash and (
                collection is None or e.collection == collection
            ):
                return e
        return None

    async def close(self) -> None:
        return None


class FakeLayer:
    """SemanticLayer falsa, so com o que a API usa."""

    def __init__(self) -> None:
        self._store = FakeStore()

    @property
    def store(self) -> FakeStore:
        return self._store

    async def add_memory(self, entry: MemoryEntry) -> tuple[str, list[str]]:
        if self._store.explode_no_store:
            raise RuntimeError("duplicate key value violates unique constraint")
        self._store.entries.append(entry)
        return entry.id, []


@pytest.fixture
def layer() -> FakeLayer:
    return FakeLayer()


@pytest.fixture
def client(layer: FakeLayer, monkeypatch: Any) -> TestClient:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    with TestClient(create_app(layer=layer)) as c:  # type: ignore[arg-type]
        yield c


@pytest.fixture
def client_sem_token(layer: FakeLayer, monkeypatch: Any) -> TestClient:
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    with TestClient(create_app(layer=layer)) as c:  # type: ignore[arg-type]
        yield c


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def payload(**over: Any) -> dict[str, Any]:
    base = {"content": "conteudo de teste", "collection": "content", "source": "agent"}
    base.update(over)
    return base


class TestAutenticacao:
    def test_health_e_publico(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_nao_revela_acervo(self, client: TestClient) -> None:
        assert set(client.get("/health").json()) == {"status", "service"}

    def test_post_sem_credencial_e_401(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload())
        assert r.status_code == 401

    def test_post_com_token_errado_e_401(self, client: TestClient) -> None:
        r = client.post(
            "/entries", json=payload(), headers={"Authorization": "Bearer errado"}
        )
        assert r.status_code == 401

    def test_esquema_nao_bearer_e_401(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload(), headers={"Authorization": f"Basic {TOKEN}"})
        assert r.status_code == 401

    def test_lookup_sem_credencial_e_401(self, client: TestClient) -> None:
        r = client.get("/entries/by-hash", params={"hash": "a" * 64})
        assert r.status_code == 401

    def test_sem_token_configurado_falha_fechada(self, client_sem_token: TestClient) -> None:
        """Sem ORKMIND_API_TOKEN a API nega, nunca abre por omissao."""
        r = client_sem_token.post("/entries", json=payload())
        assert r.status_code == 503

    def test_sem_token_nem_com_header_grava(self, client_sem_token: TestClient) -> None:
        r = client_sem_token.post("/entries", json=payload(), headers=auth())
        assert r.status_code == 503

    def test_credencial_invalida_nao_vaza_o_token(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload(), headers={"Authorization": "Bearer x"})
        assert TOKEN not in r.text


class TestSimetriaDeHash:
    def test_hash_divergente_e_recusado(self, client: TestClient) -> None:
        """Impede registrar conteudo sob o hash de outro conteudo.

        Sem esta checagem um cliente poderia ocupar a chave de dedupe de
        um conteudo que ainda nao existe, e o drainer passaria a tratar o
        conteudo legitimo como duplicata, perdendo a gravacao.
        """
        r = client.post(
            "/entries",
            json=payload(content="conteudo A", content_hash=compute_content_hash("conteudo B")),
            headers=auth(),
        )
        assert r.status_code == 400
        assert "divergente" in r.json()["error"]

    def test_hash_correto_e_aceito(self, client: TestClient) -> None:
        conteudo = "conteudo coerente"
        r = client.post(
            "/entries",
            json=payload(content=conteudo, content_hash=compute_content_hash(conteudo)),
            headers=auth(),
        )
        assert r.status_code == 201

    def test_hash_e_opcional(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload(), headers=auth())
        assert r.status_code == 201

    def test_servidor_calcula_o_hash(self, client: TestClient) -> None:
        """O servidor e a autoridade sobre o hash, nao o cliente."""
        conteudo = "quem manda no hash e o conteudo"
        r = client.post("/entries", json=payload(content=conteudo), headers=auth())
        assert r.json()["content_hash"] == compute_content_hash(conteudo)


class TestIdempotencia:
    def test_primeira_gravacao_e_created(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload(), headers=auth())
        assert r.status_code == 201
        assert r.json()["status"] == "created"
        assert r.json()["entry_id"]

    def test_segunda_gravacao_e_duplicate_com_mesmo_id(self, client: TestClient) -> None:
        p = payload(content="conteudo repetido")
        primeiro = client.post("/entries", json=p, headers=auth()).json()
        segundo = client.post("/entries", json=p, headers=auth()).json()

        assert primeiro["status"] == "created"
        assert segundo["status"] == "duplicate"
        assert segundo["entry_id"] == primeiro["entry_id"]

    def test_repeticao_nao_cria_entry_nova(self, client: TestClient, layer: FakeLayer) -> None:
        p = payload(content="conteudo unico")
        for _ in range(4):
            client.post("/entries", json=p, headers=auth())
        assert len(layer.store.entries) == 1

    def test_corrida_no_indice_unico_vira_duplicate(
        self, client: TestClient, layer: FakeLayer
    ) -> None:
        """Se o indice unico barrar o INSERT, respondemos com o id real."""
        existente = MemoryEntry(
            content="disputado", collection="content",
            content_hash=compute_content_hash("disputado"),
        )
        layer.store.entries.append(existente)
        layer.store.explode_no_store = True

        r = client.post("/entries", json=payload(content="disputado"), headers=auth())
        assert r.json()["status"] == "duplicate"
        assert r.json()["entry_id"] == existente.id

    def test_conteudo_diferente_gera_entry_nova(self, client: TestClient) -> None:
        a = client.post("/entries", json=payload(content="A"), headers=auth()).json()
        b = client.post("/entries", json=payload(content="B"), headers=auth()).json()
        assert a["entry_id"] != b["entry_id"]
        assert b["status"] == "created"

    def test_colecoes_distintas_nao_colidem(self, client: TestClient) -> None:
        """A chave de dedupe e (colecao, hash), nao so o hash."""
        client.post("/entries", json=payload(content="X", collection="content"), headers=auth())
        r = client.post("/entries", json=payload(content="X", collection="fact"), headers=auth())
        assert r.json()["status"] == "created"


class TestLookup:
    def test_hash_desconhecido_e_404(self, client: TestClient) -> None:
        r = client.get("/entries/by-hash", params={"hash": "a" * 64}, headers=auth())
        assert r.status_code == 404
        assert r.json()["status"] == "not_found"

    def test_hash_conhecido_devolve_entry_id(self, client: TestClient) -> None:
        conteudo = "conteudo procurado"
        criado = client.post("/entries", json=payload(content=conteudo), headers=auth()).json()
        r = client.get(
            "/entries/by-hash",
            params={"hash": compute_content_hash(conteudo), "collection": "content"},
            headers=auth(),
        )
        assert r.status_code == 200
        assert r.json()["entry_id"] == criado["entry_id"]

    def test_lookup_nao_devolve_conteudo(self, client: TestClient) -> None:
        """O lookup nao pode virar oraculo de leitura do acervo."""
        segredo = "conteudo sensivel que nao deve vazar pelo lookup"
        client.post("/entries", json=payload(content=segredo), headers=auth())
        r = client.get(
            "/entries/by-hash",
            params={"hash": compute_content_hash(segredo)},
            headers=auth(),
        )
        assert segredo not in r.text
        assert "content" not in r.json()

    def test_hash_obrigatorio(self, client: TestClient) -> None:
        assert client.get("/entries/by-hash", headers=auth()).status_code == 400

    def test_colecao_invalida_e_400(self, client: TestClient) -> None:
        r = client.get(
            "/entries/by-hash",
            params={"hash": "a" * 64, "collection": "inexistente"},
            headers=auth(),
        )
        assert r.status_code == 400


class TestGovernanca:
    def test_agente_nao_cria_mandatory(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload(mandatory=True), headers=auth())
        assert r.status_code == 403

    def test_agente_nao_cria_critical(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload(priority="critical"), headers=auth())
        assert r.status_code == 403

    def test_humano_pode_criar_mandatory(self, client: TestClient) -> None:
        r = client.post(
            "/entries", json=payload(source="human", mandatory=True), headers=auth()
        )
        assert r.status_code == 201


class TestValidacao:
    def test_conteudo_vazio_e_400(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload(content="  "), headers=auth())
        assert r.status_code == 400

    def test_colecao_invalida_e_400(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload(collection="inexistente"), headers=auth())
        assert r.status_code == 400

    def test_prioridade_invalida_e_400(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload(priority="urgentissimo"), headers=auth())
        assert r.status_code == 400

    def test_tags_mal_formadas_e_400(self, client: TestClient) -> None:
        r = client.post("/entries", json=payload(tags={"project": "nao-e-lista"}), headers=auth())
        assert r.status_code == 400

    def test_corpo_nao_json_e_400(self, client: TestClient) -> None:
        r = client.post(
            "/entries", content=b"nao e json",
            headers={**auth(), "Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_corpo_lista_e_400(self, client: TestClient) -> None:
        assert client.post("/entries", json=[1, 2], headers=auth()).status_code == 400
