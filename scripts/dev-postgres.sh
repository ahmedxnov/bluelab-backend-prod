#!/usr/bin/env bash
# Local PostgreSQL 16 for the integration suites.
#
# infra/00 §1 specifies Docker `postgres:16` for local, and docker-compose.yml
# defines it alongside Valkey, MinIO and Mailpit. This script is the WSL
# alternative for a machine without Docker — same engine version, same roles.
#
# Run as the postgres superuser:
#   wsl -d Ubuntu-22.04 -- bash -lc "sudo -u postgres bash <path>/dev-postgres.sh"
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PORT="${PGPORT:-5433}"
DATABASE_NAME="bluelab_test"

pg_createcluster 16 main --start --port "$PORT" 2>/dev/null || echo "cluster 16/main already exists"

# The database must exist before the shared role bootstrap can connect to it.
# Ownership is transferred to the migration role by bootstrap.sql.
psql -p "$PORT" -tAc "select 1 from pg_database where datname='$DATABASE_NAME'" | grep -q 1 \
  || psql -p "$PORT" -c "create database $DATABASE_NAME;"

# One executable role definition is used by WSL, Docker, and CI. It converges an
# old cluster too, including removing an inherited superuser grant from bluelab.
psql -p "$PORT" -d "$DATABASE_NAME" -v ON_ERROR_STOP=1 \
  -v migration_role=bluelab \
  -v app_role=bluelab_app \
  -v role_password=bluelab \
  -v database_name="$DATABASE_NAME" \
  -f "$BACKEND_ROOT/sql/roles/bootstrap.sql"

echo "OK — PostgreSQL 16 on port $PORT, database $DATABASE_NAME"
psql -p "$PORT" -tAc "select version();"
