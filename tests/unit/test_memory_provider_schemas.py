"""Modelos Pydantic v2, RBAC e SQL de escopo do Memory Provider nativo."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

import orkmind
from orkmind.memory_provider.config import MemoryProviderSettings
from orkmind.memory_provider.db.migrations import render_schema
from orkmind.memory_provider.embeddings import HashingEmbeddingProvider
from orkmind.memory_provider.models.schemas import (
    AccessLevel,
    DocumentIngestRequest,
    ExecutiveScopeGuard,
    MemoryScopeFilter,
    UserContext,
    content_sha256,
    readable_levels,
)
from orkmind.memory_provider.services.retrieval import quote_lexeme, select_candidate_lexemes
from orkmind.memory_provider.services.scope import document_scope_args, document_scope_clause

OPERACIONAL = UserContext(user_id="ana", role=AccessLevel.OPERATIONAL, departments=["noc"])
EXECUTIVA = UserContext(user_id="dora", role=AccessLevel.EXECUTIVE, departments=["financeiro"])


def _request(**overrides: object) -> DocumentIngestRequest:
    base: dict[str, object] = {
        "slug": "playbook-sdlc",
        "title": "Playbook SDLC",
        "doc_type": "playbook",
        "raw_content": "# Playbook\n\nConteudo.",
        "access_level": AccessLevel.OPERATIONAL,
    }
    base.update(overrides)
    return DocumentIngestRequest(**base)  # type: ignore[arg-type]


class TestHierarquia:
    def test_niveis_legiveis_sao_cumulativos(self) -> None:
        assert readable_levels(AccessLevel.OPERATIONAL) == (AccessLevel.OPERATIONAL,)
        assert readable_levels(AccessLevel.EXECUTIVE) == (
            AccessLevel.OPERATIONAL,
            AccessLevel.EXECUTIVE,
        )
        assert set(readable_levels(AccessLevel.SYSTEM_ADMIN)) == set(AccessLevel)


class TestMemoryScopeFilter:
    def test_padrao_e_fail_closed(self) -> None:
        scope = MemoryScopeFilter()
        assert scope.user_role is AccessLevel.OPERATIONAL
        assert scope.effective_access_levels == ["OPERATIONAL"]
        assert scope.cross_department is False

    def test_pedir_nivel_acima_do_papel_e_erro_nao_filtro_silencioso(self) -> None:
        with pytest.raises(ValidationError, match="nao pode ler"):
            MemoryScopeFilter(
                user_role=AccessLevel.OPERATIONAL, access_levels=[AccessLevel.EXECUTIVE]
            )

    def test_cross_department_exige_executivo(self) -> None:
        with pytest.raises(ValidationError, match="cross_department"):
            MemoryScopeFilter(user_role=AccessLevel.OPERATIONAL, cross_department=True)
        assert MemoryScopeFilter(user_role=AccessLevel.EXECUTIVE, cross_department=True)

    def test_executivo_pode_estreitar_para_um_nivel(self) -> None:
        scope = MemoryScopeFilter(
            user_role=AccessLevel.EXECUTIVE, access_levels=[AccessLevel.EXECUTIVE]
        )
        assert scope.effective_access_levels == ["EXECUTIVE"]

    def test_access_levels_vazio_e_rejeitado(self) -> None:
        with pytest.raises(ValidationError, match="vazio"):
            MemoryScopeFilter(access_levels=[])

    def test_departamentos_sao_normalizados(self) -> None:
        scope = MemoryScopeFilter(departments=[" noc ", "NOC", "financeiro", ""])
        assert scope.departments == ["NOC", "FINANCEIRO"]

    def test_index_path_e_doc_type_sao_validados(self) -> None:
        assert MemoryScopeFilter(index_path="/FINANCAS_OPEX/CONTRATOS_TI/").index_path == (
            "FINANCAS_OPEX/CONTRATOS_TI"
        )
        for ruim in ("financas", "A/B/C", "A/b c", "A/B'; DROP TABLE x;--"):
            with pytest.raises(ValidationError):
                MemoryScopeFilter(index_path=ruim)
        with pytest.raises(ValidationError):
            MemoryScopeFilter(doc_types=["Deep Research"])


class TestExecutiveScopeGuard:
    def test_operacional_com_filtro_executivo_para_antes_do_sql(self) -> None:
        forjado = MemoryScopeFilter(user_role=AccessLevel.EXECUTIVE, cross_department=True)
        with pytest.raises(ValidationError, match="declara papel EXECUTIVE"):
            ExecutiveScopeGuard(user=OPERACIONAL, scope=forjado)

    def test_operacional_nao_consulta_departamento_alheio(self) -> None:
        alheio = MemoryScopeFilter(departments=["financeiro"])
        with pytest.raises(ValidationError, match="nao pertence"):
            ExecutiveScopeGuard(user=OPERACIONAL, scope=alheio)

    def test_for_user_amarra_papel_e_departamentos_ao_canal(self) -> None:
        scope = MemoryScopeFilter.for_user(OPERACIONAL, doc_types=["playbook"])
        assert scope.user_role is AccessLevel.OPERATIONAL
        assert scope.departments == ["NOC"]
        assert scope.doc_types == ["playbook"]

    def test_for_user_nao_aceita_papel_por_parametro(self) -> None:
        with pytest.raises(TypeError):
            MemoryScopeFilter.for_user(OPERACIONAL, user_role=AccessLevel.EXECUTIVE)

    def test_for_user_propaga_tentativa_de_escalada(self) -> None:
        with pytest.raises(ValidationError):
            MemoryScopeFilter.for_user(OPERACIONAL, cross_department=True)
        with pytest.raises(ValidationError):
            MemoryScopeFilter.for_user(OPERACIONAL, access_levels=[AccessLevel.SYSTEM_ADMIN])

    def test_executivo_pode_tudo_que_o_papel_permite(self) -> None:
        scope = MemoryScopeFilter.for_user(EXECUTIVA, cross_department=True, departments=["noc"])
        assert scope.cross_department and scope.departments == ["NOC"]
        assert scope.effective_access_levels == ["OPERATIONAL", "EXECUTIVE"]

    def test_escopo_mais_estreito_que_o_usuario_e_aceito(self) -> None:
        estreito = MemoryScopeFilter(user_role=AccessLevel.OPERATIONAL, departments=["financeiro"])
        assert ExecutiveScopeGuard(user=EXECUTIVA, scope=estreito).scope is estreito


class TestScopeSql:
    def test_clausula_e_deterministica_e_parametrizada(self) -> None:
        clause = document_scope_clause("d", 5)
        assert "d.status = 'active'" in clause
        assert "d.access_level = ANY($5::text[])" in clause
        assert "$6::boolean" in clause and "d.department_scope && $7::text[]" in clause

    def test_args_saem_do_filtro_validado(self) -> None:
        scope = MemoryScopeFilter.for_user(OPERACIONAL)
        assert document_scope_args(scope) == (["OPERATIONAL"], False, ["NOC"])


class TestDocumentIngestRequest:
    def test_classificacao_e_obrigatoria(self) -> None:
        with pytest.raises(ValidationError, match="access_level"):
            DocumentIngestRequest(slug="x", title="X", doc_type="note", raw_content="conteudo")  # type: ignore[call-arg]

    def test_hash_ignora_fim_de_linha_e_borda_mas_nao_conteudo(self) -> None:
        unix = _request(raw_content="# A\n\nlinha\n")
        windows = _request(raw_content="\r\n# A\r\n\r\nlinha\r\n\r\n")
        outro = _request(raw_content="# A\n\nlinha!\n")
        assert unix.content_hash == windows.content_hash == content_sha256("# A\n\nlinha")
        assert unix.content_hash != outro.content_hash
        assert len(unix.content_hash) == 64

    def test_raw_content_e_preservado_intacto(self) -> None:
        bruto = "\r\n# A\r\n\r\nlinha\r\n"
        assert _request(raw_content=bruto).raw_content == bruto

    @pytest.mark.parametrize("slug", ["Playbook", "com espaco", "-inicio", "acentuação", ""])
    def test_slug_invalido(self, slug: str) -> None:
        with pytest.raises(ValidationError):
            _request(slug=slug)

    def test_conteudo_vazio_e_rejeitado(self) -> None:
        with pytest.raises(ValidationError, match="vazio"):
            _request(raw_content="  \n ")

    def test_tags_e_departamentos_sao_normalizados(self) -> None:
        request = _request(tags=["Rede_Neutra", " opex ", "opex"], department_scope=["noc", "NOC"])
        assert request.tags == ["opex", "rede_neutra"]
        assert request.department_scope == ["NOC"]

    def test_metadata_tags_nao_sobrescreve_chave_do_pipeline(self) -> None:
        with pytest.raises(ValidationError, match="reservada"):
            _request(metadata_tags={"chunk_index": 99})
        with pytest.raises(ValidationError, match="reservada"):
            _request(metadata_tags={"tags": ["x"]})
        assert _request(metadata_tags={"contrato": "CT-2024/0187"})

    def test_index_paths_exigem_no_tematico(self) -> None:
        with pytest.raises(ValidationError, match="NO_TEMATICO"):
            _request(index_paths=["FINANCAS_OPEX"])
        request = _request(index_paths=["FINANCAS_OPEX/CONTRATOS_TI", "FINANCAS_OPEX/CONTRATOS_TI"])
        assert request.index_paths == ["FINANCAS_OPEX/CONTRATOS_TI"]


class TestSettings:
    def test_identificadores_que_entram_no_ddl_sao_validados(self) -> None:
        for campo in ("db_schema", "fts_config"):
            with pytest.raises(ValidationError):
                MemoryProviderSettings(**{campo: "public; DROP TABLE x"})  # type: ignore[arg-type]

    def test_dimensao_respeita_o_limite_do_hnsw(self) -> None:
        with pytest.raises(ValidationError):
            MemoryProviderSettings(embedding_dim=3072)

    def test_dsn_nao_tem_default(self) -> None:
        with pytest.raises(ValueError, match="DSN ausente"):
            MemoryProviderSettings().require_database_url()

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "ORKMIND_PROVIDER_DATABASE_URL", "postgresql://u@localhost:5433/provider_test"
        )
        monkeypatch.setenv("ORKMIND_PROVIDER_EMBEDDING_DIM", "768")
        monkeypatch.setenv("ORKMIND_PROVIDER_CHUNK_MAX_TOKENS", "256")
        settings = MemoryProviderSettings.from_env(rrf_k=30)
        assert settings.database_url.endswith(":5433/provider_test")
        assert settings.embedding_dim == 768
        assert settings.chunking.max_tokens == 256
        assert settings.rrf_k == 30

    def test_ddl_renderizado_tem_tabelas_indices_e_dimensao(self) -> None:
        ddl = render_schema(MemoryProviderSettings(embedding_dim=768, fts_config="simple"))
        assert "{{" not in ddl
        assert "CREATE EXTENSION IF NOT EXISTS vector" in ddl
        for tabela in (
            "core_blocks",
            "chat_history",
            "wiki_documents",
            "wiki_document_versions",
            "wiki_chunks",
            "meta_index",
        ):
            assert f"CREATE TABLE IF NOT EXISTS {tabela} " in ddl
        assert "vector(768)" in ddl and "'simple'::regconfig" in ddl
        assert "USING hnsw (embedding vector_cosine_ops)" in ddl
        assert "WITH (m = 16, ef_construction = 64)" in ddl
        assert "USING gin (tsv)" in ddl and "USING gin (metadata_tags)" in ddl
        assert "ON DELETE CASCADE" in ddl


class TestHashingEmbedding:
    def test_deterministico_normalizado_e_lexicalmente_sensivel(self) -> None:
        embedder = HashingEmbeddingProvider(64)

        async def run() -> list[list[float]]:
            return await embedder.embed_batch(
                ["contrato da rede neutra", "Contrato da REDE neutra", "politica de ferias"]
            )

        a, b, c = asyncio.run(run())
        assert len(a) == 64 and a == b
        assert abs(sum(v * v for v in a) - 1.0) < 1e-9

        def cos(x: list[float], y: list[float]) -> float:
            return sum(p * q for p, q in zip(x, y))

        assert cos(a, b) > cos(a, c)


class TestLexicalCandidates:
    def test_entram_os_mais_raros_ate_o_teto(self) -> None:
        frequencias = {"contrat": 900, "/0187": 1, "ct": 40, "valor": 300}
        assert select_candidate_lexemes(frequencias, cap=50) == ["/0187", "ct"]
        assert select_candidate_lexemes(frequencias, cap=2000) == [
            "/0187",
            "ct",
            "valor",
            "contrat",
        ]

    def test_so_palavra_onipresente_nao_gera_candidato(self) -> None:
        assert select_candidate_lexemes({"contrat": 2001, "red": 2001}, cap=2000) == []

    def test_frequencia_zero_entra_sempre(self) -> None:
        # Cache velho pode dizer 0 para um termo recem-ingerido.
        assert select_candidate_lexemes({"novo": 0, "contrat": 2001}, cap=2000) == ["novo"]

    def test_lexema_e_escapado_na_sintaxe_de_tsquery(self) -> None:
        assert quote_lexeme("/0187") == "'/0187'"
        assert quote_lexeme("d'agua") == "'d''agua'"
        assert quote_lexeme("a\\b") == "'a\\\\b'"


class TestImportSemDriver:
    """O pacote inteiro importa sem o extra; so CONECTAR exige o asyncpg.

    Sem isto, `pip install -e ".[dev]"` seguido de `pytest` - exatamente o que
    o README manda o contribuidor fazer - abortava a suite INTEIRA na coleta,
    porque qualquer import do pacote arrastava o driver.
    """

    def test_parte_pura_funciona_sem_asyncpg(self, tmp_path: Path) -> None:
        # Um asyncpg.py que estoura no import simula o extra ausente.
        (tmp_path / "asyncpg.py").write_text(
            "raise ImportError(\"No module named 'asyncpg'\")\n", encoding="utf-8"
        )
        script = textwrap.dedent(
            # raw: o \n do documento de teste tem de chegar literal ao subprocesso.
            r"""
            import asyncio

            import orkmind.memory_provider as mp
            from orkmind.memory_provider.db.migrations import render_schema
            from orkmind.memory_provider.services.chunking import chunk_document

            settings = mp.MemoryProviderSettings(embedding_dim=64)
            assert mp.MemoryScopeFilter(user_role=mp.AccessLevel.OPERATIONAL)
            assert mp.NativeMemoryProvider is not None
            assert "wiki_chunks" in render_schema(settings)
            assert chunk_document("# Titulo\n\nCorpo do documento.")

            conectar = mp.NativeMemoryProvider.connect(
                mp.MemoryProviderSettings(database_url="postgresql://u@localhost/x")
            )
            try:
                asyncio.run(conectar)
            except mp.MemoryProviderError as erro:
                assert "orkmind[memory-provider]" in str(erro), erro
            else:
                raise AssertionError("conectar sem o driver deveria falhar")
            """
        )
        raiz_do_pacote = Path(orkmind.__file__).parents[1]
        resultado = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), str(raiz_do_pacote)])},
            capture_output=True,
            text=True,
        )
        assert resultado.returncode == 0, resultado.stderr
