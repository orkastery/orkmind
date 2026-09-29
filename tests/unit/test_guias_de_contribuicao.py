"""Testes do checador dos guias de contribuicao (`.github/scripts/checar_guias.py`).

Cada caso monta uma arvore minima em `tmp_path` e prova uma reprovacao. O ultimo roda o
checador sobre a arvore real em modo de existencia (sem rodar os blocos, que rodariam esta
suite): e o que faz o job `testes` de todo PR segurar a paridade dos guias. Fora de um checkout
(o sdist nao leva `.github/` nem `docs/`), os testes pulam.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import textwrap
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[2]
CHECADOR = RAIZ / ".github" / "scripts" / "checar_guias.py"

if not CHECADOR.is_file():
    pytest.skip("checador dos guias ausente (sdist)", allow_module_level=True)


def _carregar() -> ModuleType:
    spec = importlib.util.spec_from_file_location("checar_guias", CHECADOR)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    # dataclass com `from __future__ import annotations` procura o modulo em sys.modules.
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


checar_guias = _carregar()

CLI = {"store": {"info", "export", "import"}, "search": None}
# Montado aqui para o nome falso nao entrar no corpus de variaveis conhecidas da arvore real.
VARIAVEL_FALSA = "ORKMIND_" + "NAO_EXISTE"

PYPROJECT = """
[project]
name = "exemplo"

[project.optional-dependencies]
dev = ["pytest"]

[project.scripts]
orkmind = "exemplo.cli:cli"

[tool.pytest.ini_options]
markers = ["integration: precisa de banco"]
"""

TRIAGEM = """
# Triagem

## Rótulos

| Rótulo | Quando |
| --- | --- |
| `bug` | Defeito |

## Prazo de resposta

Primeira resposta em até 7 dias.
"""

INDICE = """
# Contribuindo

- [Guia](docs/contribuir/guia.md)
- [Triagem](docs/contribuir/triagem.md)
"""


def _arvore(tmp_path: Path, guia: str, extras: dict[str, str] | None = None) -> Path:
    arquivos = {
        "pyproject.toml": PYPROJECT,
        "CONTRIBUTING.md": INDICE,
        "docs/contribuir/guia.md": guia,
        "docs/contribuir/triagem.md": TRIAGEM,
        "tests/test_exemplo.py": 'import os\n\nos.environ.get("ORKMIND_TEST_DATABASE_URL")\n',
        **(extras or {}),
    }
    for rel, texto in arquivos.items():
        caminho = tmp_path / rel
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(textwrap.dedent(texto).lstrip(), encoding="utf-8")
    return tmp_path


def _checar(raiz: Path, executar: bool = True) -> Any:
    return checar_guias.checar(raiz, executar=executar, prazo_total_s=60, cli=CLI)


def test_guia_correto_passa_e_bloco_citado_nao_roda(tmp_path: Path) -> None:
    guia = """
    # Guia

    Rode `orkmind store info`, `python -m pytest -m "not integration"` e instale `.[dev]`.
    O banco vem de `ORKMIND_TEST_DATABASE_URL`; o teste fica em `tests/test_exemplo.py`.
    Ver o [prazo](triagem.md#prazo-de-resposta), os [rótulos](triagem.md#rótulos) e o
    [topo](#guia).

    ```bash
    true
    ```

    <!-- checagem: citado -->

    ```bash
    false
    ```
    """
    resultado = _checar(_arvore(tmp_path, guia))
    assert resultado.falhas == []
    assert [c for _, _, c in resultado.a_rodar] == ["true"]
    assert resultado.citados == 1


def test_bloco_bash_que_falha_reprova(tmp_path: Path) -> None:
    guia = """
    # Guia

    ```bash
    echo antes && false
    ```
    """
    falhas = _checar(_arvore(tmp_path, guia)).falhas
    assert len(falhas) == 1
    assert '"echo antes && false" saiu 1' in falhas[0]
    assert "antes" in falhas[0]


def test_bloco_sh_sem_marca_reprova(tmp_path: Path) -> None:
    guia = """
    # Guia

    ```sh
    true
    ```
    """
    falhas = _checar(_arvore(tmp_path, guia)).falhas
    assert len(falhas) == 1
    assert "bloco sh sem a marca de citado" in falhas[0]


def test_orkmind_inexistente_reprova(tmp_path: Path) -> None:
    guia = """
    # Guia

    `orkmind nada`, `orkmind store nada` e `orkmind search tudo`.
    Nome que so comeca por orkmind nao e comando: `orkmind-postgres`.

    <!-- checagem: citado -->

    ```bash
    docker exec orkmind-postgres psql
    orkmind-nada --help
    ```
    """
    falhas = _checar(_arvore(tmp_path, guia), executar=False).falhas
    assert sorted(falhas) == sorted([
        'docs/contribuir/guia.md:3: o CLI nao tem "orkmind nada"',
        'docs/contribuir/guia.md:3: o CLI nao tem "orkmind store nada"',
        'docs/contribuir/guia.md:10: pyproject.toml nao declara o script "orkmind-nada"',
    ])


def test_marca_extra_variavel_e_caminho_inexistentes_reprovam(tmp_path: Path) -> None:
    guia = f"""
    # Guia

    `python -m pytest -m "not lenta"`, `pip install -e ".[nada]"` e `tests/nada.py`.
    O banco vem de `{VARIAVEL_FALSA}`.
    """
    falhas = _checar(_arvore(tmp_path, guia), executar=False).falhas
    assert sorted(falhas) == sorted([
        'docs/contribuir/guia.md:3: pyproject.toml nao declara a marca "lenta"',
        'docs/contribuir/guia.md:3: pyproject.toml nao tem o extra "nada"',
        "docs/contribuir/guia.md:3: o caminho tests/nada.py nao existe",
        f"docs/contribuir/guia.md:4: a variavel {VARIAVEL_FALSA} nao aparece no codigo "
        "nem nos testes",
    ])


def test_link_e_ancora_quebrados_reprovam(tmp_path: Path) -> None:
    guia = """
    # Guia

    [a](nada.md), [b](triagem.md#nada), [c](#nada) e [d](../../../fora.md).
    """
    falhas = _checar(_arvore(tmp_path, guia), executar=False).falhas
    assert [f.split(": ", 1)[1] for f in falhas] == [
        "o link nada.md aponta para caminho que nao existe",
        "o link triagem.md#nada aponta para ancora que nao existe",
        "o link #nada aponta para ancora que nao existe",
        "o link ../../../fora.md sai do repositorio",
    ]


def test_rotulo_fora_da_triagem_e_link_blob_quebrado_reprovam(tmp_path: Path) -> None:
    modelo = """
    name: Bug
    description: Um defeito
    labels: ["bug", "needs triage"]
    body:
      - type: markdown
        attributes:
          value: |
            Ver https://github.com/orkastery/orkmind/blob/main/docs/contribuir/nada.md
    """
    raiz = _arvore(tmp_path, "# Guia\n", {".github/ISSUE_TEMPLATE/bug.yml": modelo})
    falhas = _checar(raiz, executar=False).falhas
    assert sorted(falhas) == sorted([
        '.github/ISSUE_TEMPLATE/bug.yml: o rotulo "needs triage" nao esta na tabela de '
        "docs/contribuir/triagem.md",
        ".github/ISSUE_TEMPLATE/bug.yml:8: o link para docs/contribuir/nada.md aponta para "
        "caminho que nao existe",
    ])


def test_rotulos_do_modelo_nas_tres_formas() -> None:
    ler = checar_guias.rotulos_do_modelo
    assert ler('name: x\nlabels: ["bug", \'needs triage\']\n') == ["bug", "needs triage"]
    assert ler("labels: bug, question  # comentario\n") == ["bug", "question"]
    assert ler("labels:\n  - bug\n  - help wanted\nbody: []\n") == ["bug", "help wanted"]
    assert ler("labels:\nbody: []\n") == []
    assert ler("name: x\n") is None


def test_checks_do_guia_contra_os_jobs_do_ci(tmp_path: Path) -> None:
    ci = """
    name: CI
    jobs:
      testes:
        name: testes (Python ${{ matrix.python }})
        steps:
          - name: instalar
      pacote:
        name: pacote (sdist e wheel)
    """
    pr = """
    # Pull request

    ## Checks obrigatórios

    | Check | O que roda |
    | --- | --- |
    | `testes (Python <versão>)`, um por versão | A suíte |
    | `outro` | Nada |
    """
    raiz = _arvore(tmp_path, "# Guia\n", {
        ".github/workflows/ci.yml": ci,
        "docs/contribuir/pull-request.md": pr,
        "CONTRIBUTING.md": INDICE + "- [PR](docs/contribuir/pull-request.md)\n",
    })
    falhas = _checar(raiz, executar=False).falhas
    assert falhas == [
        'docs/contribuir/pull-request.md: o check "outro" nao e job de .github/workflows/ci.yml',
        'docs/contribuir/pull-request.md: o job "pacote (sdist e wheel)" de '
        ".github/workflows/ci.yml falta na tabela de checks",
    ]


def test_guia_fora_do_indice_reprova(tmp_path: Path) -> None:
    raiz = _arvore(tmp_path, "# Guia\n", {"docs/contribuir/solto.md": "# Solto\n"})
    falhas = _checar(raiz, executar=False).falhas
    assert falhas == ["CONTRIBUTING.md: o guia docs/contribuir/solto.md nao esta no indice"]


def test_blocos_rodam_sem_as_variaveis_do_orkmind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORKMIND_TEST_DATABASE_URL", "postgresql://h/orkmind_test")
    guia = """
    # Guia

    Sem `ORKMIND_TEST_DATABASE_URL` no ambiente do bloco:

    ```bash
    test -z "${ORKMIND_TEST_DATABASE_URL:-}"
    ```
    """
    assert _checar(_arvore(tmp_path, guia)).falhas == []


def test_blocos_rodam_com_home_vazio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home-de-quem-chama"
    (home / ".orkmind").mkdir(parents=True)
    (home / ".orkmind" / "config.toml").write_text('[store]\nbackend = "pgvector"\n')
    monkeypatch.setenv("HOME", str(home))
    guia = """
    # Guia

    ```bash
    test -z "$(ls -A "$HOME")"
    ```
    """
    assert _checar(_arvore(tmp_path / "repo", guia)).falhas == []


def test_python_da_checagem_vem_primeiro_no_path(tmp_path: Path) -> None:
    esperado = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "python")
    guia = f"""
    # Guia

    ```bash
    test "$(command -v python)" = "{esperado}"
    ```
    """
    assert _checar(_arvore(tmp_path, guia)).falhas == []


def test_pipe_que_falha_a_esquerda_reprova(tmp_path: Path) -> None:
    guia = """
    # Guia

    ```bash
    false | cat
    ```
    """
    falhas = _checar(_arvore(tmp_path, guia)).falhas
    assert len(falhas) == 1 and '"false | cat" saiu 1' in falhas[0]


def test_bloco_nao_espera_stdin_e_o_prazo_mata_o_bloco(tmp_path: Path) -> None:
    guia = """
    # Guia

    ```bash
    cat
    sleep 30
    ```
    """
    inicio = time.monotonic()
    falhas = checar_guias.checar(_arvore(tmp_path, guia), prazo_total_s=3, cli=CLI).falhas
    assert time.monotonic() - inicio < 15
    assert len(falhas) == 1
    assert '"sleep 30" saiu pelo prazo' in falhas[0]


def test_bloco_sem_lingua_reprova(tmp_path: Path) -> None:
    guia = """
    # Guia

    ```
    true
    ```
    """
    falhas = _checar(_arvore(tmp_path, guia)).falhas
    assert falhas == ["docs/contribuir/guia.md:3: bloco sem lingua: marque bash, text ou a "
                      "lingua do trecho"]


def test_ancora_segue_o_github() -> None:
    assert checar_guias.ancora("Falha anterior ou regressão") == "falha-anterior-ou-regressão"
    assert checar_guias.ancora("`orkmind store info`") == "orkmind-store-info"
    assert checar_guias.ancora("O que precisa de banco?") == "o-que-precisa-de-banco"
    md = checar_guias.analisar("# Notas\n\n## Notas\n\n```bash\n# nao e titulo\n```\n")
    assert checar_guias.ancoras(md) == {"notas", "notas-1"}


@pytest.mark.skipif(
    not (RAIZ / "docs" / "contribuir").is_dir(), reason="guias ausentes (sdist)"
)
def test_arvore_real_passa_em_modo_de_existencia() -> None:
    resultado = checar_guias.checar(RAIZ, executar=False)
    assert resultado.falhas == []
    assert resultado.rodados > 0
