# Repository guidance

These instructions apply throughout the repository.

## Project overview

The Standard Annotation Backend (SABE) stores and manages Gene Ontology Standard
Annotations. It is a Python 3.13 application built with FastAPI, Pydantic, SQLAlchemy,
PostgreSQL, Alembic, Celery, and Redis.

Prefer the existing project commands and architectural boundaries over introducing new
tools or alternate patterns for the same work.

## Project setup and common commands

The supported development environment uses Docker Compose and `just`.

- Run `just setup` to create `.env` when needed, build the containers, and apply
  migrations.
- Run `just up` to start the web application, PostgreSQL, Redis, and the worker.
- Run `just logs` to follow web application logs.
- Run `just migrate` after adding or receiving a database migration.
- Run `just refresh KIND [SOURCE]` to refresh reference data (authorization, ontology,
  entity, or annotation) from `config/sources.yaml`.
- Run `just test` to execute the complete test suite with PostgreSQL available.
- Run `just check` to check formatting, lint, and static types.
- Run `just down` to stop services without deleting local database data.

Database migrations are always explicit. Do not make application startup alter the
database schema.

Generate new revisions with Alembic. Do not hand-author revision IDs or migration
filenames; let Alembic generate the `revision` value and apply the `file_template`
configured in `alembic.ini`. Run `uv run alembic revision -m "<message>"` with a short
message; Alembic builds the filename from it.

- A new table must be added to `APPLICATION_TABLES` in `tests/integration/conftest.py`
  so that integration tests reset it.
- A new job type needs a `JobType` member, a migration that drops and recreates the
  `ck_job_type_allowed` check constraint with the new value (as `03a2094d9969` does),
  a dispatch-failure message in `domain/jobs.py`, and a runner that claims the type.
  `JobExecutors` refuses to start when a job type has no runner.
- To check a migration's downgrade, round-trip it on the disposable test database:
  `docker compose run --build --rm tools sh -c "uv run --locked --no-sync alembic
  downgrade <rev> && uv run --locked --no-sync alembic upgrade head"`. Never downgrade
  the development database; doing so deletes local data.

## Deployment status

SABE is not deployed yet. The local Docker Compose environment is the only SABE
environment, and it holds no data that must be preserved. Until that changes:

- Prefer internal consistency over backward compatibility. Do not design for rolling
  deploys or for workers from two releases sharing one database.
- A migration may clear data when that is simpler than converting it.
- Advisory lock key values (`LockNamespace`) and Celery task names may change between
  releases, provided every process is restarted together.

## Application architecture

Code follows this general dependency flow:

```text
HTTP route → service → unit of work → repository → PostgreSQL
```

Routes in `src/standard_annotation_backend/api/routes/` translate between HTTP and the
application. Name route modules after the resource they expose. They parse request data,
headers, paths, and query parameters; invoke services; and construct responses. Keep
HTTP status codes, response headers, FastAPI dependencies, and API request or response
models in this layer.

Services in `src/standard_annotation_backend/services/` implement complete application
operations. They validate untrusted input, coordinate repository calls, attach request
identity to recorded changes, translate persistence records into application results,
and commit only after the complete operation succeeds.

The unit of work provides repositories that share one database transaction. It rolls
back uncommitted changes and closes the session when an operation ends. Services decide
when to commit; repositories must not commit independently.

Repositories in `src/standard_annotation_backend/persistence/` contain database-facing
behavior. Put SQLAlchemy queries, record creation and updates, database-backed filters,
locking, and concurrency-sensitive enforcement here. Repositories may flush changes so
database errors appear promptly, but their caller owns the transaction.

Keep rules that require neither HTTP nor database access in
`src/standard_annotation_backend/domain/`. Domain rules should be usable without a web
request or a database session.

Some rules span layers. The service decides when an operation requires the rule, while
the repository safely enforces any portion that depends on current database state. For
example, duplicate prevention is application behavior, but its database check and write
must occur together under repository-managed locks.

When adding code:

- Use a route for HTTP parsing, response formatting, and public API metadata.
- Use a service for workflow, validation, application results, and transaction
  coordination.
- Use a repository for SQL, storage, querying, locks, and database-dependent rules.
- Use the domain layer for pure models and rules.

Do not make services construct SQLAlchemy queries or manipulate sessions directly. Do
not make repositories depend on FastAPI, HTTP headers, or API response models.

## Coding conventions

- Support Python 3.13 and provide type annotations for new and changed Python code.
- Follow the Ruff formatting and lint configuration in `pyproject.toml`.
- Use constants from `fastapi.status` instead of bare integers for HTTP status codes.
- Use Pydantic models at untrusted data boundaries and reuse the schema-generated
  Standard Annotation model rather than duplicating it.
- Use Pydantic models for untrusted input, public API schemas, and other runtime
  validation or serialization boundaries. Use frozen, slotted dataclasses for trusted
  internal value objects. Reserve `TypedDict` for values that are intentionally
  dictionary-shaped, such as normalized JSON-compatible records.
- Preserve stable public error envelopes and machine-readable error codes. When a public
  route changes, update its OpenAPI declaration and contract tests together.
- Use Alembic for database schema changes. Add constraints and indexes when correctness
  or supported query behavior depends on them.
- Convert SQLAlchemy records to service results inside the unit of work. Records are
  detached once it closes, and reading one then raises `DetachedInstanceError`, which
  surfaces as a `500`.
- `Settings` and the parsed sources file are cached per process. Code or tests that
  change configuration must call `get_settings.cache_clear()`.
- Run `ruff check --fix` only after every new module exists; Ruff sorts an import of a
  module it cannot find as third-party. In migrations, put `from alembic import op` in
  its own group after the `sqlalchemy` imports, because Ruff in the tools container
  treats the repository's `alembic/` directory as first-party.
- Keep transaction and locking behavior explicit. Concurrency rules must remain correct
  when requests use independent database connections.

## Docstrings and developer-facing text

Write docstrings in Google style using plain, direct language. Assume the reader
understands Python but may not know the surrounding codebase.

- Begin with a concise description of what the module, class, method, or function does.
- Explain why behavior matters when the purpose is not clear from the signature.
- Use single backticks for code references. Do not use double backticks or other
  reStructuredText syntax.
- Use `Args:`, `Returns:`, `Yields:`, `Raises:`, and `Attributes:` sections when they
  add information a caller or maintainer needs.
- Describe externally visible behavior and important guarantees, not a line-by-line
  restatement of the implementation.
- Avoid unexplained internal shorthand and architectural jargon. When a technical term
  is necessary, explain it where it first appears.
- Write test docstrings as statements of the behavior being verified. Do not describe
  them only in terms of how a broken implementation would fail.

Keep API messages and other developer-facing text clear and stable. Tests should assert
stable structure and meaning without unnecessarily depending on third-party wording.

Keep `README.md` focused primarily on setting up and using SABE as a developer. Document
supported commands and day-to-day workflows, but do not duplicate implementation rules,
data-model details, API error contracts, security rationale, or edge-case behavior there.
Limit discussion of how SABE works to a brief `Architecture` section that links to
`ARCHITECTURE.md`. Put detailed code behavior and guarantees in docstrings beside the
relevant implementation. Avoid describing missing capabilities, rejected alternatives,
temporary omissions, future milestones, or release operations in the README.

Define each SABE-specific term, and each GO term that SABE uses in a specific way, in
`docs/glossary.md`. Keep each entry to a short definition that links to where the term
is explained, and update the entry when a term is added, renamed, or changes meaning.

## Testing and verification

Add focused regression coverage for every behavior change. Tests should check what a
client or caller can observe: HTTP responses and error codes, job records and results,
and what a later read, write, or validation returns.

- Test observable behavior and outcomes rather than merely introspecting implementation
  structure or checking that a class, table, column, route, or symbol exists. Schema and
  contract assertions should demonstrate a consumer-visible guarantee or an enforced
  rule; existence alone is not meaningful regression coverage.
- Do not test migrations directly. A behavioral test that fails when a migration was
  not applied is enough.
- Tests of internal details, such as private helpers or a migration, may be useful
  while developing a change. Before handing off, remove each one unless it checks
  something a caller could observe that no other test covers.
- Use unit tests for domain rules, service orchestration, parsing, and error translation
  that can be checked without external services.
- Use integration tests for SQL queries, database constraints, transaction behavior,
  locking, concurrency, and end-to-end HTTP behavior backed by PostgreSQL.
- Use independent PostgreSQL connections when a test claims to verify concurrent
  transactions.
- Update OpenAPI contract tests when routes, parameters, response headers, models, or
  documented errors change.

Integration tests may reset their configured database. `SABE_ENVIRONMENT` must be
`testing`, the database name must end in `_test`, and the URL must not override the
database name with a `dbname` query parameter. Always use a disposable test database.

Run focused tests while developing. Before handing off a completed change, run
`just check`, `just test`, and `git diff --check`.

- `just test <paths>` runs the named test files and passes other pytest options
  through. Select tests by file path; `-k` expressions have failed to collect tests.
- Unit tests need no services: `uv run pytest tests/unit -q`.
- Every integration test uses the same `sabe_test` database, so never run two
  integration test processes at once.
- An API test that gets an unexpected `500` hides the exception. Run pytest with `-l`
  to see local variables, or call the service directly to see the error.
- `just test` and `just check` rebuild the tools image on every run, and the old images
  accumulate until Docker runs out of space ("No space left on device" during
  `uv sync`). `docker image prune -f` frees the space; ask the developer before
  running it.
