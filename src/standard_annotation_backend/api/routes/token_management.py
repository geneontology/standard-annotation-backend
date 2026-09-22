"""Serve the SAB-owned browser interface for bearer-token management."""

from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)
_PACKAGE_FILES = files("standard_annotation_backend")


def _read_template() -> str:
    """Read the UTF-8 browser entry point bundled with the application package."""
    return _PACKAGE_FILES.joinpath("templates", "token_management.html").read_text(
        encoding="utf-8"
    )


@router.get("/token-management", response_class=HTMLResponse)
def token_management_page() -> HTMLResponse:
    """Return the browser entry point for managing the signed-in user's tokens."""
    return HTMLResponse(_read_template())
