#!/usr/bin/env bash
# Provision the pgvector extension inside the LIVE lescall-pg container.
#
# Why in-place: lescall-pg (postgres:16-alpine) must NEVER be recreated (it holds
# billing + call records; see deploy/docker-compose.postgres.yml). The stock image
# ships PostgreSQL server headers and pg_config, so we compile pgvector from source
# against the exact running binary (PG 16.14, musl/Alpine 3.21) and install into the
# container's writable layer. Survives restarts; lost only on container recreation —
# if that ever happens (disaster recovery), re-run this script afterwards.
#
# Idempotent: safe to re-run; skips the build if the extension is already available.
#
# Usage: bash deploy/pg_provision_pgvector.sh
set -euo pipefail

CONTAINER=lescall-pg
PGVECTOR_VERSION=0.8.1

echo "== pgvector provision (${CONTAINER}, v${PGVECTOR_VERSION}) =="

if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
  echo "ERROR: container ${CONTAINER} is not running" >&2
  exit 1
fi

available=$(docker exec "${CONTAINER}" sh -c \
  "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -tA -c \"SELECT count(*) FROM pg_available_extensions WHERE name='vector'\"")

if [ "${available}" = "0" ]; then
  echo "-- building pgvector ${PGVECTOR_VERSION} from source inside the container"
  docker exec "${CONTAINER}" sh -ec "
    apk add --no-cache --virtual .pgvector-build gcc make musl-dev >/dev/null
    cd /tmp
    rm -rf pgvector-${PGVECTOR_VERSION} pgvector.tar.gz
    wget -q -O pgvector.tar.gz https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz
    tar xzf pgvector.tar.gz
    cd pgvector-${PGVECTOR_VERSION}
    # with_llvm=no: skip bitcode; the official alpine image has no LLVM toolchain
    make with_llvm=no >/dev/null
    make with_llvm=no install >/dev/null
    cd /tmp && rm -rf pgvector-${PGVECTOR_VERSION} pgvector.tar.gz
    apk del .pgvector-build >/dev/null
  "
  echo "-- build complete"
else
  echo "-- pgvector already available, skipping build"
fi

echo "-- creating extensions (vector, pg_trgm, unaccent)"
docker exec "${CONTAINER}" sh -c \
  "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -v ON_ERROR_STOP=1 \
     -c 'CREATE EXTENSION IF NOT EXISTS vector' \
     -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm' \
     -c 'CREATE EXTENSION IF NOT EXISTS unaccent'"

docker exec "${CONTAINER}" sh -c \
  "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -tA -c \
   \"SELECT extname || ' ' || extversion FROM pg_extension WHERE extname IN ('vector','pg_trgm','unaccent') ORDER BY extname\""

echo "== done =="
