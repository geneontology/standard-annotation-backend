"""Tests for the failure and outcome types shared by every refresh."""

import pytest
from fastapi import status

from standard_annotation_backend.api.errors import status_for
from standard_annotation_backend.domain.annotation_management import (
    AnnotationImportConflictError,
    GroupSabManagedError,
)
from standard_annotation_backend.domain.entities import (
    EntityCandidateConflictError,
    EntityCatalogCollisionError,
)
from standard_annotation_backend.domain.errors import SabError
from standard_annotation_backend.domain.ontology import (
    OntologyCandidateConflictError,
)
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshKindName,
    RefreshOutcome,
    RetryableRefreshError,
    TerminalRefreshError,
    UnknownSourceError,
)
from standard_annotation_backend.refresh.fetchers import SourceError
from standard_annotation_backend.services.annotation_refresh_service import (
    AnnotationRefreshBusyError,
)
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshBusyError,
)


class _FixedCodeError(TerminalRefreshError):
    failure_code = RefreshFailureCode.CATALOG_COLLISION

    def __init__(self) -> None:
        super().__init__("identifiers collide")


def test_terminal_error_records_a_code_and_details_given_by_the_raiser() -> None:
    """A translated parser failure carries its code and details to the runner."""
    error = TerminalRefreshError(
        failure_code=RefreshFailureCode.HEADER, failure_details={"message": "bad"}
    )

    assert error.failure_code is RefreshFailureCode.HEADER
    assert error.failure_details == {"message": "bad"}
    assert str(error) == "Refresh failed: header"


def test_terminal_error_subclass_declares_its_code_and_keeps_its_message() -> None:
    """A domain error with a fixed code needs no translation and keeps its text."""
    error = _FixedCodeError()

    assert error.failure_code is RefreshFailureCode.CATALOG_COLLISION
    assert error.failure_details is None
    assert str(error) == "identifiers collide"


def test_terminal_error_without_any_code_is_a_programming_error() -> None:
    """Raising the base class without a code fails loudly instead of guessing."""
    with pytest.raises(TypeError):
        TerminalRefreshError()


def test_outcome_defaults_to_an_applied_document_without_counts() -> None:
    """An outcome is applied unless a service says the document was unchanged."""
    outcome = RefreshOutcome(result={"source_key": "mgi"})

    assert outcome.unchanged is False
    assert dict(outcome.counts) == {}
    assert outcome.warnings == ()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (SourceError(RefreshFailureCode.TIMEOUT), RefreshFailureCode.TIMEOUT),
        (
            UnknownSourceError(RefreshKindName.ENTITY, "mgi"),
            RefreshFailureCode.UNKNOWN_SOURCE,
        ),
        (EntityCandidateConflictError(), RefreshFailureCode.CANDIDATE_CONFLICT),
        (
            EntityCatalogCollisionError(("MGI:1",)),
            RefreshFailureCode.CATALOG_COLLISION,
        ),
        (
            OntologyCandidateConflictError("newer snapshot active"),
            RefreshFailureCode.CANDIDATE_CONFLICT,
        ),
        (GroupSabManagedError("MGI"), RefreshFailureCode.GROUP_SAB_MANAGED),
        (AnnotationImportConflictError(), RefreshFailureCode.INVALID_PARAMETERS),
    ],
)
def test_refresh_only_errors_declare_their_failure_code(
    error: TerminalRefreshError, code: RefreshFailureCode
) -> None:
    """Errors that only mean "this refresh failed" need no per-kind mapping."""
    assert isinstance(error, TerminalRefreshError)
    assert error.failure_code is code
    assert error.failure_details is None


def test_source_error_keeps_its_code_attribute_and_message() -> None:
    """Retrieval errors still expose `code` and a message without the URL."""
    error = SourceError(RefreshFailureCode.HTTP_STATUS)

    assert error.code is RefreshFailureCode.HTTP_STATUS
    assert str(error) == "Source retrieval failed: http_status"


def test_source_error_rejects_a_code_that_is_not_a_retrieval_failure() -> None:
    """Only retrieval and decoding codes describe a source failure."""
    with pytest.raises(ValueError, match="retrieval"):
        SourceError(RefreshFailureCode.HEADER)


@pytest.mark.parametrize(
    "error", [OntologyRefreshBusyError(), AnnotationRefreshBusyError()]
)
def test_busy_lock_errors_are_retryable_not_terminal(error: Exception) -> None:
    """Lock contention is retried later instead of failing the job."""
    assert isinstance(error, RetryableRefreshError)
    assert not isinstance(error, TerminalRefreshError)


def test_unknown_source_is_a_located_invalid_input_error() -> None:
    """An unknown source is reported to clients at the body's `source_key`."""
    error = UnknownSourceError(RefreshKindName.ENTITY, "mgi")

    assert isinstance(error, SabError)
    assert status_for(type(error)) == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert error.code == "unknown_source"
    assert error.message == "Source is not configured"
    assert error.details() == (
        {
            "location": ("body", "source_key"),
            "message": "Source is not configured",
            "type": "unknown_source",
        },
    )
    assert error.kind is RefreshKindName.ENTITY
    assert error.source_key == "mgi"
    assert str(error) == "source is not configured"


def test_group_sab_managed_is_a_conflict_error() -> None:
    """A SAB-managed group is a conflict for clients and a failure for refreshes."""
    error = GroupSabManagedError("MGI")

    assert isinstance(error, SabError)
    assert status_for(type(error)) == status.HTTP_409_CONFLICT
    assert error.code == "group_sab_managed"
    assert error.message == (
        "The source's group is managed in SAB, so GPAD can no longer replace "
        "its annotations"
    )
    assert error.details() is None
    assert error.group_key == "MGI"
    assert str(error) == "group is SAB-managed"
