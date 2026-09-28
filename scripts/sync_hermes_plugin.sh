#!/usr/bin/env bash
# Sincroniza a copia versionada do plugin OrkMind para o diretorio de
# plugins do Hermes (~/.hermes/plugins/orkmind).
#
# A fonte de verdade e integrations/hermes/orkmind/ neste repositorio.
# Uso: ./scripts/sync_hermes_plugin.sh [--dry-run]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${REPO_DIR}/integrations/hermes/orkmind"
DEST="${HERMES_HOME:-$HOME/.hermes}/plugins/orkmind"

if [[ ! -d "$SRC" ]]; then
    echo "erro: origem nao encontrada: $SRC" >&2
    exit 1
fi

python3 -m py_compile "${SRC}/__init__.py"

if [[ "${1:-}" == "--dry-run" ]]; then
    diff -u "${DEST}/__init__.py" "${SRC}/__init__.py" || true
    exit 0
fi

mkdir -p "$DEST"
if [[ -f "${DEST}/__init__.py" ]]; then
    cp "${DEST}/__init__.py" "${DEST}/__init__.py.bak"
fi
cp "${SRC}/__init__.py" "${DEST}/__init__.py"
[[ -f "${SRC}/plugin.yaml" ]] && cp "${SRC}/plugin.yaml" "${DEST}/plugin.yaml"
[[ -f "${SRC}/README.md" ]] && cp "${SRC}/README.md" "${DEST}/README.md"
[[ -f "${SRC}/CHANGELOG.md" ]] && cp "${SRC}/CHANGELOG.md" "${DEST}/CHANGELOG.md"
rm -rf "${DEST}/__pycache__"
echo "plugin sincronizado: ${SRC} -> ${DEST}"
