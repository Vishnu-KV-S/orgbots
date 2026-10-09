# syntax=docker/dockerfile:1.7
#
# The bots' computer: a Linux desktop with Google Chrome on it, and the service the
# runtime drives it through (`runtime.computer`). Like GrokBot's cloud computer — one
# machine every bot shares, a window each, logins that persist.
#
#   docker compose up -d computer       # the runtime finds it at RUNTIME_COMPUTER_URL
#   http://127.0.0.1:6080               # the whole desktop, in a browser tab
#
# What runs inside (docker/computer/supervisord.conf):
#   display   TigerVNC's Xvnc — an X server whose screen is also a VNC server
#   windows   openbox — every window maximised, no title bars
#   desktop   noVNC — the VNC screen in a browser tab
#   computer  the service; it starts Chrome itself, one process per browser profile
#
# The same image is also the bots' shell (`python -m runtime.computer.shell`), the
# sibling container their commands run in, apart from the browser (docker-compose.yml).
#
# Chrome comes from Google's own apt repository, and only a rebuild updates it — do it
# for security releases:  docker compose build --pull computer

ARG UBUNTU=24.04
FROM ubuntu:${UBUNTU}

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The desktop, graphics drivers (Mesa's, for a GPU passed in: docker/computer/gpu.yml),
# fonts a real machine has (a page that finds none looks broken, and odd), and the tools
# a bot's terminal is likely to want.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg locales tzdata dbus-x11 xdg-utils util-linux \
      tigervnc-standalone-server openbox supervisor \
      mesa-vulkan-drivers libgl1-mesa-dri libegl1 \
      fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji \
      python3.12 python3-pip python3-venv bubblewrap \
      git jq less nano file zip unzip wget ripgrep \
 && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen && locale-gen \
 && tr -d '-' < /proc/sys/kernel/random/uuid > /etc/machine-id \
 && install -d -m 1777 /tmp/.X11-unix \
 && rm -rf /var/lib/apt/lists/*

# noVNC (the whole desktop in a browser tab), from its own releases: Ubuntu's package
# brings Node.js, numpy and OpenStack's configuration library with it. websockify's
# numpy, requests, jwcrypto and redis are for speed-ups and token plugins it imports only
# if they are there.
ARG NOVNC_VERSION=1.7.0
ARG WEBSOCKIFY_VERSION=0.13.0
RUN curl -fsSL "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
      | tar -xz -C /opt \
 && mv "/opt/noVNC-${NOVNC_VERSION}" /opt/novnc \
 && ln -s vnc.html /opt/novnc/index.html \
 && python3 -m venv /opt/websockify \
 && /opt/websockify/bin/pip install --no-cache-dir --no-deps "websockify==${WEBSOCKIFY_VERSION}"

# Google Chrome, signed by Google's key.
RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends google-chrome-stable \
 && rm -rf /var/lib/apt/lists/*

# The service's Python packages, from the repository's lock file — the same versions the
# runtime is tested with. Only `runtime.computer` is copied in, and it imports nothing
# else from the runtime (.importlinter).
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /opt/computer
COPY pyproject.toml uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/opt/computer/venv UV_PYTHON_DOWNLOADS=never \
    uv sync --frozen --no-dev --extra computer --no-install-project --python /usr/bin/python3.12 \
 && rm -rf /root/.cache
COPY src/runtime/__init__.py src/runtime/__init__.py
COPY src/runtime/computer src/runtime/computer

# Ubuntu's image comes with a user at uid 1000; the computer's own takes its place.
RUN userdel --remove ubuntu 2>/dev/null; \
    useradd --create-home --uid 1000 --shell /bin/bash computer \
 && mkdir -p /data/profiles /data/workspaces /run/shell \
 && chown computer:computer /data/profiles /data/workspaces /run/shell

COPY docker/computer/supervisord.conf /etc/computer/supervisord.conf
COPY docker/computer/openbox.xml /etc/computer/openbox.xml
COPY docker/computer/chrome-policies.json /etc/opt/chrome/policies/managed/computer.json
COPY docker/computer/start-computer /usr/local/bin/start-computer

ENV PATH=/opt/computer/venv/bin:$PATH \
    PYTHONPATH=/opt/computer/src \
    DISPLAY=:1 \
    COMPUTER_SCREEN=1440x900 \
    COMPUTER_BROWSER=desktop \
    COMPUTER_CHROME_PATH=/usr/bin/google-chrome \
    RUNTIME_COMPUTER_HOST=0.0.0.0 \
    RUNTIME_COMPUTER_PORT=8020 \
    RUNTIME_COMPUTER_PROFILE=/data/profiles/default \
    RUNTIME_COMPUTER_WORKSPACE=/data/workspaces/workspace

USER computer
EXPOSE 8020 6080
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8020/healthz || exit 1
CMD ["supervisord", "-n", "-c", "/etc/computer/supervisord.conf"]
