"""Run authorization refresh jobs for `RefreshRunner` from the go-site `users.yaml`.

The complete document is validated before anything changes, and the replacement
of users and grants commits in one transaction with its record and audit event.
There is no staging to recover. A redelivered job fetches again, and `apply`
recognizes the committed refresh as the current state.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from standard_annotation_backend.auth.users_yaml import (
    InvalidUsersDocumentError,
    parse_users_yaml,
)
from standard_annotation_backend.domain.audit import AuditAction, AuditResult
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import (
    ProgressReporter,
    RefreshFailureCode,
    RefreshKindName,
    RefreshOutcome,
    SourceDocument,
    SourceProvenance,
    TerminalRefreshError,
)
from standard_annotation_backend.domain.validation import ValidationIssue
from standard_annotation_backend.persistence.models import AuthorizationRefreshRecord
from standard_annotation_backend.persistence.repositories.auth import (
    SyncAuthorization,
    SyncUser,
    same_refresh_source,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.refresh.fetchers import SourceError
from standard_annotation_backend.services.job_service import Job

MAX_REPORTED_ISSUES = 100


class InvalidAuthorizationRefreshSummaryError(RuntimeError):
    """Report invalid counts read from a stored refresh record."""

    def __init__(self) -> None:
        super().__init__("stored authorization refresh summary is invalid")


class _StoredRefreshSummary(BaseModel):
    """Validate counts loaded from an existing refresh record."""

    model_config = ConfigDict(extra="forbid")

    users: Annotated[StrictInt, Field(ge=0)]
    groups: Annotated[StrictInt, Field(ge=0)]
    assignments: Annotated[StrictInt, Field(ge=0)]


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
        return {
            "refresh_id": str(self.refresh_id),
            "source_type": self.source_type,
            "source_locator": self.source_locator,
            "source_revision": self.source_revision,
            "source_checksum": self.source_checksum,
            "fetched_at": self.fetched_at.isoformat(),
            "refreshed_at": self.refreshed_at.isoformat(),
            "user_count": self.user_count,
            "group_count": self.group_count,
            "assignment_count": self.assignment_count,
        }


class AuthorizationRefreshService:
    """Validate the complete source before replacing the local authorization state.

    Args:
        unit_of_work_factory: Factory for the transaction shared by auth and audit.
    """

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    @property
    def name(self) -> RefreshKindName:
        """Return `authorization`."""
        return RefreshKindName.AUTHORIZATION

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `authorization_refresh`."""
        return frozenset({JobType.AUTHORIZATION_REFRESH})

    def recover(self, job: Job) -> RefreshOutcome | None:
        """Return `None`; `apply` recognizes a committed refresh as unchanged."""
        return None

    def apply(
        self, job: Job, document: SourceDocument, report: ProgressReporter
    ) -> RefreshOutcome:
        """Validate the complete document, then replace users and grants.

        A document identical to the one behind the most recent refresh is not
        applied again.

        Raises:
            SourceError: With `invalid_utf8` if the bytes are not UTF-8.
            TerminalRefreshError: With `invalid_document` if validation fails.
        """
        current = self._current(document.provenance)
        if current is not None:
            return _outcome(current, unchanged=True)
        try:
            yaml_text = document.content.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceError(RefreshFailureCode.INVALID_UTF8) from None
        try:
            result, applied = self._replace(
                yaml_text,
                document.provenance,
                actor_id=job.requested_by,
                job_id=job.job_id,
            )
        except InvalidUsersDocumentError as error:
            raise TerminalRefreshError(
                failure_code=RefreshFailureCode.INVALID_DOCUMENT,
                failure_details=_issue_details(error.errors),
            ) from None
        # Another job may have applied this same document first.
        return _outcome(result, unchanged=not applied)

    def discard_staging(self, uow: SqlAlchemyUnitOfWork, job_id: UUID) -> None:
        """Do nothing; authorization refreshes have no staging."""

    def after_terminal(self, source_key: str) -> None:
        """Do nothing; authorization refreshes have no follow-up work."""

    def _replace(
        self,
        yaml_text: str,
        provenance: SourceProvenance,
        *,
        actor_id: str,
        job_id: UUID,
    ) -> tuple[AuthorizationRefreshResult, bool]:
        """Replace users and grants, saving source details and audit in one commit.

        Any persistence or audit failure rolls back the entire operation. Raw
        YAML and unrelated fields are never written to storage or audit records.
        A document identical to the one behind the most recent refresh is not
        applied again.

        Args:
            yaml_text: Complete go-site users.yaml contents.
            provenance: Identifies the exact document that `yaml_text` came from.
            actor_id: Identity recorded on the audit event.
            job_id: Job that requested the refresh.

        Returns:
            Source details with active user, group, and assignment counts, and
            whether this call applied the document. `False` means the most
            recent refresh already came from the same document.

        Raises:
            InvalidUsersDocumentError: If any source entry or YAML is invalid.
            InvalidAuthorizationRefreshSummaryError: If a stored result has
                invalid counts.
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
                    for grant in user.authorizations.sab
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
        assignment_count = sum(len(user.authorizations) for user in users)
        summary: dict[str, object] = {
            "users": len(users),
            "groups": len(groups),
            "assignments": assignment_count,
        }
        with self._unit_of_work_factory() as unit_of_work:
            replacement = unit_of_work.auth.replace_authorizations(
                users=users,
                provenance=provenance,
                summary=summary,
            )
            record = replacement.record
            if replacement.applied:
                stored_summary = _StoredRefreshSummary.model_validate(summary)
            else:
                stored_summary = _stored_summary(record)
            result = _result(record, stored_summary)
            if replacement.applied:
                unit_of_work.audit.record(
                    action=AuditAction.AUTHORIZATION_REFRESHED,
                    actor_id=actor_id,
                    result=AuditResult.SUCCESS,
                    job_id=job_id,
                    details={**result.to_job_result(), **summary},
                )
            unit_of_work.commit()
        return result, replacement.applied

    def _current(
        self, provenance: SourceProvenance
    ) -> AuthorizationRefreshResult | None:
        """Return the stored result when the current state came from this document.

        The comparison uses the most recent refresh only, so returning to an older
        document applies it again.

        Raises:
            InvalidAuthorizationRefreshSummaryError: If the stored counts are invalid.
        """
        with self._unit_of_work_factory() as unit_of_work:
            latest = unit_of_work.auth.latest_refresh()
            if latest is None or not same_refresh_source(latest, provenance):
                return None
            return _result(latest, _stored_summary(latest))


def _stored_summary(record: AuthorizationRefreshRecord) -> _StoredRefreshSummary:
    """Validate the counts stored on a refresh record."""
    try:
        return _StoredRefreshSummary.model_validate(record.summary)
    except ValidationError:
        raise InvalidAuthorizationRefreshSummaryError from None


def _result(
    record: AuthorizationRefreshRecord, summary: _StoredRefreshSummary
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
