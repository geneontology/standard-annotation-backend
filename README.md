# Standard Annotation Backend

The Standard Annotation Backend (SAB) is a Python service for storing and managing Gene
Ontology standard annotations.

## Start a local development environment

The preferred setup uses Docker Compose through a small set of project commands. You
need Git, a recent version of Docker with Compose support, and
[`just`](https://just.systems/man/en/packages.html). Follow the linked `just`
installation instructions for your operating system, then confirm the command is
available:

```bash
just --version
```

Clone the repository, enter its directory, and prepare the project:

```bash
just setup
```

This creates `.env` from `.env.example` if it does not already exist, builds the
application and development-tools images, and applies the database migrations. It never
replaces an existing `.env` file.

Migrations are always run explicitly. Starting the application does not change the
database schema. Apply new migrations later with `just migrate`.

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
{
  "status": "ok"
}
```

The web container reloads when files under `src/` change, so most Python edits do not
require an image rebuild. Restart the worker after changing worker code. Follow the web
application logs with:

```bash
just logs
```

Stop the environment when you are finished:

```bash
just down
```

This keeps the PostgreSQL data volume. Add `--volumes` only when you intentionally run
the underlying Docker Compose command and want to remove your local database data.

## Run the tests

Run the complete test suite in a disposable development-tools container:

```bash
just test
```

The command builds the tools image and starts PostgreSQL when needed.

The test fixture only uses the database named by `SAB_TEST_DATABASE_URL`. For safety,
`SAB_ENVIRONMENT` must be `testing`, the database name must end in
`_test`, and the URL may not override the database name with a `dbname` query parameter.
Always use a disposable test database because the fixture resets it.

## Run contributor checks

Run the same formatting, lint, and type checks used in CI:

```bash
just check
```

Local settings live in `.env`. The checked-in `.env.example` contains safe development
defaults. Set `SAB_LOG_FORMAT=console` for readable logs or
`SAB_LOG_FORMAT=json` for structured logs.

Run `just` with no recipe to see all available project commands.

## Versioning

SAB uses Git tags as the source of package versions. Hatchling and `hatch-vcs`
generate a [PEP 440](https://peps.python.org/pep-0440/) version when the package is
built. FastAPI reads the installed distribution metadata, so the version shown in
Swagger UI and `/openapi.json` is the version assigned to the running build.

Version tags have the form `vMAJOR.MINOR.PATCH`, such as `v0.2.0`. A build made from
that exact tag has version `MAJOR.MINOR.PATCH`. Later commits receive a development
version containing their distance from the tag and Git revision. A dirty checkout also
receives a date-qualified local version.

Display the version generated for the current checkout:

```bash
just version
```

## Architecture

SAB separates HTTP handling, application operations, and database access into layers:

```text
HTTP route → service → unit of work → repository → PostgreSQL
```

Routes in `src/standard_annotation_backend/api/routes/` are grouped into modules named
after the resource they expose. They translate between HTTP and the application by
parsing paths, query parameters, headers, and request bodies; calling a service; and
constructing the appropriate response. HTTP concepts such as status codes and `ETag`
headers should remain in this layer.

Services carry out complete application operations, such as creating an annotation or
reading its version history. They validate input, coordinate calls to repositories,
record the identity responsible for a change, and commit only after the entire operation
succeeds. A unit of work provides a service with repositories that share one database
transaction and rolls back uncommitted changes when the operation fails.

Repositories contain the persistence details. They build SQLAlchemy queries, read and
write database records, maintain version and search records, and use database locks when
an operation must remain correct under concurrent requests. They may flush changes so
database errors appear promptly, but the calling service decides when to commit the
transaction.

When deciding where new code belongs:

- Put HTTP parsing and response formatting in a route.
- Put operation workflow, validation, and transaction coordination in a service.
- Put SQL, record storage, querying, and concurrency control in a repository.
- Put rules that require neither HTTP nor a database in the domain layer.

Some rules cross a layer boundary. Duplicate prevention, for example, is part of the
application's behavior, but its database check and write must occur together under a
repository lock to prevent two concurrent requests from creating the same annotation.
