"""Confere os guias de contribuicao contra a arvore real.

Uso: python .github/scripts/checar_guias.py [raiz] [--so-existencia]

Escopo: `CONTRIBUTING.md`, `docs/contribuir/*.md` e os modelos de issue e de PR em `.github/`.

Falha (codigo 1), com arquivo e linha, quando:
  - uma linha de bloco `bash` sai != 0. Cada linha roda da raiz num `bash -o pipefail -c`
    proprio, com o `bin` do Python que roda esta checagem na frente do PATH, stdin fechado, HOME
    num diretorio vazio e SEM as variaveis `ORKMIND_*`: sem `~/.orkmind/config.toml` e sem DSN, o
    bloco que roda aqui roda sem banco, como na maquina de quem acabou de clonar. Bloco precedido
    da linha `<!-- checagem: citado -->` (rede, Docker, banco ou backend configurado, efeito fora
    da maquina, ou saida != 0 por divida anterior que o texto declara) nao roda;
  - um bloco `sh`, `shell`, `zsh` ou `console` nao tem a marca: ou roda como `bash`, ou e citado;
    e bloco sem lingua reprova: marque `bash`, `text` ou a lingua do trecho;
  - um `orkmind <comando> [<sub>]` citado nao existe no CLI; uma marca de `pytest -m`, um extra
    `.[...]`, uma variavel `ORKMIND_*` ou um caminho do repositorio citados nao existem;
  - um link relativo nao existe, ou a ancora dele nao bate com um titulo do destino. Fora do
    escopo, todo Markdown versionado tem os links relativos conferidos, sem as ancoras;
  - um rotulo dos modelos de issue nao esta na tabela de rotulos de `triagem.md`, ou um link
    `github.com/orkastery/orkmind/blob/main/...` aponta para caminho que nao existe;
  - a tabela de checks de `pull-request.md` diverge dos jobs de `.github/workflows/ci.yml`;
  - um guia de `docs/contribuir/` nao esta no indice.

`--so-existencia` confere tudo menos rodar os blocos: e o modo do teste da suite, porque rodar
os blocos rodaria a propria suite. Fora o que esta acima, os blocos rodam com o resto do seu
ambiente: rode a checagem completa com o Python do venv e so sobre guias que voce leu, como faria
com um script de instalacao.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

MARCA_CITADO = "<!-- checagem: citado -->"
DIR_GUIAS = "docs/contribuir"
GUIA_TRIAGEM = f"{DIR_GUIAS}/triagem.md"
GUIA_PR = f"{DIR_GUIAS}/pull-request.md"
WORKFLOW_CI = ".github/workflows/ci.yml"
SHELL_SEM_BASH = {"sh", "shell", "zsh", "console"}
# Abaixo dos 10 min que um passo de verificacao costuma ter: o relatorio sai, em vez de a
# checagem ser morta no meio.
PRAZO_TOTAL_S = 540
# Uma variavel ORKMIND_* citada existe quando aparece no codigo, nos testes ou no CI.
FONTES_DE_VARIAVEL = ("src", "tests", "scripts", "bench", "examples", "integrations",
                      ".github/workflows", ".github/scripts")
SUFIXOS_DE_FONTE = {".py", ".sh", ".ts", ".js", ".mjs", ".json", ".toml", ".yml", ".yaml",
                    ".sql", ".md"}
PASTAS_IGNORADAS = {"node_modules", "dist", "dist-test", ".venv", "__pycache__", ".git"}

VARIAVEL = re.compile(r"\bORKMIND_[A-Z0-9_]*[A-Z0-9]\b")
CAMINHO = re.compile(
    r"(?<![\w./-])((?:src|tests|docs|scripts|bench|examples|integrations|migrations|skills"
    r"|\.github)/[^\s'\"`)\]]*)"
)
EXTRAS = re.compile(r"(?:\.|\borkmind)\[([a-z0-9,\s-]+)\]")
MARCA_PYTEST = re.compile(r"\s-m\s+(?:\"([^\"]*)\"|'([^']*)'|(\S+))")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
LINK_BLOB = re.compile(r"github\.com/orkastery/orkmind/(?:blob|tree)/main/([^\s)\"'>\]]+)")
CRASE = re.compile(r"`([^`\n]+)`")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class Bloco:
    cerca: str
    lingua: str
    citado: bool
    inicio: int
    linhas: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class Markdown:
    blocos: list[Bloco]
    trechos: list[tuple[int, str]]
    links: list[tuple[int, str]]
    titulos: list[tuple[int, str]]
    nao_fechado: int | None


@dataclass
class Execucao:
    ok: bool
    status: str
    segundos: float
    cauda: str


@dataclass
class Resultado:
    falhas: list[str]
    rodados: int
    citados: int
    a_rodar: list[tuple[str, int, str]]


def analisar(texto: str) -> Markdown:
    """Uma passada: blocos cercados (``` ou ~~~, fechados por cerca igual ou maior, como no
    CommonMark), trechos em crase, links e titulos fora deles."""
    blocos: list[Bloco] = []
    trechos: list[tuple[int, str]] = []
    links: list[tuple[int, str]] = []
    titulos: list[tuple[int, str]] = []
    aberto: Bloco | None = None
    anterior = ""
    for numero, linha in enumerate(texto.replace("\r\n", "\n").split("\n"), start=1):
        if aberto is not None:
            fecha = re.match(r"^\s*(`{3,}|~{3,})\s*$", linha)
            if fecha and fecha[1][0] == aberto.cerca[0] and len(fecha[1]) >= len(aberto.cerca):
                blocos.append(aberto)
                aberto = None
                anterior = linha
            else:
                aberto.linhas.append((numero, linha))
            continue
        abre = re.match(r"^\s*(`{3,}|~{3,})\s*([^\s`]*)", linha)
        if abre:
            citado = anterior.strip() == MARCA_CITADO
            aberto = Bloco(cerca=abre[1], lingua=abre[2], citado=citado, inicio=numero)
            continue
        if linha.strip():
            anterior = linha
        titulo = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", linha)
        if titulo:
            titulos.append((numero, titulo[1]))
        for m in CRASE.finditer(linha):
            trechos.append((numero, m[1]))
        for m in LINK.finditer(CRASE.sub("", linha)):
            links.append((numero, m[1]))
    return Markdown(blocos, trechos, links, titulos, aberto.inicio if aberto else None)


def comandos_do_bloco(bloco: Bloco) -> list[tuple[int, str]]:
    """Os comandos de um bloco: sem linha vazia nem comentario, e com `\\` no fim juntando."""
    comandos: list[tuple[int, str]] = []
    atual: tuple[int, str] | None = None
    for numero, texto in bloco.linhas:
        t = texto.strip()
        if atual is None and (not t or t.startswith("#")):
            continue
        parte = t.removesuffix("\\").strip()
        atual = (atual[0], f"{atual[1]} {parte}") if atual else (numero, parte)
        if not t.endswith("\\"):
            comandos.append(atual)
            atual = None
    if atual:
        comandos.append(atual)
    return comandos


def ancora(titulo: str) -> str:
    """A ancora que o GitHub gera para um titulo: minusculas, sem pontuacao, espaco vira `-`."""
    texto = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", titulo).strip().lower()
    return re.sub(r"[^\w\- ]", "", texto).replace(" ", "-")


def ancoras(md: Markdown) -> set[str]:
    vistas: dict[str, int] = {}
    saida = set()
    for _, titulo in md.titulos:
        base = ancora(titulo)
        n = vistas.get(base, 0)
        saida.add(base if n == 0 else f"{base}-{n}")
        vistas[base] = n + 1
    return saida


def carregar_cli() -> dict[str, set[str] | None]:
    """Comandos do CLI por introspeccao do click: grupo leva o conjunto dos subcomandos."""
    import click

    from orkmind.cli.main import cli

    return {
        nome: set(cmd.commands) if isinstance(cmd, click.Group) else None
        for nome, cmd in cli.commands.items()
    }


def segmentos(comando: str) -> list[str]:
    """Cada comando simples, depois de `&&`, `||`, `;` e `|`, sem `!`, `(` e `VAR=valor`."""
    saida = []
    for segmento in re.split(r"&&|\|\||;|\|", comando):
        limpo = re.sub(r"^[!(]\s*", "", segmento.strip())
        saida.append(re.sub(r"^(?:[A-Z_][A-Z0-9_]*=\S*\s+)+", "", limpo))
    return saida


def citacoes_do_orkmind(comando: str) -> list[tuple[str, str]]:
    """Cada `orkmind <topo> [<sub>]` em posicao de comando."""
    citacoes = []
    for limpo in segmentos(comando):
        m = re.match(r"^orkmind\s+(\S+)(?:\s+(\S+))?", limpo)
        # Opcao global (`orkmind --version`) e marcador de lugar (`orkmind <comando>`) nao contam.
        if m and not m[1].startswith(("-", "<")):
            citacoes.append((m[1], m[2] or ""))
    return citacoes


def rotulos_da_triagem(texto: str) -> set[str]:
    """Primeira coluna, em crase, das linhas de tabela da secao `## Rotulos` de `triagem.md`."""
    rotulos = set()
    secao = ""
    for linha in texto.splitlines():
        if linha.startswith("## "):
            secao = linha[3:].strip().lower()
        m = re.match(r"^\|\s*`([^`]+)`\s*\|", linha)
        if m and secao.startswith("rótulos"):
            rotulos.add(m[1])
    return rotulos


def rotulos_do_modelo(yml: str) -> list[str] | None:
    """Rotulos de um modelo de issue: `labels: [a, "b"]`, `labels: a, b` ou a lista em linhas.
    `None` sem a chave; lista vazia quando a chave existe e nada foi lido."""
    linhas = yml.replace("\r\n", "\n").split("\n")
    i = next((n for n, linha in enumerate(linhas) if linha.startswith("labels:")), None)
    if i is None:
        return None

    def limpar(r: str) -> str:
        return r.strip().strip("\"'")

    valor = re.sub(r"\s+#.*$", "", linhas[i][len("labels:"):]).strip()
    if valor:
        itens = valor.strip("[]").split(",")
    else:
        itens = []
        for linha in linhas[i + 1:]:
            m = re.match(r"^\s*-\s+(.+?)\s*$", linha)
            if not m:
                break
            itens.append(m[1])
    return [r for r in map(limpar, itens) if r]


def jobs_do_ci(yml: str) -> set[str]:
    """O `name:` de cada job de `ci.yml`, com `${{ matrix.* }}` trocado por `<versão>`."""
    nomes = set()
    em_jobs = False
    job_aberto = False
    for linha in yml.splitlines():
        if re.match(r"^\S", linha):
            em_jobs = linha.startswith("jobs:")
            continue
        if not em_jobs:
            continue
        if re.match(r"^  [\w-]+:\s*$", linha):
            job_aberto = True
            continue
        m = re.match(r"^    name:\s*(.+?)\s*$", linha)
        if m and job_aberto:
            nome = m[1].strip("\"'")
            nomes.add(re.sub(r"\$\{\{\s*matrix\.[\w.]+\s*\}\}", "<versão>", nome))
            job_aberto = False
    return nomes


def checks_do_guia(texto: str) -> set[str]:
    """Nomes em crase na primeira coluna da tabela da secao `## Checks obrigatórios`."""
    nomes = set()
    secao = ""
    for linha in texto.splitlines():
        if linha.startswith("## "):
            secao = linha[3:].strip().lower()
        if secao == "checks obrigatórios" and linha.startswith("|"):
            primeira = linha.strip("|").split("|")[0]
            nomes.update(CRASE.findall(primeira))
    return nomes


def variaveis_conhecidas(raiz: Path) -> set[str]:
    achadas: set[str] = set()
    for base in FONTES_DE_VARIAVEL:
        pasta = raiz / base
        if not pasta.is_dir():
            continue
        for caminho in pasta.rglob("*"):
            if PASTAS_IGNORADAS.intersection(caminho.relative_to(raiz).parts):
                continue
            if caminho.suffix in SUFIXOS_DE_FONTE and caminho.is_file():
                achadas.update(VARIAVEL.findall(caminho.read_text("utf-8", errors="replace")))
    return achadas


def markdown_versionado(raiz: Path) -> list[str]:
    """O Markdown que o git versiona ou versionaria; fora de um repositorio git, nenhum."""
    try:
        r = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
            cwd=raiz, capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [linha for linha in r.stdout.splitlines() if linha]


def ambiente_dos_blocos(home: str) -> dict[str, str]:
    """O ambiente de quem chama, sem `ORKMIND_*`, com HOME em `home` (vazio) e com o venv desta
    checagem na frente do PATH."""
    ambiente = {k: v for k, v in os.environ.items() if not k.startswith("ORKMIND_")}
    # Sem o HOME de quem chama, o `~/.orkmind/config.toml` da maquina nao chega ao bloco.
    ambiente["HOME"] = home
    # abspath, e nao resolve: o `python` de um venv e um link para o do sistema.
    bin_do_python = os.path.dirname(os.path.abspath(sys.executable))
    ambiente["PATH"] = bin_do_python + os.pathsep + ambiente.get("PATH", "")
    ambiente["NO_COLOR"] = "1"
    ambiente.pop("FORCE_COLOR", None)
    return ambiente


def matar_grupo(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def rodar(raiz: Path, comando: str, prazo_s: int, ambiente: dict[str, str]) -> Execucao:
    inicio = time.monotonic()
    # Sessao propria: no estouro do prazo o grupo inteiro morre, nao so o `bash`.
    # pipefail: a falha do lado esquerdo de um pipe nao some no `xargs` do lado direito.
    proc = subprocess.Popen(
        ["bash", "-o", "pipefail", "-c", comando], cwd=raiz, env=ambiente,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", start_new_session=True,
    )
    try:
        saida, _ = proc.communicate(timeout=prazo_s)
        status = str(proc.returncode)
    except subprocess.TimeoutExpired:
        matar_grupo(proc)
        saida, _ = proc.communicate()
        status = f"pelo prazo ({prazo_s} s)"
    except BaseException:
        # Ctrl-C ou SIGTERM na checagem nao deixa o bloco orfao.
        matar_grupo(proc)
        proc.communicate()
        raise
    linhas = ANSI.sub("", saida or "").strip().split("\n")
    # A cauda de uma suite so traz o resumo: os FAILED vao junto, para dizer QUAL teste caiu.
    reprovados = [linha for linha in linhas if linha.startswith(("FAILED", "ERROR"))][:10]
    return Execucao(
        ok=proc.returncode == 0 and not status.startswith("pelo"),
        status=status,
        segundos=time.monotonic() - inicio,
        cauda="\n".join(linhas[-8:] + reprovados),
    )


def checar(
    raiz: Path,
    executar: bool = True,
    prazo_total_s: int = PRAZO_TOTAL_S,
    ao_rodar: Callable[[str, int, str, Execucao], None] | None = None,
    cli: dict[str, set[str] | None] | None = None,
) -> Resultado:
    """A checagem inteira. `executar=False` confere so a existencia."""
    with tempfile.TemporaryDirectory(prefix="checar-guias-home-") as home:
        return _checar(raiz.resolve(), executar, prazo_total_s, ao_rodar, cli,
                       ambiente_dos_blocos(home))


def _checar(
    raiz: Path,
    executar: bool,
    prazo_total_s: int,
    ao_rodar: Callable[[str, int, str, Execucao], None] | None,
    cli: dict[str, set[str] | None] | None,
    ambiente: dict[str, str],
) -> Resultado:
    falhas: list[str] = []
    a_rodar: list[tuple[str, int, str]] = []
    ja_rodados: set[str] = set()
    citados = 0
    inicio = time.monotonic()

    pyproject = raiz / "pyproject.toml"
    projeto = tomllib.loads(pyproject.read_text("utf-8")) if pyproject.is_file() else {}
    extras = set(projeto.get("project", {}).get("optional-dependencies", {}))
    scripts = set(projeto.get("project", {}).get("scripts", {}))
    marcadores = {
        m.split(":")[0].strip()
        for m in projeto.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
    }
    if cli is None:
        try:
            cli = carregar_cli()
        except Exception as erro:  # qualquer falha de import vira achado, nao excecao
            falhas.append(f"o CLI do orkmind nao importa ({erro}): instale com pip install -e .")
            cli = {}
    variaveis = variaveis_conhecidas(raiz)

    guias = sorted(p.relative_to(raiz).as_posix() for p in (raiz / DIR_GUIAS).glob("*.md"))
    if not guias:
        falhas.append(f"{DIR_GUIAS}: nenhum guia encontrado")
    escopo = (["CONTRIBUTING.md"] if (raiz / "CONTRIBUTING.md").is_file() else []) + guias
    if "CONTRIBUTING.md" not in escopo:
        falhas.append("CONTRIBUTING.md: o indice dos guias nao existe")
    modelos = []
    if (raiz / ".github").is_dir():
        for caminho in sorted((raiz / ".github").rglob("*")):
            rel = caminho.relative_to(raiz).as_posix()
            fora = rel.startswith((".github/workflows/", ".github/scripts/"))
            if caminho.is_file() and caminho.suffix in {".md", ".yml", ".yaml"} and not fora:
                modelos.append(rel)
    cache_md: dict[Path, Markdown] = {}

    def md_de(caminho: Path) -> Markdown:
        if caminho not in cache_md:
            cache_md[caminho] = analisar(caminho.read_text("utf-8"))
        return cache_md[caminho]

    def conferir(rel: str, numero: int, texto: str, em_bloco: bool = False) -> None:
        for topo, sub in citacoes_do_orkmind(texto):
            subcomandos = cli.get(topo, set())
            if topo not in cli:
                falhas.append(f'{rel}:{numero}: o CLI nao tem "orkmind {topo}"')
            elif (
                subcomandos is not None and re.fullmatch(r"[a-z][a-z-]*", sub)
                and sub not in subcomandos
            ):
                falhas.append(f'{rel}:{numero}: o CLI nao tem "orkmind {topo} {sub}"')
        # Script `orkmind-*` so conta em bloco: em crase solta, `orkmind-postgres` e so um nome.
        for limpo in segmentos(texto) if em_bloco else []:
            m = re.match(r"^(orkmind-[a-z][\w-]*)(?:\s|$)", limpo)
            if m and m[1] not in scripts:
                falhas.append(f'{rel}:{numero}: pyproject.toml nao declara o script "{m[1]}"')
        for grupo in EXTRAS.findall(texto):
            for extra in filter(None, (e.strip() for e in grupo.split(","))):
                if extra not in extras:
                    falhas.append(f'{rel}:{numero}: pyproject.toml nao tem o extra "{extra}"')
        if re.search(r"\bpytest\b", texto):
            # So o `-m` depois de `pytest`: o de `python -m pytest` e o modulo, nao a marca.
            depois = re.split(r"\bpytest\b", texto, maxsplit=1)[1]
            for m in MARCA_PYTEST.finditer(depois):
                expressao = next(g for g in m.groups() if g is not None)
                for nome in re.findall(r"[A-Za-z_][\w-]*", expressao):
                    if nome not in {"and", "or", "not"} and nome not in marcadores:
                        falhas.append(
                            f'{rel}:{numero}: pyproject.toml nao declara a marca "{nome}"'
                        )
        for caminho in CAMINHO.findall(texto):
            if any(c in caminho for c in "<*{$"):
                continue
            limpo = caminho.split("::")[0].split("#")[0].rstrip(".,;:")
            if limpo and not (raiz / limpo).exists():
                falhas.append(f"{rel}:{numero}: o caminho {limpo} nao existe")

    def conferir_link(rel: str, numero: int, alvo: str, com_ancora: bool) -> None:
        if re.match(r"^[a-z][a-z0-9+.-]*:", alvo, re.I) or alvo.startswith("/"):
            return
        origem = raiz / rel
        caminho, _, fragmento = alvo.partition("#")
        destino = (origem.parent / unquote(caminho)).resolve() if caminho else origem
        if not destino.is_relative_to(raiz):
            falhas.append(f"{rel}:{numero}: o link {alvo} sai do repositorio")
        elif not destino.exists():
            falhas.append(f"{rel}:{numero}: o link {alvo} aponta para caminho que nao existe")
        elif com_ancora and fragmento and destino.suffix == ".md":
            if unquote(fragmento).lower() not in ancoras(md_de(destino)):
                falhas.append(f"{rel}:{numero}: o link {alvo} aponta para ancora que nao existe")

    def conferir_blob(rel: str, texto: str) -> None:
        for m in LINK_BLOB.finditer(texto):
            caminho, _, fragmento = m[1].partition("#")
            destino = raiz / unquote(caminho)
            numero = texto.count("\n", 0, m.start()) + 1
            if not destino.exists():
                falhas.append(f"{rel}:{numero}: o link para {caminho} aponta para caminho que "
                              "nao existe")
            elif fragmento and destino.suffix == ".md":
                if unquote(fragmento).lower() not in ancoras(md_de(destino)):
                    falhas.append(f"{rel}:{numero}: o link para {m[1]} aponta para ancora que "
                                  "nao existe")

    def conferir_variaveis(rel: str, texto: str) -> None:
        for numero, linha in enumerate(texto.splitlines(), start=1):
            for nome in VARIAVEL.findall(linha):
                if nome not in variaveis:
                    falhas.append(f"{rel}:{numero}: a variavel {nome} nao aparece no codigo "
                                  "nem nos testes")

    for rel in escopo:
        texto = (raiz / rel).read_text("utf-8")
        md = md_de(raiz / rel)
        if md.nao_fechado:
            falhas.append(f"{rel}:{md.nao_fechado}: bloco de codigo aberto e nao fechado")
        conferir_variaveis(rel, texto)
        conferir_blob(rel, texto)
        for numero, trecho in md.trechos:
            conferir(rel, numero, trecho)
        for numero, alvo in md.links:
            conferir_link(rel, numero, alvo, com_ancora=True)
        for bloco in md.blocos:
            lingua = bloco.lingua.lower()
            if lingua in SHELL_SEM_BASH and not bloco.citado:
                falhas.append(
                    f"{rel}:{bloco.inicio}: bloco {lingua} sem a marca de citado: "
                    "use bash para rodar, ou marque como citado"
                )
            if not lingua:
                falhas.append(f"{rel}:{bloco.inicio}: bloco sem lingua: marque bash, text ou a "
                              "lingua do trecho")
            roda = lingua == "bash" and not bloco.citado
            for numero, comando in comandos_do_bloco(bloco):
                conferir(rel, numero, comando, em_bloco=True)
                if not roda:
                    citados += 1
                    continue
                a_rodar.append((rel, numero, comando))
                # O mesmo comando em dois guias prova a mesma coisa: roda uma vez.
                if not executar or comando in ja_rodados:
                    continue
                ja_rodados.add(comando)
                restante = int(prazo_total_s - (time.monotonic() - inicio))
                if restante < 1:
                    falhas.append(f'{rel}:{numero}: "{comando}" nao rodou: a checagem passou do '
                                  f"prazo total ({prazo_total_s} s)")
                    continue
                r = rodar(raiz, comando, restante, ambiente)
                if ao_rodar:
                    ao_rodar(rel, numero, comando, r)
                if not r.ok:
                    falhas.append(f'{rel}:{numero}: "{comando}" saiu {r.status}\n{r.cauda}')

    for rel in markdown_versionado(raiz):
        if rel in escopo or not (raiz / rel).is_file():
            continue
        for numero, alvo in md_de(raiz / rel).links:
            conferir_link(rel, numero, alvo, com_ancora=False)

    if "CONTRIBUTING.md" in escopo:
        indice = raiz / "CONTRIBUTING.md"
        ligados = {
            (indice.parent / unquote(alvo.partition("#")[0])).resolve()
            for _, alvo in md_de(indice).links
            if alvo.partition("#")[0]
        }
        for rel in guias:
            if (raiz / rel).resolve() not in ligados:
                falhas.append(f"CONTRIBUTING.md: o guia {rel} nao esta no indice")

    triagem = raiz / GUIA_TRIAGEM
    rotulos = rotulos_da_triagem(triagem.read_text("utf-8")) if triagem.is_file() else set()
    for rel in modelos:
        texto = (raiz / rel).read_text("utf-8")
        conferir_variaveis(rel, texto)
        conferir_blob(rel, texto)
        for numero, linha in enumerate(texto.splitlines(), start=1):
            for trecho in CRASE.findall(linha):
                conferir(rel, numero, trecho)
        if rel.startswith(".github/ISSUE_TEMPLATE/"):
            do_modelo = rotulos_do_modelo(texto)
            if do_modelo == []:
                falhas.append(f"{rel}: a chave labels existe, mas nenhum rotulo foi lido")
            for rotulo in do_modelo or []:
                if rotulo not in rotulos:
                    falhas.append(
                        f'{rel}: o rotulo "{rotulo}" nao esta na tabela de {GUIA_TRIAGEM}'
                    )

    guia_pr, ci = raiz / GUIA_PR, raiz / WORKFLOW_CI
    if guia_pr.is_file() and ci.is_file():
        no_guia = checks_do_guia(guia_pr.read_text("utf-8"))
        no_ci = jobs_do_ci(ci.read_text("utf-8"))
        for nome in sorted(no_guia - no_ci):
            falhas.append(f'{GUIA_PR}: o check "{nome}" nao e job de {WORKFLOW_CI}')
        for nome in sorted(no_ci - no_guia):
            falhas.append(f'{GUIA_PR}: o job "{nome}" de {WORKFLOW_CI} falta na tabela de checks')

    return Resultado(falhas, len(a_rodar), citados, a_rodar)


def _encerrar(sinal: int, _quadro: object) -> None:
    # SIGTERM vira SystemExit: o `rodar` mata o grupo do bloco antes de a checagem sair.
    raise SystemExit(128 + sinal)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _encerrar)
    parser = argparse.ArgumentParser(description="Confere os guias de contribuicao.")
    parser.add_argument("raiz", nargs="?", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--so-existencia", action="store_true",
                        help="confere tudo menos rodar os blocos bash")
    args = parser.parse_args(argv)

    def ao_rodar(rel: str, numero: int, comando: str, r: Execucao) -> None:
        print(f"{'ok   ' if r.ok else 'FALHA'} {r.segundos:6.1f} s  {rel}:{numero}  {comando}",
              flush=True)

    resultado = checar(args.raiz, executar=not args.so_existencia, ao_rodar=ao_rodar)
    for falha in resultado.falhas:
        print(falha)
    print(f"{resultado.rodados} comando(s) a rodar (repetidos rodam uma vez), "
          f"{resultado.citados} citado(s) sem rodar, {len(resultado.falhas)} falha(s)")
    return 1 if resultado.falhas else 0


if __name__ == "__main__":
    sys.exit(main())
