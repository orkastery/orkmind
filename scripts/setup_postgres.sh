#!/usr/bin/env bash
# Setup PostgreSQL + pgvector for OrkMind development.
# Requires Docker.

set -euo pipefail

CONTAINER_NAME="orkmind-postgres"
PG_USER="orkmind"
PG_PASSWORD="${ORKMIND_PG_PASSWORD:-senha-de-teste}"
PG_DB="orkmind"
PG_PORT="${ORKMIND_PG_PORT:-5432}"

echo "==> Starting PostgreSQL + pgvector container..."

if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "    Container '${CONTAINER_NAME}' already exists."
    docker start "${CONTAINER_NAME}" 2>/dev/null || true
else
    docker run -d \
        --name "${CONTAINER_NAME}" \
        -e POSTGRES_USER="${PG_USER}" \
        -e POSTGRES_PASSWORD="${PG_PASSWORD}" \
        -e POSTGRES_DB="${PG_DB}" \
        -p "${PG_PORT}:5432" \
        pgvector/pgvector:pg16
fi

echo "==> Waiting for PostgreSQL to be ready..."
for i in $(seq 1 30); do
    if docker exec "${CONTAINER_NAME}" pg_isready -U "${PG_USER}" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "==> Enabling pgvector extension..."
docker exec "${CONTAINER_NAME}" psql -U "${PG_USER}" -d "${PG_DB}" \
    -c "CREATE EXTENSION IF NOT EXISTS vector;"

echo ""
echo "==> PostgreSQL + pgvector is ready!"
echo "    Connection URL: postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}"
echo ""
echo "    Export it:"
echo "    export ORKMIND_DATABASE_URL=\"postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}\""
