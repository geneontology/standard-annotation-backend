# Standard Annotation Backend

The Standard Annotation Backend (SAB) is a Python service for storing and managing Gene
Ontology standard annotations.

## Set up a local development environment

The supported environment uses Docker Compose and
[`just`](https://just.systems/man/en/packages.html). Install Git, a recent version of
Docker with Compose support, and `just`, then confirm `just` is available:

```bash
just --version
```

Clone the repository, enter its directory, and prepare the project:

```bash
just setup
```

This creates `.env` from `.env.example` when needed, builds the application and
development images, and applies database migrations. It does not replace an existing
`.env` file. Local settings live in `.env`; `.env.example` contains development
defaults.

Start the web application, PostgreSQL, Redis, the background worker, and the scheduler:

```bash
just up
```

Check that the application is running:

```bash
curl http://localhost:8000/health
```

The response should be `{"status":"ok"}`. Open Swagger UI at
<http://localhost:8000/docs> or inspect the OpenAPI document at
<http://localhost:8000/openapi.json>.

The web container reloads when files under `src/` change. Restart the worker and
scheduler after changing background-job code. Follow web application logs with:

```bash
just logs
```

Apply new database migrations explicitly:

```bash
just migrate
```

Stop the environment without deleting PostgreSQL data:

```bash
just down
```

## Synchronize authorizations

Synchronize the current go-site authorization state into the local development database
after setup and migrations:

```bash
just sync-authorizations
```

The command prints the synchronized source commit and resulting counts.

## Load configured ontologies

Ontology loads run in the background worker and periodic loads are created by the
scheduler, so both services started by `just up` must be running. Ontology sources use
nested `SAB_ONTOLOGY_SOURCES__<KEY>__<FIELD>` variables. Each ontology entry must
provide its complete source configuration; `.env.example` configures GO with its
source type, GitHub repository, ref, and path. Deployments may instead set
`SAB_ONTOLOGY_SOURCES` to an equivalent JSON object. Each fetched document records its
resolved immutable commit. `SAB_ONTOLOGY_LOAD_CRON` sets the UTC Celery Beat schedule.

An `admin/global` bearer token can request a GO load and then inspect the returned job
through the common jobs API:

```bash
curl -X POST \
  -H "Authorization: Bearer ${SAB_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"ontology":"go"}' \
  http://localhost:8000/ontology-loads
```

## Import entity catalogs

Entity sources are configured in `config/entity-sources.yaml`. A scheduled job
imports every configured source; a global admin can start an import with
`POST /entity-imports` (`{}` for all sources or `{"source_key": "mgi"}` for one).

## Create and manage bearer tokens

Configure a GitHub OAuth app with this callback URL for local development:

```text
http://localhost:8000/auth/github/callback
```

Set its client ID and secret in `.env` as `SAB_GITHUB_OAUTH_CLIENT_ID` and
`SAB_GITHUB_OAUTH_CLIENT_SECRET`. After synchronizing authorizations and starting SAB,
open <http://localhost:8000/token-management> and sign in with an authorized GitHub
account.

The page creates tokens for one of the signed-in user's current authorization contexts,
lists token metadata, and revokes existing tokens. A newly created bearer token is shown
only once, so copy it to secure storage before closing the confirmation.

## Use a bearer token

Keep tokens out of source files, logs, URLs, and shell history. Assuming an issued token
is already stored in `SAB_API_TOKEN`, send it in the `Authorization` header:

```bash
curl -H "Authorization: Bearer ${SAB_API_TOKEN}" \
  http://localhost:8000/annotations
```

Swagger UI also accepts an issued token through its **Authorize** control.

## Run tests

Run the complete test suite:

```bash
just test
```

Pass paths or other pytest arguments after the recipe to run a subset:

```bash
just test tests/unit/test_permissions.py
```

Tests reset the database configured by `SAB_TEST_DATABASE_URL`; always use a disposable
database whose name ends in `_test`.

## Run contributor checks

Run formatting, lint, and static type checks:

```bash
just check
```

Run `just` with no recipe to see all available project commands.

## Versioning

Release tags use `vMAJOR.MINOR.PATCH`, such as `v0.2.0`. Display the version for the
current checkout with:

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
