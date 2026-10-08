"""Test annotation version headers and annotation error responses."""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, status
from fastapi.testclient import TestClient

from standard_annotation_backend.api.dependencies import (
    parse_if_match,
    require_expected_version,
)
from standard_annotation_backend.api.errors import install_exception_handlers
from standard_annotation_backend.domain.annotations import (
    InvalidAnnotationPayloadError,
)

_MALFORMED_IF_MATCH_MESSAGE = "If-Match must be one quoted positive integer"


def _if_match_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.patch("/resource")
    def route(
        expected_version: Annotated[int, Depends(require_expected_version)],
    ) -> dict[str, int]:
        return {"expected_version": expected_version}

    return app


def test_if_match_requires_the_header() -> None:
    """A missing `If-Match` header returns 428 `precondition_required`."""
    response = TestClient(_if_match_app()).patch("/resource")

    assert response.status_code == status.HTTP_428_PRECONDITION_REQUIRED
    assert response.json() == {
        "error": {"code": "precondition_required", "message": "If-Match is required"}
    }


@pytest.mark.parametrize(
    "header",
    ["1", 'W/"1"', "*", '"1", "2"', '"0"', '"-1"', '"abc"'],
)
def test_if_match_rejects_malformed_values(header: str) -> None:
    """A malformed `If-Match` value returns 422 located at the header."""
    response = TestClient(_if_match_app()).patch(
        "/resource", headers={"If-Match": header}
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json() == {
        "error": {
            "code": "malformed_precondition",
            "message": _MALFORMED_IF_MATCH_MESSAGE,
            "details": [
                {
                    "location": ["header", "If-Match"],
                    "message": _MALFORMED_IF_MATCH_MESSAGE,
                    "type": "malformed_precondition",
                }
            ],
        }
    }


def test_if_match_returns_the_quoted_positive_version() -> None:
    assert parse_if_match('"27"') == 27


def test_invalid_annotation_error_preserves_normalized_validation_details() -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/resource")
    def route() -> None:
        raise InvalidAnnotationPayloadError(
            (
                {
                    "location": ("annotation", "db_object_id"),
                    "message": "A supplied domain validation message",
                    "type": "missing",
                },
            )
        )

    response = TestClient(app).get("/resource")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json() == {
        "error": {
            "code": "invalid_annotation",
            "message": "Annotation payload is invalid",
            "details": [
                {
                    "location": ["annotation", "db_object_id"],
                    "message": "A supplied domain validation message",
                    "type": "missing",
                }
            ],
        }
    }
