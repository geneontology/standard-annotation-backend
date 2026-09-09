"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from standard_annotation_backend.config import get_settings
from standard_annotation_backend.domain.schema_artifacts import load_json_schema


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize application state before accepting API requests.

    Args:
        app: FastAPI application whose state is initialized.

    Yields:
        Control while the application is running.
    """
    settings = get_settings()
    app.state.settings = settings
    app.state.standard_annotation_json_schema = load_json_schema()
    yield


app = FastAPI(title="Standard Annotation Backend", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    """Return the process health status without requiring dependencies."""
    return {"status": "ok"}
