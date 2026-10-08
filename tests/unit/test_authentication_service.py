"""Reject malformed bearer credentials before opening a database transaction."""

import pytest

from standard_annotation_backend.domain.auth import AuthenticationRequiredError
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "sab_token_",
        "sab_token_short",
        "sab_session_" + "a" * 43,
        "sab_token_" + "a" * 42,
        "sab_token_" + "a" * 44,
        "sab_token_" + "!" * 43,
        "sab_token_" + "\N{SNOWMAN}" * 43,
        " sab_token_" + "a" * 43,
        "sab_token_" + "a" * 43 + "\n",
    ],
)
def test_malformed_credential_never_opens_storage(raw: str) -> None:
    """Missing and malformed token values fail with the same safe domain error."""
    from standard_annotation_backend.services.authentication_service import (
        AuthenticationService,
    )

    def unavailable_storage() -> SqlAlchemyUnitOfWork:
        raise AssertionError("Malformed credentials must not query storage")

    with pytest.raises(AuthenticationRequiredError) as raised:
        AuthenticationService(unavailable_storage).authenticate(raw)
    assert str(raised.value) == "Authentication required"
