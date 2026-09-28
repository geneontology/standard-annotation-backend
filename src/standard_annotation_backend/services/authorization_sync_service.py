"""Replace synchronized authorizations and record their source revision."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
)

from standard_annotation_backend.auth.users_yaml import parse_users_yaml
from standard_annotation_backend.domain.audit import AuditAction, AuditResult
from standard_annotation_backend.domain.validation import ValidationIssue
from standard_annotation_backend.persistence.repositories.auth import (
    SyncAuthorization,
    SyncUser,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.validation_types import TrimmedNonBlankString


class _SyncSource(BaseModel):
    """Validate the source repository and commit before changing authorizations."""

    source_repository: TrimmedNonBlankString
    source_commit_sha: Annotated[
        str, StringConstraints(pattern=r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
    ]


class InvalidAuthorizationSyncSourceError(Exception):
    """Report an invalid source repository or commit without storing its value."""

    def __init__(self, errors: tuple[ValidationIssue, ...]) -> None:
        super().__init__("invalid authorization sync source")
        self.errors = errors


class InvalidAuthorizationSyncSummaryError(RuntimeError):
    """Report invalid counts read from a stored synchronization record."""

    def __init__(self) -> None:
        super().__init__("stored authorization sync summary is invalid")


class _StoredSyncSummary(BaseModel):
    """Validate counts loaded from an existing synchronization record."""

    model_config = ConfigDict(extra="forbid")

    users: Annotated[StrictInt, Field(ge=0)]
    groups: Annotated[StrictInt, Field(ge=0)]
    assignments: Annotated[StrictInt, Field(ge=0)]


@dataclass(frozen=True, slots=True)
class AuthorizationSyncResult:
    """Describe a committed sync with counts of its resulting active state.

    User counts include all GitHub-linked people, even those without grants.
    Group counts include distinct groups in active grants, excluding retained
    historical groups. Assignment counts exclude identical repeated grants.
    """

    sync_id: UUID
    source_repository: str
    source_commit_sha: str
    synchronized_at: datetime
    user_count: int
    group_count: int
    assignment_count: int
    applied: bool


class AuthorizationSyncService:
    """Validate the complete source before replacing the local authorization state.

    Args:
        unit_of_work_factory: Factory for the transaction shared by auth and audit.
    """

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def synchronize(
        self,
        yaml_text: str,
        source_repository: str,
        source_commit_sha: str,
        *,
        actor_id: str = "authorization-sync",
        job_id: UUID | None = None,
    ) -> AuthorizationSyncResult:
        """Replace users and grants, saving source details and audit in one commit.

        Any persistence or audit failure rolls back the entire operation. Raw
        YAML and unrelated fields are never written to storage or audit records.

        Args:
            yaml_text: Complete go-site users.yaml contents.
            source_repository: Nonblank identifier of the source repository.
            source_commit_sha: Full hexadecimal Git commit hash (40 or 64 digits).

        Returns:
            Source details and active user, group, and assignment counts.

        Raises:
            InvalidUsersDocumentError: If any source entry or YAML is invalid.
            InvalidAuthorizationSyncSourceError: If the repository or commit is invalid.
        """
        document = parse_users_yaml(yaml_text)
        try:
            source = _SyncSource(
                source_repository=source_repository, source_commit_sha=source_commit_sha
            )
        except ValidationError as error:
            raise InvalidAuthorizationSyncSourceError(
                tuple(
                    ValidationIssue(
                        location=detail["loc"],
                        message=detail["msg"],
                        type=detail["type"],
                    )
                    for detail in error.errors(
                        include_url=False, include_context=False, include_input=False
                    )
                )
            ) from None

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
                source_repository=source.source_repository,
                source_commit_sha=source.source_commit_sha.lower(),
                summary=summary,
            )
            record = replacement.record
            if replacement.applied:
                stored_summary = _StoredSyncSummary.model_validate(summary)
            else:
                try:
                    stored_summary = _StoredSyncSummary.model_validate(record.summary)
                except ValidationError:
                    raise InvalidAuthorizationSyncSummaryError from None
            result = AuthorizationSyncResult(
                sync_id=record.sync_id,
                source_repository=record.source_repository,
                source_commit_sha=record.source_commit_sha,
                synchronized_at=record.synchronized_at,
                user_count=stored_summary.users,
                group_count=stored_summary.groups,
                assignment_count=stored_summary.assignments,
                applied=replacement.applied,
            )
            if replacement.applied:
                unit_of_work.audit.record(
                    action=AuditAction.AUTHORIZATION_SYNCHRONIZED,
                    actor_id=actor_id,
                    result=AuditResult.SUCCESS,
                    job_id=job_id,
                    details={
                        "sync_id": str(result.sync_id),
                        "source_repository": result.source_repository,
                        "source_commit_sha": result.source_commit_sha,
                        "synchronized_at": result.synchronized_at.isoformat(),
                        **summary,
                    },
                )
            unit_of_work.commit()
        return result
