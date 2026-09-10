# List the commands available for local development.
set default-list

# Prepare local settings, build the development images, and migrate the database.
setup: _ensure-env
    docker compose build web worker tools
    docker compose run --rm web alembic upgrade head

# Start the application and its supporting services.
up:
    docker compose up -d

# Stop the application while keeping local database data.
down:
    docker compose down

# Follow the web application logs.
[no-exit-message]
logs:
    docker compose logs -f web

# Apply all pending database migrations.
migrate:
    docker compose run --build --rm web alembic upgrade head

# Run the complete test suite in a disposable container.
test:
    docker compose run --build --rm tools uv run --locked pytest

# Run formatting, lint, and type checks in a disposable container.
check:
    docker compose run --build --rm --no-deps tools sh -c "uv run --locked ruff format --check . && uv run --locked ruff check . && uv run --locked ty check src tests"

[private]
[unix]
_ensure-env:
    test -f .env || cp .env.example .env

[private]
[windows]
_ensure-env:
    powershell.exe -NoProfile -Command "if (!(Test-Path .env)) { Copy-Item .env.example .env }"
