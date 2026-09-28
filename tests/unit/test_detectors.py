"""Tests for context detectors."""

import pytest

from orkmind.core.detectors import detect_context


class TestKeywordDetector:
    def test_deploy_detection(self) -> None:
        tags = detect_context("let's deploy the app")
        assert "deploy" in tags.skill
        assert "deploy" in tags.situation

    def test_git_detection(self) -> None:
        tags = detect_context("check the git history")
        assert "git" in tags.skill

    def test_docker_detection(self) -> None:
        tags = detect_context("build the Docker image")
        assert "docker" in tags.skill

    def test_debug_detection(self) -> None:
        tags = detect_context("let me debug this issue")
        assert "debugging" in tags.situation

    def test_code_review_detection(self) -> None:
        tags = detect_context("time for code review")
        assert "code-review" in tags.situation

    def test_terraform_detection(self) -> None:
        tags = detect_context("update the terraform modules")
        assert "terraform" in tags.skill

    def test_auth_detection(self) -> None:
        tags = detect_context("fix the authentication flow")
        assert "auth" in tags.skill
        assert "security" in tags.domain

    def test_multiple_detections(self) -> None:
        tags = detect_context("deploy the docker containers to kubernetes")
        assert "deploy" in tags.skill
        assert "docker" in tags.skill
        assert "kubernetes" in tags.skill

    def test_no_detection(self) -> None:
        tags = detect_context("hello world")
        assert tags.to_dict() == {}


class TestFilePathDetector:
    def test_terraform_file(self) -> None:
        tags = detect_context(files=["infra/main.tf"])
        assert "terraform" in tags.skill
        assert "infra" in tags.domain

    def test_dockerfile(self) -> None:
        tags = detect_context(files=["Dockerfile"])
        assert "docker" in tags.skill

    def test_github_workflow(self) -> None:
        tags = detect_context(files=[".github/workflows/ci.yml"])
        assert "ci-cd" in tags.skill

    def test_tsx_file(self) -> None:
        tags = detect_context(files=["src/components/App.tsx"])
        assert "frontend" in tags.domain

    def test_auth_path(self) -> None:
        tags = detect_context(files=["src/auth/login.py"])
        assert "auth" in tags.skill
        assert "security" in tags.domain

    def test_test_file(self) -> None:
        tags = detect_context(files=["tests/test_models.py"])
        assert "testing" in tags.skill


class TestCombinedDetection:
    def test_conversation_and_files(self) -> None:
        tags = detect_context(
            conversation="let's deploy",
            files=["infra/main.tf"],
        )
        assert "deploy" in tags.skill
        assert "terraform" in tags.skill
        assert "infra" in tags.domain

    def test_plan_example_from_spec(self) -> None:
        """Test the example from the plan: detect_context('vamos fazer deploy', ['main.tf'])"""
        tags = detect_context("vamos fazer deploy", ["main.tf"])
        assert "deploy" in tags.skill
        assert "terraform" in tags.skill


class TestDetectoresPtBr:
    """B5: cobertura pt-BR dos detectores de palavra-chave.

    Todas as frases sao derivadas de uso real (deploy, revisao de codigo,
    migracao, seguranca). As regras mapeiam para as MESMAS tags das regras
    em ingles: so aumentam recall, nao criam dimensao nova.
    """

    @pytest.mark.parametrize(
        ("frase", "dimensao", "valor"),
        [
            ("preciso implantar a nova versao hoje", "situation", "deploy"),
            ("a implantacao quebrou em producao", "situation", "deploy"),
            ("a implantação quebrou em produção", "situation", "deploy"),
            ("vamos reverter esse commit", "situation", "deploy"),
            ("faz uma revisao de codigo nesse PR", "situation", "code-review"),
            ("preciso de uma revisão de código", "situation", "code-review"),
            ("refatorar esse modulo inteiro", "situation", "refactor"),
            ("refatoracao pesada no core", "situation", "refactor"),
            ("refatoração pesada no core", "situation", "refactor"),
            ("depurar esse comportamento estranho", "situation", "debugging"),
            ("depuracao do pipeline", "situation", "debugging"),
            ("depuração do pipeline", "situation", "debugging"),
            ("a falha aconteceu no terceiro turno", "situation", "debugging"),
            ("implementar autenticacao por token", "skill", "auth"),
            ("autenticação via OAuth", "skill", "auth"),
            ("rodar os testes antes de subir", "skill", "testing"),
            ("vou testar isso agora", "skill", "testing"),
            ("aplicar a migracao do banco", "skill", "database"),
            ("as migrações estao pendentes", "skill", "database"),
            ("questao de seguranca aqui", "domain", "security"),
            ("questão de segurança aqui", "domain", "security"),
            ("isso nao e seguro", "domain", "security"),
            ("consultar o banco de dados", "domain", "database"),
            ("mexer na infraestrutura", "domain", "infra"),
            ("o servidor caiu de madrugada", "domain", "backend"),
        ],
    )
    def test_frase_ptbr_infere_a_tag(
        self, frase: str, dimensao: str, valor: str
    ) -> None:
        tags = detect_context(frase)
        assert valor in getattr(tags, dimensao), (
            f"'{frase}' deveria inferir {dimensao}={valor}"
        )

    def test_autenticacao_infere_tambem_o_dominio_de_seguranca(self) -> None:
        tags = detect_context("implementar autenticacao por token")
        assert "auth" in tags.skill
        assert "security" in tags.domain

    def test_migracao_infere_skill_e_dominio(self) -> None:
        tags = detect_context("aplicar a migracao do banco")
        assert "database" in tags.skill
        assert "database" in tags.domain

    @pytest.mark.parametrize(
        "frase",
        [
            "bom dia, tudo bem por ai?",
            "me manda o resumo da reuniao de ontem",
            "obrigado, pode seguir",
        ],
    )
    def test_frase_neutra_nao_dispara_nada(self, frase: str) -> None:
        tags = detect_context(frase)
        assert not tags.skill
        assert not tags.situation
        assert not tags.domain

    def test_erro_generico_nao_e_gatilho(self) -> None:
        """'erro' sozinho dispararia em quase todo turno: fica de fora."""
        tags = detect_context("deu erro")
        assert not tags.situation
