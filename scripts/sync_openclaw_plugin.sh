#!/usr/bin/env bash
# Sincroniza a copia versionada do plugin memory-orkmind para o diretorio de
# extensoes do OpenClaw (~/.openclaw/extensions/memory-orkmind).
#
# A fonte de verdade e integrations/openclaw/memory-orkmind/ neste
# repositorio. O diretorio vivo e copia publicada, nunca o contrario: editar
# direto em ~/.openclaw/extensions/ perde a mudanca no proximo sync.
#
# O comando de restart foi confirmado em 31/08/2026 contra o CLI instalado
# (OpenClaw 2026.7.1-2): `openclaw gateway restart` reinicia o servico
# gerenciado (launchd/systemd/schtasks). O gateway precisa reiniciar porque
# carrega o dist/ do plugin uma unica vez, na subida do processo.
#
# Uso:
#   ./scripts/sync_openclaw_plugin.sh --dry-run   mostra o que mudaria
#   ./scripts/sync_openclaw_plugin.sh             compila e copia
#   ./scripts/sync_openclaw_plugin.sh --restart   compila, copia e reinicia
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${REPO_DIR}/integrations/openclaw/memory-orkmind"
DEST="${OPENCLAW_HOME:-$HOME/.openclaw}/extensions/memory-orkmind"

MODO="${1:-}"

if [[ ! -d "$SRC" ]]; then
    echo "erro: origem nao encontrada: $SRC" >&2
    exit 1
fi

# Compilar sempre: o dist/ nao e versionado, entao ele so existe se alguem
# rodou o build. Publicar um dist/ velho seria pior do que nao publicar.
echo "==> compilando (tsc) em $SRC"
(cd "$SRC" && npm run build >/dev/null)

if [[ "$MODO" == "--dry-run" ]]; then
    echo "==> diferencas em src/"
    diff -rq "${DEST}/src" "${SRC}/src" 2>&1 || true
    echo "==> diferencas em dist/"
    diff -rq "${DEST}/dist" "${SRC}/dist" 2>&1 || true
    echo "==> diferencas em manifesto e package.json"
    diff -q "${DEST}/openclaw.plugin.json" "${SRC}/openclaw.plugin.json" 2>&1 || true
    diff -q "${DEST}/package.json" "${SRC}/package.json" 2>&1 || true
    exit 0
fi

mkdir -p "$DEST"

# Backup do dist/ anterior: se o build novo quebrar o gateway, da para
# voltar sem depender de rebuild.
if [[ -d "${DEST}/dist" ]]; then
    rm -rf "${DEST}/dist.bak"
    cp -r "${DEST}/dist" "${DEST}/dist.bak"
fi

# Copia limpa de src/ e dist/ (remove arquivos que sumiram da fonte).
# node_modules/ do destino e PRESERVADO: e a instalacao de runtime.
rm -rf "${DEST}/src" "${DEST}/dist"
cp -r "${SRC}/src" "${DEST}/src"
cp -r "${SRC}/dist" "${DEST}/dist"
cp "${SRC}/openclaw.plugin.json" "${DEST}/openclaw.plugin.json"
cp "${SRC}/package.json" "${DEST}/package.json"
cp "${SRC}/tsconfig.json" "${DEST}/tsconfig.json"
[[ -f "${SRC}/README.md" ]] && cp "${SRC}/README.md" "${DEST}/README.md"
[[ -f "${SRC}/CHANGELOG.md" ]] && cp "${SRC}/CHANGELOG.md" "${DEST}/CHANGELOG.md"

echo "==> plugin sincronizado: ${SRC} -> ${DEST}"

# Verificacao de paridade: mesma checagem usada na exploracao F1.
echo "==> verificando paridade repo/maquina"
if diff -rq "${SRC}/src" "${DEST}/src" && diff -rq "${SRC}/dist" "${DEST}/dist"; then
    echo "    paridade OK (src/ e dist/ identicos)"
else
    echo "    ATENCAO: divergencia apos o sync" >&2
    exit 1
fi

if [[ "$MODO" == "--restart" ]]; then
    echo "==> reiniciando o gateway do OpenClaw"
    openclaw gateway restart
    openclaw gateway status || true
fi
