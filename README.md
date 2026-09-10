# Standard Annotation Backend

The Standard Annotation Backend (SAB) is a Python service for storing and
managing Gene Ontology standard annotations.

## Start a local development environment

The preferred setup uses Docker Compose. You only need Git and a recent version
of Docker with Compose support.

Clone the repository, then create your local settings file:

```bash
cp .env.example .env
```

Build the application image and apply the database migrations:

```bash
docker compose build web
docker compose run --rm web alembic upgrade head
```

Migrations are always run explicitly. Starting the application does not change
the database schema.

Start the web application, PostgreSQL, Redis, and the background worker:

```bash
docker compose up -d
```

Check that the application is running:

```bash
curl http://localhost:8000/health
```

You should receive:

```json
{"status":"ok"}
```

The web container reloads when files under `src/` change, so most Python edits do
not require an image rebuild. Restart the worker after changing worker code.
Follow the web application logs with:

```bash
docker compose logs -f web
```

Stop the environment when you are finished:

```bash
docker compose down
```

This keeps the PostgreSQL data volume. Add `--volumes` only when you intentionally
want to remove your local database data.

## Run the tests

Start PostgreSQL if the development environment is not already running:

```bash
docker compose up -d postgres
```

Run the complete test suite in a disposable application container:

```bash
docker compose run --build --rm --no-deps \
  -e SAB_ENVIRONMENT=testing \
  -e SAB_TEST_DATABASE_URL=postgresql+psycopg://sab:sab@postgres:5432/sab_test \
  --volume "$PWD/tests:/app/tests:ro" \
  web sh -c 'uv sync --locked && uv run --locked pytest -q'
```

The test fixture only uses the database named by `SAB_TEST_DATABASE_URL`. For
safety, `SAB_ENVIRONMENT` must be `testing`, the database name must end in
`_test`, and the URL may not override the database name with a `dbname` query
parameter. Always use a disposable test database because the fixture resets it.

## Run contributor checks

Install [uv](https://docs.astral.sh/uv/) if you want to run the fast checks on
your host machine, then synchronize the locked environment:

```bash
uv sync --locked
```

Run the same formatting, lint, and type checks used in CI:

```bash
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked ty check src tests
```

Local settings live in `.env`. The checked-in `.env.example` contains safe
development defaults. Set `SAB_LOG_FORMAT=console` for readable logs or
`SAB_LOG_FORMAT=json` for structured logs.
