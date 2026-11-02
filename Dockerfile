# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

FROM base AS builder

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/ongma-venv

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

FROM base AS runtime

LABEL org.opencontainers.image.title="Ongma" \
      org.opencontainers.image.description="Gerenciador leve de automações Python" \
      org.opencontainers.image.source="https://github.com/alvesmagnocavalcante/gerenciador"

WORKDIR /app

RUN useradd --create-home --uid 10001 ongma

COPY --from=builder /opt/ongma-venv /opt/ongma-venv
COPY --chown=ongma:ongma app ./app
COPY --chown=ongma:ongma runtime ./runtime
COPY --chown=ongma:ongma migrations ./migrations
COPY --chown=ongma:ongma worker ./worker
COPY --chown=ongma:ongma alembic.ini main.py pyproject.toml uv.lock ./

RUN mkdir -p /app/data/automations \
    && chown -R ongma:ongma /app/data

ENV PATH="/opt/ongma-venv/bin:${PATH}" \
    DATA_DIR=/app/data \
    AUTOMATIONS_DIR=/app/data/automations \
    DB_PATH=/app/data/app.db \
    PORT=8000

VOLUME ["/app/data"]
EXPOSE 8000
USER ongma
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/login', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
