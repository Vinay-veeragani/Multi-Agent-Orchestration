# syntax=docker/dockerfile:1
#
# Two stages: `builder` compiles the package and its dependencies into a
# throwaway virtualenv; `runtime` copies only that venv plus the source the
# application actually needs at runtime (migrations, the benchmark scenario
# library, the CLI) into a slim final image. No compiler, no pip cache, no
# dev tooling (ruff/mypy/pytest) ships in the image that actually runs.

FROM python:3.11-slim AS builder

# Postgres client headers are needed to build asyncpg's C extension on some
# platforms; build-essential covers the rest of the compiled-dependency
# surface. Purged from the final image -- this layer never ships.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
# A minimal README satisfies setuptools' `readme = "README.md"` metadata
# without pulling the real (large, frequently-changing) one into this layer's
# cache key -- editing README.md should not invalidate the dependency install.
RUN echo "agent-orchestration-engine" > README.md
RUN pip install --no-cache-dir .


FROM python:3.11-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system orchestrator \
    && useradd --system --gid orchestrator --create-home --home-dir /home/orchestrator orchestrator

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# The package itself is already installed (non-editable, into /opt/venv) --
# these are the top-level directories the *running* app, the CLI, and
# `alembic` need that are not part of the importable package: migrations,
# the benchmark scenario library and its CLI-writable results directory,
# and Alembic's own config.
COPY alembic.ini ./
COPY migrations ./migrations
COPY benchmarks ./benchmarks

RUN mkdir -p /app/.artifacts /app/benchmarks/results \
    && chown -R orchestrator:orchestrator /app

USER orchestrator

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# No default CMD: docker-compose.yml assigns each service (migrate vs the API
# server) its own command against this same image, rather than this image
# guessing which one it is.
