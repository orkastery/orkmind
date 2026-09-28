"""Confere o sdist e o wheel antes de qualquer publicacao.

Uso: python .github/scripts/conferir_pacote.py dist/

Falha (codigo 1) quando:
  - o sdist leva material interno (estado do Orkastery, worktrees, relatorios,
    planos, benchmark, plugin do OpenClaw, venv, node_modules);
  - o wheel nao leva os arquivos de dados que o pacote le em tempo de execucao;
  - algum arquivo de texto de um dos dois tem cara de credencial.
O achado sai com artefato, arquivo e linha; o valor nunca e impresso.
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from collections import Counter
from pathlib import Path

PROIBIDOS_NO_SDIST = re.compile(
    r"(^|/)(\.orkastery|\.worktrees|\.github|\.venv|node_modules|docs|bench)(/|$)"
    r"|(^|/)(AGENTS\.md|orkastery\.yaml)$"
    r"|(^|/)IMPLEMENTATION_REPORT[^/]*$"
    r"|(^|/)integrations/openclaw(/|$)"
)

DADOS_NO_WHEEL = (
    "orkmind/py.typed",
    "orkmind/contracts/company-brain.v1.json",
    "orkmind/memory_provider/db/schema.sql",
    "orkmind/store/company_brain_v1.sql",
)

# Credenciais de servicos comuns. DSN com senha so conta fora de host de teste
# (RFC 2606) e quando a senha nao e marcador obvio de exemplo.
CREDENCIAIS = {
    "chave AWS": r"AKIA[0-9A-Z]{16}",
    "token GitHub": r"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,}",
    "token npm": r"npm_[A-Za-z0-9]{36}",
    "chave OpenAI": r"sk-(proj-)?[A-Za-z0-9_-]{32,}",
    "chave Anthropic": r"sk-ant-[A-Za-z0-9_-]{32,}",
    "chave Google": r"AIza[0-9A-Za-z_-]{35}",
    "token Slack": r"xox[baprs]-[A-Za-z0-9-]{10,}",
    "token de bot": r"\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b",
    "chave privada": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "DSN com senha": (
        r"(postgres(ql)?|mysql|mongodb(\+srv)?|redis)://[^:/\s@]+:"
        r"(?!\$|\*|senha|password|xxx)[^@\s]{6,}@"
        r"(?![^/\s]*\.(invalid|example|test)\b)(?!(host|db|h)[/:\s])"
    ),
}
PADROES = {rotulo: re.compile(p) for rotulo, p in CREDENCIAIS.items()}


def varrer(artefato: str, nome: str, dados: bytes) -> list[str]:
    try:
        texto = dados.decode("utf-8")
    except UnicodeDecodeError:
        return []
    achados = []
    for numero, linha in enumerate(texto.splitlines(), start=1):
        for rotulo, padrao in PADROES.items():
            if padrao.search(linha):
                achados.append(f"{artefato}: {nome}:{numero} [{rotulo}]")
    return achados


def conferir_sdist(caminho: Path) -> list[str]:
    erros = []
    topo = Counter()
    with tarfile.open(caminho) as tar:
        for membro in tar.getmembers():
            if not membro.isfile():
                continue
            # Tira o prefixo `orkmind-X.Y.Z/` que todo sdist tem.
            nome = membro.name.split("/", 1)[1] if "/" in membro.name else membro.name
            partes = nome.split("/")
            topo["/".join(partes[:2]) if len(partes) > 2 else nome] += 1
            if PROIBIDOS_NO_SDIST.search(nome):
                erros.append(f"{caminho.name}: material interno no sdist: {nome}")
            arquivo = tar.extractfile(membro)
            if arquivo is not None:
                erros += varrer(caminho.name, nome, arquivo.read())
    print(f"sdist {caminho.name}: {sum(topo.values())} arquivos")
    for grupo, quantos in sorted(topo.items()):
        print(f"  {quantos:4d}  {grupo}")
    return erros


def conferir_wheel(caminho: Path) -> list[str]:
    erros = []
    with zipfile.ZipFile(caminho) as whl:
        nomes = set(whl.namelist())
        for obrigatorio in DADOS_NO_WHEEL:
            if obrigatorio not in nomes:
                erros.append(f"{caminho.name}: falta {obrigatorio} no wheel")
        for nome in sorted(nomes):
            erros += varrer(caminho.name, nome, whl.read(nome))
    print(f"wheel {caminho.name}: {len(nomes)} arquivos")
    return erros


def main(argv: list[str]) -> int:
    pasta = Path(argv[1] if len(argv) > 1 else "dist")
    sdists = sorted(pasta.glob("*.tar.gz"))
    wheels = sorted(pasta.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        print(f"esperava um sdist e um wheel em {pasta}, achei {len(sdists)} e {len(wheels)}")
        return 1
    erros = conferir_sdist(sdists[0]) + conferir_wheel(wheels[0])
    for erro in erros:
        print(f"ERRO {erro}")
    return 1 if erros else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
