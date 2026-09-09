"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from standard_annotation_backend.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load process settings before accepting API requests."""
    app.state.settings = get_settings()
    yield


app = FastAPI(title="Standard Annotation Backend", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    """Return the process health status without requiring dependencies."""
    return {"status": "ok"}
