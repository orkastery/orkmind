"""Context detectors: Keyword and File path based detection."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from orkmind.core.models import ContextTags

# Keyword -> inferred tags mapping
KEYWORD_RULES: list[tuple[re.Pattern[str], dict[str, list[str]]]] = [
    # Situation detection
    (re.compile(r"\bdeploy\b", re.I), {"skill": ["deploy"], "situation": ["deploy"]}),
    (re.compile(r"\brollback\b", re.I), {"skill": ["deploy"], "situation": ["deploy"]}),
    (re.compile(r"\bci[/-]?cd\b", re.I), {"skill": ["ci-cd"], "situation": ["deploy"]}),
    (re.compile(r"\bcode.?review\b", re.I), {"situation": ["code-review"]}),
    (re.compile(r"\brefactor\b", re.I), {"situation": ["refactor"]}),
    (re.compile(r"\bdebug\b", re.I), {"situation": ["debugging"]}),
    (re.compile(r"\bbug\b", re.I), {"situation": ["debugging"]}),
    # Skill detection
    (re.compile(r"\bgit\b", re.I), {"skill": ["git"]}),
    (re.compile(r"\bdocker\b", re.I), {"skill": ["docker"]}),
    (re.compile(r"\bterraform\b", re.I), {"skill": ["terraform"]}),
    (re.compile(r"\bkubernetes\b|\bk8s\b", re.I), {"skill": ["kubernetes"]}),
    (re.compile(r"\bauth\b|\bauthenticat", re.I), {"skill": ["auth"], "domain": ["security"]}),
    (re.compile(r"\btest(?:ing|s)?\b", re.I), {"skill": ["testing"]}),
    (re.compile(r"\bmigrat(?:e|ion)\b", re.I), {"skill": ["database"], "domain": ["database"]}),
    # Domain detection
    (re.compile(r"\bfrontend\b|\breact\b|\bvue\b|\bangular\b", re.I), {"domain": ["frontend"]}),
    (re.compile(r"\bbackend\b|\bapi\b|\bserver\b", re.I), {"domain": ["backend"]}),
    (re.compile(r"\bdatabase\b|\bpostgres\b|\bmysql\b|\bsql\b", re.I), {"domain": ["database"]}),
    (re.compile(r"\binfra\b|\binfrastructure\b", re.I), {"domain": ["infra"]}),
    (re.compile(r"\bsecur(?:ity|e)\b", re.I), {"domain": ["security"]}),
    # --- pt-BR ---
    #
    # Mapeiam para as MESMAS tags das regras em ingles: so aumentam recall,
    # nao criam dimensao nova. As variantes sem acento existem porque agentes
    # escrevem com frequencia sem acentuacao, e o `\b` do modulo `re` ja
    # funciona com acentos em strings unicode.
    #
    # `\berro\b` foi deliberadamente deixado de fora: e generico demais e
    # dispararia em quase todo turno de uma sessao de desenvolvimento.
    (
        re.compile(r"\bimplanta(?:r|cao|\u00e7\u00e3o)\b", re.I),
        {"skill": ["deploy"], "situation": ["deploy"]},
    ),
    (re.compile(r"\breverter\b", re.I), {"skill": ["deploy"], "situation": ["deploy"]}),
    (
        re.compile(r"\brevis(?:ao|\u00e3o) de c(?:o|\u00f3)digo\b", re.I),
        {"situation": ["code-review"]},
    ),
    (
        re.compile(r"\brefatora(?:r|cao|\u00e7\u00e3o)\b", re.I),
        {"situation": ["refactor"]},
    ),
    (
        re.compile(r"\bdepura(?:r|cao|\u00e7\u00e3o)\b", re.I),
        {"situation": ["debugging"]},
    ),
    (re.compile(r"\bfalha\b", re.I), {"situation": ["debugging"]}),
    (
        re.compile(r"\bautentica(?:r|cao|\u00e7\u00e3o)\b", re.I),
        {"skill": ["auth"], "domain": ["security"]},
    ),
    (re.compile(r"\btestes?\b|\btestar\b", re.I), {"skill": ["testing"]}),
    (
        re.compile(
            r"\bmigra(?:r|cao|\u00e7\u00e3o|coes|\u00e7\u00f5es)\b", re.I
        ),
        {"skill": ["database"], "domain": ["database"]},
    ),
    (
        re.compile(r"\bseguran(?:c|\u00e7)a\b|\bseguro\b", re.I),
        {"domain": ["security"]},
    ),
    (re.compile(r"\bbanco de dados\b", re.I), {"domain": ["database"]}),
    (re.compile(r"\binfraestrutura\b", re.I), {"domain": ["infra"]}),
    (re.compile(r"\bservidor\b", re.I), {"domain": ["backend"]}),
]

# File extension/path -> inferred tags mapping
FILE_PATH_RULES: list[tuple[str, dict[str, list[str]]]] = [
    # Extensions
    (".tf", {"skill": ["terraform"], "domain": ["infra"]}),
    (".hcl", {"skill": ["terraform"], "domain": ["infra"]}),
    ("Dockerfile", {"skill": ["docker"], "domain": ["infra"]}),
    ("docker-compose", {"skill": ["docker"], "domain": ["infra"]}),
    (".github/workflows", {"skill": ["ci-cd"], "situation": ["deploy"]}),
    (".gitlab-ci", {"skill": ["ci-cd"], "situation": ["deploy"]}),
    ("Jenkinsfile", {"skill": ["ci-cd"], "situation": ["deploy"]}),
    # Path patterns
    ("src/auth", {"skill": ["auth"], "domain": ["security"]}),
    ("auth/", {"skill": ["auth"], "domain": ["security"]}),
    ("test", {"skill": ["testing"]}),
    ("spec/", {"skill": ["testing"]}),
    ("migration", {"skill": ["database"], "domain": ["database"]}),
    # Frontend
    (".tsx", {"domain": ["frontend"]}),
    (".jsx", {"domain": ["frontend"]}),
    (".vue", {"domain": ["frontend"]}),
    (".svelte", {"domain": ["frontend"]}),
    (".css", {"domain": ["frontend"]}),
    # Backend
    (".go", {"domain": ["backend"]}),
    (".rs", {"domain": ["backend"]}),
    (".java", {"domain": ["backend"]}),
    # Kubernetes
    ("k8s/", {"skill": ["kubernetes"], "domain": ["infra"]}),
    ("kustomization", {"skill": ["kubernetes"], "domain": ["infra"]}),
    ("helm/", {"skill": ["kubernetes"], "domain": ["infra"]}),
]


def _detect_keywords(text: str) -> ContextTags:
    tags = ContextTags()
    for pattern, inferred in KEYWORD_RULES:
        if pattern.search(text):
            for dim, values in inferred.items():
                current = getattr(tags, dim)
                for v in values:
                    if v not in current:
                        current.append(v)
    return tags


def _detect_file_paths(files: list[str]) -> ContextTags:
    tags = ContextTags()
    for filepath in files:
        normalized = filepath.replace("\\", "/")
        suffix = PurePosixPath(normalized).suffix
        for pattern, inferred in FILE_PATH_RULES:
            if pattern.startswith(".") and suffix == pattern:
                matched = True
            elif pattern in normalized:
                matched = True
            else:
                matched = False
            if matched:
                for dim, values in inferred.items():
                    current = getattr(tags, dim)
                    for v in values:
                        if v not in current:
                            current.append(v)
    return tags


def detect_context(
    conversation: str = "",
    files: list[str] | None = None,
) -> ContextTags:
    """Detect context tags from conversation text and file paths.

    Uses Keyword detector on conversation text and File path detector
    on the list of files touched in the current context.
    """
    keyword_tags = _detect_keywords(conversation)
    file_tags = _detect_file_paths(files or [])
    return keyword_tags.merge(file_tags)
