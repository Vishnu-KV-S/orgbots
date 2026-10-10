#!/usr/bin/env bash
# The bots' computer (docker/computer): build it, start it, stop it, look at it.
#
#   scripts/computer.sh up        build if needed, then start the computer and its shell
#   scripts/computer.sh down      stop them; browser profiles and workspaces are kept
#   scripts/computer.sh logs      follow their output
#   scripts/computer.sh rebuild   newest Ubuntu packages and Chrome, then restart
#   scripts/computer.sh desktop   open the whole desktop in a browser tab
#
# Passes this machine's GPU in when it has one (docker/computer/gpu.yml), and on a Mac
# runs it with the Mac's time zone (docker/computer/mac.yml). The ports move
# with COMPUTER_PORT and COMPUTER_DESKTOP_PORT (8020, 6080); COMPOSE_PROJECT_NAME runs a
# second computer with its own profiles beside the first.
set -euo pipefail
cd "$(dirname "$0")/.."

files=(-f docker-compose.yml)
if compgen -G "/dev/dri/renderD*" >/dev/null && getent group render >/dev/null; then
  COMPUTER_RENDER_GID="${COMPUTER_RENDER_GID:-$(getent group render | cut -d: -f3)}"
  export COMPUTER_RENDER_GID
  files+=(-f docker/computer/gpu.yml)
fi
if [[ "$(uname -s)" == Darwin ]]; then
  # /etc/localtime -> /var/db/timezone/zoneinfo/Europe/London
  TZ="${TZ:-$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')}"
  export TZ
  files+=(-f docker/computer/mac.yml)
fi
open_url() { if command -v xdg-open >/dev/null; then xdg-open "$1"; else open "$1"; fi; }
compose() { docker compose "${files[@]}" "$@"; }

case "${1:-up}" in
  up) compose up -d --build computer shell ;;
  down) compose stop computer shell && compose rm -f computer shell ;;
  logs) compose logs -f computer shell ;;
  rebuild) compose build --pull computer && compose up -d computer shell ;;
  desktop) open_url "http://127.0.0.1:${COMPUTER_DESKTOP_PORT:-6080}/vnc.html?autoconnect=1&resize=scale" ;;
  *)
    echo "usage: $0 up | down | logs | rebuild | desktop" >&2
    exit 2
    ;;
esac
