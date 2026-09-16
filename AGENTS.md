# Repository guidance

These instructions apply throughout the repository.

## Project overview

The Standard Annotation Backend (SAB) stores and manages Gene Ontology Standard
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
- Run `just test` to execute the complete test suite with PostgreSQL available.
- Run `just check` to check formatting, lint, and static types.
- Run `just down` to stop services without deleting local database data.

Database migrations are always explicit. Do not make application startup alter the
database schema.

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

Keep `README.md` focused on current, supported behavior and workflows. Avoid describing
missing capabilities, rejected alternatives, temporary omissions, or future milestones.
Include future-state context only when it is necessary to understand or use current
behavior.

## Testing and verification

Add focused regression coverage for every behavior change.

- Use unit tests for domain rules, service orchestration, parsing, and error translation
  that can be checked without external services.
- Use integration tests for SQL queries, database constraints, migrations, transaction
  behavior, locking, concurrency, and end-to-end HTTP behavior backed by PostgreSQL.
- Use independent PostgreSQL connections when a test claims to verify concurrent
  transactions.
- Update OpenAPI contract tests when routes, parameters, response headers, models, or
  documented errors change.

Integration tests may reset their configured database. `SAB_ENVIRONMENT` must be
`testing`, the database name must end in `_test`, and the URL must not override the
database name with a `dbname` query parameter. Always use a disposable test database.

Run focused tests while developing. Before handing off a completed change, run
`just check`, `just test`, and `git diff --check`.
