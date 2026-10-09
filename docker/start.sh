#!/usr/bin/env bash
# Orgbots in one container: PostgreSQL, Redis, the computer (the shared Chromium the
# bots drive), the API, the worker and the web UI, started in order and stopped
# together.
#
# The first boot creates the database cluster and a credential key under
# $ORGBOTS_DATA; every boot runs the migrations. Each process's output is prefixed
# with its name. If any process exits, the rest are stopped and the script exits
# non-zero, so a restart policy brings the whole set back rather than half of it.
#
# Every path and port can be overridden through the environment, so the same script
# also runs outside a container.
set -euo pipefail

DATA="${ORGBOTS_DATA:-/data}"
APP="${ORGBOTS_APP:-/app}"
PY="${ORGBOTS_PYTHON:-$APP/.venv/bin/python}"
UI_DIR="${ORGBOTS_UI_DIR:-$APP/ui}"
PGBIN="${ORGBOTS_PGBIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
PG_PORT="${ORGBOTS_PG_PORT:-5432}"
REDIS_PORT="${ORGBOTS_REDIS_PORT:-6379}"
API_PORT="${ORGBOTS_API_PORT:-8000}"
COMPUTER_PORT="${ORGBOTS_COMPUTER_PORT:-8020}"
UI_PORT="${ORGBOTS_PORT:-3000}"
UI_HOST="${ORGBOTS_HOST:-0.0.0.0}"

log() { printf '%-8s | %s\n' orgbots "$*"; }

mkdir -p "$DATA/postgres" "$DATA/redis" "$DATA/artifacts" "$DATA/secrets" "$DATA/computer"
chmod 700 "$DATA/postgres" "$DATA/secrets"

# --- the processes ------------------------------------------------------------------
declare -A NAME_OF=()
APP_PIDS=()
PG_PID=""
REDIS_PID=""

# Start a process in the background with its output prefixed by its name.
start() {
  local name=$1
  shift
  "$@" > >(sed -u "s/^/$(printf '%-8s' "$name") | /") 2>&1 &
  NAME_OF[$!]=$name
  LAST_PID=$!
}

stop() {
  local signal=$1
  shift
  local pid
  for pid in "$@"; do kill "-$signal" "$pid" 2>/dev/null || true; done
  for pid in "$@"; do
    for _ in $(seq 1 100); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -KILL "$pid" 2>/dev/null || true
  done
}

# The app processes first, then Redis, then PostgreSQL with a fast shutdown, so
# nothing is still writing when the database goes.
stop_all() {
  trap - TERM INT
  log "stopping"
  # Reverse start order: the UI first, the computer last.
  local reversed=()
  local i
  for ((i = ${#APP_PIDS[@]} - 1; i >= 0; i--)); do reversed+=("${APP_PIDS[i]}"); done
  if ((${#reversed[@]})); then stop TERM "${reversed[@]}"; fi
  if [[ -n "$REDIS_PID" ]]; then stop TERM "$REDIS_PID"; fi
  if [[ -n "$PG_PID" ]]; then stop INT "$PG_PID"; fi
  log "stopped"
}
trap 'stop_all; exit 0' TERM INT

wait_for() {
  local what=$1
  shift
  for _ in $(seq 1 120); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  log "$what did not come up"
  stop_all
  exit 1
}

# --- PostgreSQL ---------------------------------------------------------------------
if [[ ! -s "$DATA/postgres/PG_VERSION" ]]; then
  log "first boot: creating the database cluster"
  "$PGBIN/initdb" -D "$DATA/postgres" -U runtime --auth=trust -E UTF8 >/dev/null
fi
# TCP on loopback only; no Unix socket, whose path has a 107-byte limit a deep data
# directory can exceed, and which nothing here uses.
start postgres "$PGBIN/postgres" -D "$DATA/postgres" -p "$PG_PORT" -k "" \
  -c listen_addresses=127.0.0.1 -c max_connections=200
PG_PID=$LAST_PID
wait_for postgres "$PGBIN/pg_isready" -h 127.0.0.1 -p "$PG_PORT" -U runtime -d postgres
if [[ -z "$("$PGBIN/psql" -h 127.0.0.1 -p "$PG_PORT" -U runtime -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname = 'runtime'")" ]]; then
  "$PGBIN/createdb" -h 127.0.0.1 -p "$PG_PORT" -U runtime runtime
fi

# --- Redis --------------------------------------------------------------------------
start redis redis-server --bind 127.0.0.1 --port "$REDIS_PORT" --dir "$DATA/redis" \
  --appendonly yes --save ""
REDIS_PID=$LAST_PID
wait_for redis redis-cli -p "$REDIS_PORT" ping

# --- configuration for the runtime's processes ---------------------------------------
# The login vault's key: from the environment if given, otherwise made once and kept.
if [[ -z "${RUNTIME_CREDENTIAL_KEYS:-}" ]]; then
  if [[ ! -s "$DATA/secrets/credential-keys" ]]; then
    log "first boot: creating the credential key"
    (umask 077 && "$PY" -c \
      "import base64, os; print('k1:' + base64.b64encode(os.urandom(32)).decode())" \
      >"$DATA/secrets/credential-keys")
  fi
  RUNTIME_CREDENTIAL_KEYS="$(cat "$DATA/secrets/credential-keys")"
fi

export RUNTIME_CREDENTIAL_KEYS
export RUNTIME_DATABASE_URL="postgresql+psycopg://runtime@127.0.0.1:$PG_PORT/runtime"
export RUNTIME_REDIS_URL="redis://127.0.0.1:$REDIS_PORT/0"
export RUNTIME_ARTIFACT_BACKEND=fs
export RUNTIME_ARTIFACT_FS_ROOT="$DATA/artifacts"
export RUNTIME_COMPUTER_URL="http://127.0.0.1:$COMPUTER_PORT"
export RUNTIME_COMPUTER_HOST=127.0.0.1
export RUNTIME_COMPUTER_PORT="$COMPUTER_PORT"
export RUNTIME_COMPUTER_PROFILE="$DATA/computer/profile"
export RUNTIME_COMPUTER_WORKSPACE="$DATA/computer/workspace"
export RUNTIME_UI_URL="${RUNTIME_UI_URL:-http://localhost:$UI_PORT}"
# Helper bots are delegations, and a bot waiting on its helper holds a worker slot.
export RUNTIME_DELEGATION_ENABLED="${RUNTIME_DELEGATION_ENABLED:-true}"
export RUNTIME_WORKER_SLOTS="${RUNTIME_WORKER_SLOTS:-4}"
export RUNTIME_LOG_JSON="${RUNTIME_LOG_JSON:-false}"

if [[ -z "${RUNTIME_DEEPSEEK_API_KEY:-}" ]]; then
  log "RUNTIME_DEEPSEEK_API_KEY is not set: the app runs, but bots cannot think until it is"
fi

# --- migrations, then the runtime ---------------------------------------------------
log "running migrations"
if ! (cd "$APP" && "$PY" -m alembic upgrade head) 2>&1 |
  sed -u "s/^/$(printf '%-8s' migrate) | /"; then
  log "migrations failed"
  stop_all
  exit 1
fi

cd "$APP"
start computer "$PY" -m runtime.computer.main
APP_PIDS+=("$LAST_PID")
start api "$PY" -m uvicorn runtime.api.app:app --host 127.0.0.1 --port "$API_PORT" \
  --no-access-log
APP_PIDS+=("$LAST_PID")
start worker "$PY" -m runtime.worker.main
APP_PIDS+=("$LAST_PID")
start ui env PORT="$UI_PORT" HOSTNAME="$UI_HOST" \
  RUNTIME_API_URL="http://127.0.0.1:$API_PORT" node "$UI_DIR/server.js"
APP_PIDS+=("$LAST_PID")

log "Orgbots is starting on http://localhost:$UI_PORT"

# Run until any process exits; then stop the rest and exit with an error.
set +e
wait -n -p EXITED "$PG_PID" "$REDIS_PID" "${APP_PIDS[@]}"
status=$?
set -e
log "${NAME_OF[$EXITED]:-a process} exited with status $status"
stop_all
exit 1
