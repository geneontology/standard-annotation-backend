"""Refresh SAB's authorization state from the go-site `users.yaml` source.

The complete document is validated before anything changes, and the replacement
of users and grants commits in one transaction with its record and audit event.
There is no staging to recover. A redelivered job fetches again, and `unchanged`
recognizes the committed refresh as the current state.
"""

from collections.abc import Mapping
from uuid import UUID

from standard_annotation_backend.auth.users_yaml import InvalidUsersDocumentError
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshKindName,
    refresh_failure_message,
)
from standard_annotation_backend.domain.validation import ValidationIssue
from standard_annotation_backend.refresh.fetchers import SourceDocument, SourceError
from standard_annotation_backend.refresh.kinds import (
    ProgressReporter,
    RefreshResult,
    TerminalRefreshError,
)
from standard_annotation_backend.refresh.sources import (
    GitHubSource,
    HttpsSource,
    RefreshSources,
)
from standard_annotation_backend.services.authorization_refresh_service import (
    AuthorizationRefreshResult,
    AuthorizationRefreshService,
)
from standard_annotation_backend.services.job_service import Job, JobService

MAX_REPORTED_ISSUES = 100


class AuthorizationRefreshKind:
    """Refresh authorization from the single configured `users.yaml` source.

    Args:
        sources: Configured sources for every kind.
        service: Validates and applies `users.yaml`.
        jobs: Records job failures.
    """

    def __init__(
        self,
        *,
        sources: RefreshSources,
        service: AuthorizationRefreshService,
        jobs: JobService,
    ) -> None:
        self._sources = sources
        self._service = service
        self._jobs = jobs

    @property
    def name(self) -> RefreshKindName:
        """Return `authorization`."""
        return RefreshKindName.AUTHORIZATION

    @property
    def job_type(self) -> JobType:
        """Return `authorization_refresh`."""
        return JobType.AUTHORIZATION_REFRESH

    @property
    def terminal_errors(self) -> Mapping[type[Exception], RefreshFailureCode]:
        """Authorization has no kind-specific terminal errors."""
        return {}

    def sources(self) -> Mapping[str, GitHubSource | HttpsSource]:
        """Return the authorization source under the key `go-site`."""
        return self._sources.sources(RefreshKindName.AUTHORIZATION)

    def recover(self, job: Job) -> RefreshResult | None:
        """Return `None`; a committed refresh is recognized by `unchanged` instead."""
        return None

    def unchanged(
        self, source_key: str, document: SourceDocument
    ) -> RefreshResult | None:
        """Return the stored result when the current state came from this document."""
        result = self._service.unchanged(document.provenance)
        return None if result is None else _refresh_result(result)

    def apply(
        self, job: Job, document: SourceDocument, progress: ProgressReporter
    ) -> RefreshResult:
        """Validate the complete document, then replace users and grants.

        Raises:
            SourceError: With `invalid_utf8` if the bytes are not UTF-8.
            TerminalRefreshError: With `invalid_document` if validation fails.
        """
        try:
            yaml_text = document.content.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceError(RefreshFailureCode.INVALID_UTF8) from None
        try:
            result = self._service.refresh(
                yaml_text,
                document.provenance,
                actor_id=job.requested_by,
                job_id=job.job_id,
            )
        except InvalidUsersDocumentError as error:
            raise TerminalRefreshError(
                RefreshFailureCode.INVALID_DOCUMENT, _issue_details(error.errors)
            ) from None
        return _refresh_result(result)

    def fail(
        self,
        job_id: UUID,
        failure_code: RefreshFailureCode,
        failure_details: dict[str, object] | None,
    ) -> None:
        """Mark the job failed and audit why."""
        self._jobs.fail_refresh(
            job_id,
            error=refresh_failure_message(RefreshKindName.AUTHORIZATION),
            failure_code=failure_code,
            failure_details=failure_details,
        )

    def after_terminal(self, source_key: str) -> None:
        """Authorization refreshes have no follow-up work."""


def _refresh_result(result: AuthorizationRefreshResult) -> RefreshResult:
    """Convert a service result into the job's result and progress."""
    progress: dict[str, object] = {
        "user_count": result.user_count,
        "group_count": result.group_count,
        "assignment_count": result.assignment_count,
    }
    if not result.applied:
        # Another job applied this same document first (or it was already
        # active). Report it with the flag every kind uses.
        progress["unchanged"] = True
    return RefreshResult(result=result.to_job_result(), progress=progress)


def _issue_details(issues: tuple[ValidationIssue, ...]) -> dict[str, object]:
    """Describe validation issues for the job, keeping at most 100 of them."""
    return {
        "issue_count": len(issues),
        "issues": [
            {
                "location": list(issue["location"]),
                "message": issue["message"],
                "type": issue["type"],
            }
            for issue in issues[:MAX_REPORTED_ISSUES]
        ],
    }
