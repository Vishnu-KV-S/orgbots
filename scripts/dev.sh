#!/usr/bin/env bash
# Orgbots for development, with one command: the infrastructure in Docker, the app on
# this machine with hot reload.
#
#   scripts/dev.sh          start everything; Ctrl+C stops the app, Docker keeps running
#   scripts/dev.sh down     stop the Docker side too: Postgres, Redis and the computer
#
# Docker runs Postgres and Redis (docker-compose.yml) and the bots' desktop computer
# (scripts/computer.sh). This machine runs the API (uvicorn --reload), the worker
# (restarted by watchfiles on a change under src/) and the UI (next dev), so an edit
# shows up without a rebuild. Settings come from .env; the vault key and the artifacts
# live in .devstack/.
set -euo pipefail
cd "$(dirname "$0")/.."

log() { printf '%-8s | %s\n' dev "$*"; }

case "${1:-up}" in
  up) ;;
  down)
    docker compose -f docker-compose.yml stop postgres redis
    scripts/computer.sh down
    exit 0
    ;;
  *)
    echo "usage: $0 [up | down]" >&2
    exit 2
    ;;
esac

need() {
  if ! command -v "$1" >/dev/null; then
    log "$1 is not installed: $2"
    exit 1
  fi
}
need docker "install Docker Desktop and open it"
need uv "https://docs.astral.sh/uv/"
need npm "brew install node"

for port in 3000 8000; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    log "port $port is in use (the orgbots container? docker rm -f orgbots)"
    exit 1
  fi
done

# --- Docker: Postgres, Redis, the computer -------------------------------------------
log "starting Postgres, Redis and the computer in Docker"
docker compose -f docker-compose.yml up -d --wait postgres redis
scripts/computer.sh up

# --- dependencies, only when they changed --------------------------------------------
log "Python dependencies"
uv sync --frozen --extra dev --extra computer --quiet
if [[ "$(uname -s)" == Darwin ]]; then
  # uv marks .venv hidden on macOS and its files inherit the flag, and Python skips a
  # hidden .pth file, so the editable install of runtime/ would not be importable.
  chflags nohidden .venv/lib/python3*/site-packages/*.pth
fi
if [[ ! -f ui/node_modules/.package-lock.json ||
  ui/package-lock.json -nt ui/node_modules/.package-lock.json ]]; then
  log "UI dependencies"
  (cd ui && npm ci --no-audit --no-fund)
fi

# --- configuration -------------------------------------------------------------------
PY=.venv/bin/python
mkdir -p .devstack/artifacts
if [[ -z "${RUNTIME_CREDENTIAL_KEYS:-}" ]]; then
  if [[ ! -s .devstack/credential-keys ]]; then
    (umask 077 && "$PY" -c \
      "import base64, os; print('k1:' + base64.b64encode(os.urandom(32)).decode())" \
      >.devstack/credential-keys)
  fi
  RUNTIME_CREDENTIAL_KEYS="$(cat .devstack/credential-keys)"
fi
export RUNTIME_CREDENTIAL_KEYS
export RUNTIME_ARTIFACT_BACKEND=fs
export RUNTIME_ARTIFACT_FS_ROOT="$PWD/.devstack/artifacts"
export RUNTIME_DELEGATION_ENABLED="${RUNTIME_DELEGATION_ENABLED:-true}"
export RUNTIME_WORKER_SLOTS="${RUNTIME_WORKER_SLOTS:-4}"
export RUNTIME_LOG_JSON="${RUNTIME_LOG_JSON:-false}"

log "running migrations"
"$PY" -m alembic upgrade head

# --- the app, with hot reload --------------------------------------------------------
# Every process is in this script's process group: Ctrl+C reaches all of them, and so
# does the kill on the way out if one of them takes the script down.
trap 'trap - EXIT INT TERM; kill 0 2>/dev/null' EXIT INT TERM

run() {
  local name=$1
  shift
  "$@" 2>&1 | sed -u "s/^/$(printf '%-8s' "$name") | /" &
}

run api "$PY" -m uvicorn runtime.api.app:app --port 8000 --no-access-log \
  --reload --reload-dir src
run worker "$PY" -m watchfiles --filter python "$PY -m runtime.worker.main" src
run ui env RUNTIME_API_URL=http://127.0.0.1:8000 npm --prefix ui run dev

log "Orgbots: http://localhost:3000   the computer's desktop: http://127.0.0.1:6080"
log "edits under src/ and ui/ reload by themselves; Ctrl+C to stop"
wait
