# Standard Annotation Backend

Python 3.13 service foundation for the Standard Annotation Backend (SAB).

## Local development

Install [uv](https://docs.astral.sh/uv/) and synchronize the locked environment:

```bash
uv sync --locked
```

Before running locally or starting Docker Compose, copy the required local
settings file:

```bash
cp .env.example .env
```

Docker Compose loads shared settings from `.env` for both application services,
while overriding only database and Redis URLs with its internal `postgres` and
`redis` service DNS addresses. The database and Redis URLs in `.env.example`
remain for processes run directly on the host.

Set `SAB_LOG_FORMAT=console` for readable local-development logs or
`SAB_LOG_FORMAT=json` for machine-readable logs in deployed environments. Log
format selection is independent of `SAB_ENVIRONMENT`; application, Uvicorn,
and Celery records all use the selected renderer.

Run quality checks and tests:

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check src tests
uv run pytest
```

## Containers

Start the web service, PostgreSQL, Redis, and the Celery worker:

```bash
docker compose up --build -d
```

The health endpoint does not depend on PostgreSQL or Redis being ready, so it
can respond while those services finish starting:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status": "ok"}
```

Stop the local stack without removing its PostgreSQL volume:

```bash
docker compose down
```
