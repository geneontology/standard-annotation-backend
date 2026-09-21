"""FastAPI application entry point."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from standard_annotation_backend.api.errors import (
    install_exception_handlers,
    oauth_callback_failure_response,
)
from standard_annotation_backend.api.routes.annotation_versions import (
    router as annotation_versions_router,
)
from standard_annotation_backend.api.routes.annotations import (
    router as annotations_router,
)
from standard_annotation_backend.api.routes.change_sets import (
    router as change_sets_router,
)
from standard_annotation_backend.api.routes.tokens import (
    OAUTH_STATE_COOKIE,
    secure_cookies,
)
from standard_annotation_backend.api.routes.tokens import router as tokens_router
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
app.include_router(tokens_router)


@app.middleware("http")
async def protect_token_management_responses(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Protect responses from internal token-management routes.

    Token-management responses are not cacheable and cannot supply referrer data.
    OAuth callbacks also consume the browser's state cookie, including when an
    unexpected error occurs.

    Args:
        request: Incoming HTTP request.
        call_next: Callable that runs the remaining middleware and route handler.

    Returns:
        Response with token-management security headers and callback cleanup applied.

    Raises:
        Exception: Re-raises unexpected failures outside the OAuth callback route.
    """
    path = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        if path != "/auth/github/callback":
            raise
        # Callback failures must still consume browser state. Do not render or
        # log the exception: even unexpected failures may retain OAuth secrets.
        response = oauth_callback_failure_response()
    if (
        path.startswith("/auth/github/")
        or path == "/tokens"
        or path.startswith("/tokens/")
    ):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    if path == "/auth/github/callback":
        response.delete_cookie(
            OAUTH_STATE_COOKIE,
            path="/",
            secure=secure_cookies(request),
            httponly=True,
            samesite="lax",
        )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    """Return the process health status without requiring dependencies."""
    return {"status": "ok"}
