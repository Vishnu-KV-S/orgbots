#!/usr/bin/env bash
# Dockerless dev stack.
#
# `docker compose up` is the supported path (see docker-compose.yml). This script
# is the fallback for machines without Docker: it initdb's a private Postgres
# cluster and starts a private Redis, both inside .devstack/, both owned by the
# invoking user. No sudo, no system services touched. The artifact store falls
# back to the filesystem backend (ARTIFACT_BACKEND=fs), which is what the
# fail-closed tests exercise anyway.
#
#   scripts/devstack.sh up | down | status | env
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK="${DEVSTACK_DIR:-$ROOT/.devstack}"
PGDATA="$STACK/pgdata"
PGPORT="${DEVSTACK_PG_PORT:-54329}"
REDIS_PORT="${DEVSTACK_REDIS_PORT:-63799}"
PGBIN="${DEVSTACK_PGBIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)}"

if [[ -z "${PGBIN}" || ! -x "$PGBIN/initdb" ]]; then
  echo "no postgres server binaries found; set DEVSTACK_PGBIN" >&2
  exit 1
fi

up() {
  mkdir -p "$STACK"
  if [[ ! -d "$PGDATA" ]]; then
    "$PGBIN/initdb" -D "$PGDATA" -U runtime --auth=trust -E UTF8 >"$STACK/initdb.log" 2>&1
    {
      echo "port = $PGPORT"
      echo "unix_socket_directories = '$STACK'"
      echo "listen_addresses = '127.0.0.1'"
      echo "max_connections = 200"
      echo "fsync = off"              # dev cluster; chaos tests kill workers, not the DB
      echo "synchronous_commit = off"
    } >>"$PGDATA/postgresql.conf"
  fi
  if ! "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
    "$PGBIN/pg_ctl" -D "$PGDATA" -l "$STACK/postgres.log" -w start
  fi
  "$PGBIN/createdb" -h 127.0.0.1 -p "$PGPORT" -U runtime runtime 2>/dev/null || true

  if ! redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1; then
    redis-server --port "$REDIS_PORT" --daemonize yes \
      --dir "$STACK" --dbfilename devstack.rdb --appendonly no \
      --pidfile "$STACK/redis.pid" --logfile "$STACK/redis.log"
    for _ in $(seq 1 50); do
      redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1 && break
      sleep 0.1
    done
  fi

  mkdir -p "$STACK/artifacts"
  env_block
}

down() {
  if "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
    "$PGBIN/pg_ctl" -D "$PGDATA" -m immediate -w stop
  fi
  redis-cli -p "$REDIS_PORT" shutdown nosave 2>/dev/null || true
  echo "devstack down"
}

status() {
  "$PGBIN/pg_ctl" -D "$PGDATA" status || true
  redis-cli -p "$REDIS_PORT" ping || true
}

env_block() {
  cat <<EOF
export RUNTIME_DATABASE_URL="postgresql+psycopg://runtime@127.0.0.1:$PGPORT/runtime"
export RUNTIME_REDIS_URL="redis://127.0.0.1:$REDIS_PORT/0"
export RUNTIME_ARTIFACT_BACKEND="fs"
export RUNTIME_ARTIFACT_FS_ROOT="$STACK/artifacts"
EOF
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  env) env_block ;;
  *) echo "usage: $0 {up|down|status|env}" >&2; exit 2 ;;
esac
