# syntax=docker/dockerfile:1

# python@3.13.15
FROM python@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS base

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src"

RUN pip install --no-cache-dir uv==0.12.9

RUN groupadd --gid 10001 sab \
    && useradd --uid 10001 --gid sab --create-home --shell /usr/sbin/nologin sab \
    && chown -R sab:sab /app

USER sab

FROM base AS vcs-build-base

USER root

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && git config --system --add safe.directory /source \
    && rm -rf /var/lib/apt/lists/*

USER sab

FROM vcs-build-base AS development-dependencies

RUN --mount=type=bind,source=.,target=/source,readonly \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    uv sync --project /source --locked --no-editable

FROM vcs-build-base AS runtime-dependencies

RUN --mount=type=bind,source=.,target=/source,readonly \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    uv sync --project /source --locked --no-dev --no-editable

FROM development-dependencies AS development

COPY --chown=sab:sab pyproject.toml uv.lock README.md ./
COPY --chown=sab:sab src ./src
COPY --chown=sab:sab alembic.ini ./
COPY --chown=sab:sab alembic ./alembic

FROM base AS runtime

COPY --from=runtime-dependencies --chown=sab:sab /app/.venv /app/.venv
COPY --chown=sab:sab pyproject.toml uv.lock README.md ./
COPY --chown=sab:sab src ./src
COPY --chown=sab:sab alembic.ini ./
COPY --chown=sab:sab alembic ./alembic

EXPOSE 8000

CMD ["uvicorn", "standard_annotation_backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-config", "src/standard_annotation_backend/uvicorn_logging.json"]
