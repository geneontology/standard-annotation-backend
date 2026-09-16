"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from standard_annotation_backend.api.errors import install_exception_handlers
from standard_annotation_backend.api.routes.annotation_versions import (
    router as annotation_versions_router,
)
from standard_annotation_backend.api.routes.annotations import (
    router as annotations_router,
)
from standard_annotation_backend.api.routes.change_sets import (
    router as change_sets_router,
)
from standard_annotation_backend.config import get_settings
from standard_annotation_backend.domain.schema_artifacts import load_json_schema
from standard_annotation_backend.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from standard_annotation_backend.persistence.unit_of_work import (
    create_unit_of_work_factory,
)
from standard_annotation_backend.version import get_application_version


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
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    app.state.unit_of_work_factory = create_unit_of_work_factory(session_factory)
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(
    title="Standard Annotation Backend",
    version=get_application_version(),
    redoc_url=None,
    lifespan=lifespan,
)
install_exception_handlers(app)
app.include_router(annotation_versions_router)
app.include_router(annotations_router)
app.include_router(change_sets_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Return the process health status without requiring dependencies."""
    return {"status": "ok"}
