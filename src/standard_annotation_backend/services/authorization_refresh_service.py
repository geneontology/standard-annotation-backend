"""Run authorization refresh jobs for `RefreshRunner` from the go-site `users.yaml`.

The complete document is validated before anything changes. The replacement of
users and grants, its record, and its audit event commit in the job's
transaction.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field

from standard_annotation_backend.auth.users_yaml import (
    InvalidUsersDocumentError,
    parse_users_yaml,
)
from standard_annotation_backend.domain.jobs import (
    JobFailureCode,
    JobType,
    ProgressReporter,
)
from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
    RefreshOutcome,
    SourceDocument,
    TerminalRefreshError,
    validation_failure_details,
)
from standard_annotation_backend.domain.stored_json import StoredJson
from standard_annotation_backend.persistence.models import AuthorizationRefreshRecord
from standard_annotation_backend.persistence.repositories.auth import (
    SyncAuthorization,
    SyncUser,
    same_refresh_source,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from standard_annotation_backend.refresh.fetchers import SourceError
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job


@dataclass(frozen=True, slots=True)
class AuthorizationRefreshResult:
    """Describe a committed refresh with counts of its resulting active state.

    User counts include all GitHub-linked people, even those without grants.
    Group counts include distinct groups in active grants, excluding retained
    historical groups. Assignment counts exclude identical repeated grants.
    """

    refresh_id: UUID
    source_type: str
    source_locator: str
    source_revision: str | None
    source_checksum: str
    fetched_at: datetime
    refreshed_at: datetime
    user_count: int
    group_count: int
    assignment_count: int

    def to_job_result(self) -> dict[str, object]:
        """Return the result as a JSON-compatible dict for the job record."""
        return STORED_AUTHORIZATION_REFRESH_RESULT.dump(self)


@dataclass(frozen=True, slots=True)
class AuthorizationRefreshSummary:
    """Count the users, groups, and grants resulting from a refresh.

    The counts are stored with the refresh record so a later job for the same
    document can report them without recounting.

    Attributes:
        users: GitHub-linked people, including those without grants.
        groups: Distinct groups in active grants.
        assignments: Grants, excluding identical repeats.
    """

    users: Annotated[int, Field(ge=0)]
    groups: Annotated[int, Field(ge=0)]
    assignments: Annotated[int, Field(ge=0)]


STORED_AUTHORIZATION_SUMMARY = StoredJson(
    AuthorizationRefreshSummary, label="authorization refresh summary"
)
STORED_AUTHORIZATION_REFRESH_RESULT = StoredJson(
    AuthorizationRefreshResult, label="authorization refresh result"
)


class AuthorizationRefreshService:
    """Validate the complete source before replacing the local authorization state."""

    @property
    def name(self) -> RefreshKindName:
        """Return `authorization`."""
        return RefreshKindName.AUTHORIZATION

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `authorization_refresh`."""
        return frozenset({JobType.AUTHORIZATION_REFRESH})

    def apply(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        job: Job,
        document: SourceDocument,
        report: ProgressReporter,
    ) -> RefreshOutcome:
        """Validate the complete document, then replace users and grants.

        A document whose content the most recent refresh already holds, read
        with the same reader version, is not applied again. Raw YAML and
        unrelated fields are never stored.

        Raises:
            SourceError: With `invalid_utf8` if the bytes are not UTF-8.
            TerminalRefreshError: With `invalid_document` if validation fails.
            StoredDataError: If a stored summary has invalid counts.
        """
        latest = unit_of_work.auth.latest_refresh()
        if latest is not None and same_refresh_source(latest, document.provenance):
            summary = STORED_AUTHORIZATION_SUMMARY.load(latest.summary)
            return _outcome(_result(latest, summary), unchanged=True)
        try:
            yaml_text = document.content.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceError(JobFailureCode.INVALID_UTF8) from None
        try:
            users, summary = _users(yaml_text)
        except InvalidUsersDocumentError as error:
            raise TerminalRefreshError(
                failure_code=JobFailureCode.INVALID_DOCUMENT,
                failure_details=validation_failure_details(error.errors),
            ) from None
        stored_summary = STORED_AUTHORIZATION_SUMMARY.dump(summary)
        record = unit_of_work.auth.replace_authorizations(
            users=users, provenance=document.provenance, summary=stored_summary
        )
        result = _result(record, summary)
        AuditService(unit_of_work.audit).record_authorization_refreshed(
            actor_id=job.requested_by,
            job_id=job.job_id,
            details={**result.to_job_result(), **stored_summary},
        )
        return _outcome(result, unchanged=False)


def _users(
    yaml_text: str,
) -> tuple[tuple[SyncUser, ...], AuthorizationRefreshSummary]:
    """Validate `users.yaml` and return its GitHub-linked users and their counts.

    Raises:
        InvalidUsersDocumentError: If any entry or the YAML is invalid.
    """
    document = parse_users_yaml(yaml_text)
    users = tuple(
        SyncUser(
            github_login=user.accounts.github,
            display_name=user.nickname,
            authorizations=tuple(
                SyncAuthorization(
                    role=grant.role, scope=grant.scope, group_id=grant.group
                )
                for grant in user.authorizations.sabe
            ),
        )
        for user in document.root
        if user.accounts.github is not None
    )
    groups = {
        grant.group_id
        for user in users
        for grant in user.authorizations
        if grant.group_id is not None
    }
    return users, AuthorizationRefreshSummary(
        users=len(users),
        groups=len(groups),
        assignments=sum(len(user.authorizations) for user in users),
    )


def _result(
    record: AuthorizationRefreshRecord, summary: AuthorizationRefreshSummary
) -> AuthorizationRefreshResult:
    """Build a result from a stored refresh record and its validated counts."""
    return AuthorizationRefreshResult(
        refresh_id=record.refresh_id,
        source_type=record.source_type,
        source_locator=record.source_locator,
        source_revision=record.source_revision,
        source_checksum=record.source_checksum,
        fetched_at=record.fetched_at,
        refreshed_at=record.refreshed_at,
        user_count=summary.users,
        group_count=summary.groups,
        assignment_count=summary.assignments,
    )


def _outcome(result: AuthorizationRefreshResult, *, unchanged: bool) -> RefreshOutcome:
    """Convert a service result into the job's outcome."""
    return RefreshOutcome(
        result=result.to_job_result(),
        counts={
            "user_count": result.user_count,
            "group_count": result.group_count,
            "assignment_count": result.assignment_count,
        },
        unchanged=unchanged,
    )
