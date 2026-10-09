# syntax=docker/dockerfile:1.7
#
# Orgbots in a single container: PostgreSQL, Redis, the shared Chromium the bots
# drive, the API, the worker and the web UI. docker/start.sh starts them in order and
# stops them together; everything they keep lives in /data.
#
#   docker build -t orgbots .
#   docker run -d --name orgbots -p 3000:3000 -v orgbots-data:/data \
#     -e RUNTIME_DEEPSEEK_API_KEY=sk-... orgbots
#
# then open http://localhost:3000.

# --- the web UI, built to a self-contained Node server ----------------------------
FROM node:22-bookworm-slim AS ui
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build \
 && cp -r public .next/standalone/ \
 && cp -r .next/static .next/standalone/.next/

# --- the runtime ------------------------------------------------------------------
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    NEXT_TELEMETRY_DISABLED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql redis-server tini ca-certificates libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
COPY --from=ui /usr/local/bin/node /usr/local/bin/node

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra computer --no-install-project

COPY alembic.ini ./
COPY config ./config
COPY src ./src
RUN uv sync --frozen --no-dev --extra computer

# The browser the bots drive, with the system libraries and fonts it needs.
RUN .venv/bin/playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ui /ui/.next/standalone ./ui
COPY docker/start.sh ./docker/start.sh

RUN useradd --create-home --uid 1000 orgbots \
 && mkdir -p /data \
 && chown orgbots:orgbots /data \
 && chmod -R a+rX /ms-playwright \
 && chmod +x docker/start.sh

USER orgbots
VOLUME /data
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/', timeout=4)"]

ENTRYPOINT ["tini", "--", "/app/docker/start.sh"]
