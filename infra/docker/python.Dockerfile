# syntax=docker/dockerfile:1.7
#
# One Dockerfile for every Python service. Each image installs only its own
# package and that package's dependencies (`uv sync --package`), from the
# committed lock file (`--frozen`), so images are small and reproducible.
#
#   docker build -f infra/docker/python.Dockerfile \
#     --build-arg PACKAGE=mara-orchestrator --build-arg MODULE=mara_orchestrator -t mara/orchestrator .

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ARG PACKAGE
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY packages ./packages
COPY apps ./apps
COPY mcp-servers ./mcp-servers
COPY agents ./agents
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

FROM python:3.12-slim-bookworm
ARG MODULE
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/.secrets /data \
    && chown app:app /app/.secrets /data
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app config ./config
COPY --chown=app:app mcp-tools.lock.json ./
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MARA_ROOT=/app \
    MODULE="${MODULE}"
USER app
CMD ["sh", "-c", "exec python -m ${MODULE}"]
