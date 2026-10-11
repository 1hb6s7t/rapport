# syntax=docker/dockerfile:1
# Stage 1: build the frontend
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Stage 2: python runtime
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    RAPPORT_DATA_DIR=/data RAPPORT_HOST=0.0.0.0 RAPPORT_ALLOWED_HOSTS='["127.0.0.1","localhost"]'
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/rapport ./rapport
COPY --from=web /app/backend/rapport/static ./rapport/static
RUN uv sync --frozen --no-dev
RUN useradd -m -u 10001 rapport && mkdir -p /data && chown rapport:rapport /data
USER rapport
VOLUME ["/data"]
EXPOSE 7860
# The container listens on 0.0.0.0 internally; compose.yaml maps it to 127.0.0.1 on the host only.
CMD ["uv", "run", "--no-sync", "rapport", "serve"]
