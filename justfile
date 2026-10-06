# List the commands available for local development.
set default-list

# Prepare local settings, build the development images, and migrate the database.
setup: _ensure-env build
    docker compose run --rm web alembic upgrade head

# Rebuild the development images
build:
    docker compose build web worker scheduler tools

# Start the application and its supporting services.
up:
    docker compose up -d

# Stop the application while keeping local database data.
down:
    docker compose down

# Follow the web and worker application logs.
[no-exit-message]
logs:
    docker compose logs -f web worker

# Apply all pending database migrations.
migrate:
    docker compose run --build --rm web alembic upgrade head

# Refresh reference data from config/sources.yaml: KIND is authorization, ontology, entity, or annotation.
[positional-arguments]
refresh KIND *SOURCE:
    docker compose run --build --rm web python -m standard_annotation_backend.cli.refresh "$@"

# Run tests in a temporary container.
[positional-arguments]
test *ARGS:
    docker compose run --build --rm tools uv run --locked --no-sync pytest "$@"

# Run formatting, lint, and type checks in a temporary container.
check:
    docker compose run --build --rm --no-deps tools sh -c "uv run --locked --no-sync ruff format --check . && uv run --locked --no-sync ruff check . && uv run --locked --no-sync ty check src tests"

# Display the application version generated from the current Git state.
version:
    docker compose run --build --rm --no-deps tools python -c "from importlib.metadata import version; print(version('standard-annotation-backend'))"

[private]
[unix]
_ensure-env:
    test -f .env || cp .env.example .env

[private]
[windows]
_ensure-env:
    powershell.exe -NoProfile -Command "if (!(Test-Path .env)) { Copy-Item .env.example .env }"
