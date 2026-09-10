# Standard Annotation Backend

The Standard Annotation Backend (SAB) is a Python service for storing and
managing Gene Ontology standard annotations.

## Start a local development environment

The preferred setup uses Docker Compose through a small set of project commands.
You need Git, a recent version of Docker with Compose support, and
[`just`](https://just.systems/man/en/packages.html). Follow the linked `just`
installation instructions for your operating system, then confirm the command
is available:

```bash
just --version
```

Clone the repository, enter its directory, and prepare the project:

```bash
just setup
```

This creates `.env` from `.env.example` if it does not already exist, builds the
application and development-tools images, and applies the database migrations.
It never replaces an existing `.env` file.

Migrations are always run explicitly. Starting the application does not change
the database schema. Apply new migrations later with `just migrate`.

Start the web application, PostgreSQL, Redis, and the background worker:

```bash
just up
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
just logs
```

Stop the environment when you are finished:

```bash
just down
```

This keeps the PostgreSQL data volume. Add `--volumes` only when you intentionally
run the underlying Docker Compose command and want to remove your local database
data.

## Run the tests

Run the complete test suite in a disposable development-tools container:

```bash
just test
```

The command builds the tools image and starts PostgreSQL when needed.

The test fixture only uses the database named by `SAB_TEST_DATABASE_URL`. For
safety, `SAB_ENVIRONMENT` must be `testing`, the database name must end in
`_test`, and the URL may not override the database name with a `dbname` query
parameter. Always use a disposable test database because the fixture resets it.

## Run contributor checks

Run the same formatting, lint, and type checks used in CI:

```bash
just check
```

Local settings live in `.env`. The checked-in `.env.example` contains safe
development defaults. Set `SAB_LOG_FORMAT=console` for readable logs or
`SAB_LOG_FORMAT=json` for structured logs.

Run `just` with no recipe to see all available project commands.
