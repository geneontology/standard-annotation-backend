"""Verify validation completes before a synchronization opens its transaction."""

import pytest

from standard_annotation_backend.auth.users_yaml import InvalidUsersDocumentError
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork
from standard_annotation_backend.services.authorization_sync_service import (
    AuthorizationSyncService,
    InvalidAuthorizationSyncSourceError,
)


@pytest.mark.parametrize(
    ("source", "repository", "sha"),
    [
        ("[", "geneontology/go-site", "a" * 40),
        ("!!set {}", "geneontology/go-site", "a" * 40),
        (
            "- accounts: {github: alice}\n  authorizations: {sab: !!set {}}",
            "geneontology/go-site",
            "a" * 40,
        ),
        (
            "- accounts: {github: alice}\n- authorizations: {sab: [{role: edit, scope: group}]}\n",
            "geneontology/go-site",
            "a" * 40,
        ),
        ("[]", " ", "a" * 40),
        ("[]", "geneontology/go-site", "main"),
        ("[]", "geneontology/go-site", "a" * 39),
        ("[]", "geneontology/go-site", "z" * 40),
    ],
)
def test_invalid_input_never_opens_a_unit_of_work(
    source: str, repository: str, sha: str
) -> None:
    """Invalid YAML, entries, or provenance are rejected without a database connection."""

    def unavailable_transaction() -> SqlAlchemyUnitOfWork:
        pytest.fail("validation must finish before opening the unit of work")

    service = AuthorizationSyncService(unavailable_transaction)
    with pytest.raises(
        (InvalidUsersDocumentError, InvalidAuthorizationSyncSourceError)
    ):
        service.synchronize(source, repository, sha)
