#!/usr/bin/env bash
# Orgbots and the bots' desktop computer, with one command.
#
#   scripts/orgbots.sh up      build what changed, start the computer, then Orgbots
#   scripts/orgbots.sh down    stop both; the data, sign-ins and files are kept
#   scripts/orgbots.sh logs    follow Orgbots' output
#
# The computer is docker/computer (scripts/computer.sh); Orgbots is the single container
# (Dockerfile), started on the computer's network so it reaches it as http://computer:8020
# instead of running its own headless browser. Settings come from .env when there is one.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME=orgbots
PORT="${ORGBOTS_PORT:-3000}"

up() {
  scripts/computer.sh up

  local computer network
  computer="$(docker compose -f docker-compose.yml ps -q computer)"
  network="$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{end}}' "$computer")"

  docker build -t orgbots .
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  local env_file=()
  if [[ -f .env ]]; then env_file=(--env-file .env); fi
  docker run -d --name "$NAME" --restart unless-stopped \
    -p "$PORT:3000" -v orgbots-data:/data \
    --network "$network" \
    ${env_file[@]+"${env_file[@]}"} \
    -e ORGBOTS_COMPUTER_URL=http://computer:8020 \
    orgbots >/dev/null

  printf 'waiting for Orgbots'
  for _ in $(seq 1 90); do
    if curl -fsS -o /dev/null "http://localhost:$PORT/"; then
      printf '\nOrgbots is running on http://localhost:%s (the desktop: http://127.0.0.1:%s)\n' \
        "$PORT" "${COMPUTER_DESKTOP_PORT:-6080}"
      return 0
    fi
    printf '.'
    sleep 2
  done
  printf '\nOrgbots did not come up; see: docker logs %s\n' "$NAME" >&2
  return 1
}

case "${1:-up}" in
  up) up ;;
  down) docker rm -f "$NAME" >/dev/null 2>&1 || true; scripts/computer.sh down ;;
  logs) docker logs -f "$NAME" ;;
  *)
    echo "usage: $0 up | down | logs" >&2
    exit 2
    ;;
esac
