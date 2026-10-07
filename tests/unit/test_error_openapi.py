"""Documented error responses reference the error envelope and list their codes."""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter
from fastapi.routing import APIRoute

from standard_annotation_backend.api.models import ApiErrorResponse
from standard_annotation_backend.main import app


def _is_error(status_code: str | int) -> bool:
    return str(status_code).startswith(("4", "5"))


def test_every_documented_error_response_lists_its_codes() -> None:
    """Every documented 4xx/5xx response in the schema uses the error envelope.

    This covers the routes that appear in the generated OpenAPI schema,
    including FastAPI's default 422 entry, which must be replaced.
    """
    schema = app.openapi()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for status_code, response in operation.get("responses", {}).items():
                if not _is_error(status_code):
                    continue
                reference = response["content"]["application/json"]["schema"].get(
                    "$ref", ""
                )
                assert reference.endswith("/ApiErrorResponse"), (
                    method,
                    path,
                    status_code,
                )
                assert "`" in response["description"], (method, path, status_code)


def _included_routers() -> list[APIRouter]:
    """Return every router included in the app, including hidden ones.

    FastAPI wraps each included router and exposes the original as
    `original_router`; `app.routes` itself holds only the wrappers. If a FastAPI
    upgrade changes this, the visited-route assertions in the test below fail
    instead of the test silently checking nothing.
    """
    routers: list[APIRouter] = []
    for route in app.routes:
        original = getattr(route, "original_router", None)
        if isinstance(original, APIRouter):
            routers.append(original)
    return routers


def _assert_error_responses(
    responses: Mapping[int | str, Mapping[str, Any]], owner: str
) -> None:
    """Require each 4xx/5xx entry to use the envelope model and list its codes."""
    for status_code, response in responses.items():
        if not _is_error(status_code):
            continue
        where = (owner, int(status_code))
        assert response["model"] is ApiErrorResponse, where
        assert "`" in response["description"], where


def test_every_route_including_hidden_ones_documents_errors_with_codes() -> None:
    """Every router and route, hidden or not, declares errors with the envelope model.

    The schema test above cannot see routes hidden from OpenAPI, such as token
    management, or tell router-level entries from route-level ones. This test
    checks the declarations themselves: each 4xx/5xx entry on a router or route
    names `ApiErrorResponse` as its model and lists backticked error codes.
    """
    visited: set[str] = set()
    for router in _included_routers():
        _assert_error_responses(router.responses, router.prefix)
        for route in router.routes:
            if isinstance(route, APIRoute):
                visited.add(route.path)
                _assert_error_responses(route.responses, route.path)

    # Prove the walk reached hidden routes, including the OAuth callback.
    assert {"/auth/github/callback", "/tokens", "/token-management"} <= visited
    assert {"/admin/jobs/{job_id}", "/annotations/{annotation_id}"} <= visited


def test_oauth_callback_documents_its_internal_failure_code() -> None:
    """The OAuth callback documents its 500 as `oauth_callback_failed`."""
    callback = next(
        route
        for router in _included_routers()
        for route in router.routes
        if isinstance(route, APIRoute) and route.path == "/auth/github/callback"
    )

    assert "`oauth_callback_failed`" in callback.responses[500]["description"]


def test_every_bearer_route_documents_internal_error() -> None:
    """Every route that accepts a bearer token documents `500: internal_error`."""
    schema = app.openapi()
    checked = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if {"Bearer": []} not in operation.get("security", []):
                continue
            checked.append((method, path))
            description = operation["responses"]["500"]["description"]
            assert "`internal_error`" in description, (method, path)
    assert ("get", "/annotations") in checked
    assert ("post", "/change-sets/{change_set_id}/accept") in checked
