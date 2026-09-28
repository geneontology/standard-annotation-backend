"""Persist user authorizations and credentials in caller-owned transactions."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
)
from standard_annotation_backend.domain.tokens import TokenMetadata
from standard_annotation_backend.persistence.locks import (
    acquire_authorization_sync_lock,
)
from standard_annotation_backend.persistence.models import (
    ApiTokenRecord,
    AuthorizationAssignmentRecord,
    AuthorizationSyncRecord,
    SabGroupRecord,
    SabUserRecord,
    TokenManagementSessionRecord,
)


class CredentialPersistenceError(Exception):
    """Hide credential parameters and database diagnostics from error consumers."""

    def __init__(self) -> None:
        super().__init__("Credential storage is unavailable")


def _protect_credential_errors[**P, T](operation: Callable[P, T]) -> Callable[P, T]:
    """Prevent database exceptions from disclosing credential lookup digests.

    Args:
        operation: Repository operation that reads or writes credential data.

    Returns:
        Wrapped operation that replaces SQLAlchemy errors with a safe error.
    """

    @wraps(operation)
    def protected(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return operation(*args, **kwargs)
        except SQLAlchemyError:
            # Driver diagnostics can contain a failing row as well as parameters.
            # Neither may cross this credential boundary or enter application logs.
            raise CredentialPersistenceError from None

    return protected


@dataclass(frozen=True, slots=True)
class SyncAuthorization:
    """Describe a validated grant using the external annotation ownership key.

    Attributes:
        role: Kind of action the grant permits.
        scope: Ownership boundary for the grant.
        group_id: External group key, or `None` for global scope.
    """

    role: str
    scope: str
    group_id: str | None


@dataclass(frozen=True, slots=True)
class SyncUser:
    """Supply a validated user and grants from the authorization document.

    Attributes:
        github_login: GitHub login used as the synchronized identity key.
        display_name: User's display name, when available.
        authorizations: Validated grants belonging to the user.
    """

    github_login: str
    display_name: str | None = None
    authorizations: tuple[SyncAuthorization, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorizationReplacement:
    """Pair a sync record with whether the current call created it."""

    record: AuthorizationSyncRecord
    applied: bool


class AuthRepository:
    """Keep synchronized grants and credentials in the shared transaction.

    Args:
        session: Session owned by the calling unit of work.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_authorizations(
        self,
        *,
        users: Sequence[SyncUser],
        source_repository: str,
        source_commit_sha: str,
        summary: dict[str, object],
    ) -> AuthorizationReplacement:
        """Replace active grants atomically and retain removed grants as history.

        A transaction-scoped advisory lock ensures that only one synchronization
        can occur at a time. If a second is started, it waits for the first to commit or
        roll back before proceeding.

        Unchanged active assignments retain their IDs. Previously deactivated
        assignments are never reactivated, so regranting cannot revive old tokens.
        The caller validates source data before entering the transaction and owns
        the commit, including any audit event for this synchronization.

        GitHub logins are the identity keys supplied by the source document.
        Changing a login creates a new local user and deactivates the old one.

        Args:
            users: Complete set of validated GitHub users and active grants.
            source_repository: Repository from which the source document was read.
            source_commit_sha: Immutable commit containing the source document.
            summary: Credential-free counts to retain with the synchronization.

        Returns:
            Stored synchronization record and whether this call applied it.
        """
        acquire_authorization_sync_lock(self.session)
        existing = self.session.scalar(
            select(AuthorizationSyncRecord).where(
                AuthorizationSyncRecord.source_repository == source_repository,
                AuthorizationSyncRecord.source_commit_sha == source_commit_sha,
            )
        )
        if existing is not None:
            return AuthorizationReplacement(existing, False)

        current_users = {
            user.github_login.lower(): user
            for user in self.session.scalars(select(SabUserRecord)).all()
        }
        groups = {
            group.group_key: group
            for group in self.session.scalars(select(SabGroupRecord)).all()
        }
        active = {
            (grant.user_id, grant.role, grant.scope, grant.group_id): grant
            for grant in self.session.scalars(
                select(AuthorizationAssignmentRecord).where(
                    AuthorizationAssignmentRecord.is_active
                )
            ).all()
        }
        retained: set[tuple[UUID, str, str, UUID | None]] = set()
        seen_users: set[UUID] = set()
        now = datetime.now(UTC)
        for entry in users:
            login = entry.github_login.lower()
            owner = current_users.get(login)
            if owner is None:
                owner = SabUserRecord(github_login=login)
                self.session.add(owner)
                self.session.flush([owner])
            seen_users.add(owner.user_id)
            owner.display_name = entry.display_name
            owner.is_active = True
            owner.updated_at = now
            for grant in entry.authorizations:
                group = None
                if grant.group_id is not None:
                    group = groups.get(grant.group_id)
                    if group is None:
                        group = SabGroupRecord(group_key=grant.group_id)
                        self.session.add(group)
                        self.session.flush([group])
                        groups[grant.group_id] = group
                group_id = None if group is None else group.group_id
                key = (owner.user_id, grant.role, grant.scope, group_id)
                if key not in active and key not in retained:
                    self.session.add(
                        AuthorizationAssignmentRecord(
                            user_id=owner.user_id,
                            role=grant.role,
                            scope=grant.scope,
                            group_id=group_id,
                        )
                    )
                retained.add(key)
        for owner in current_users.values():
            if owner.user_id not in seen_users:
                owner.is_active = False
                owner.updated_at = now
        for key, assignment in active.items():
            if key not in retained:
                assignment.is_active = False
        sync = AuthorizationSyncRecord(
            source_repository=source_repository,
            source_commit_sha=source_commit_sha,
            summary=summary,
        )
        self.session.add(sync)
        self.session.flush()
        return AuthorizationReplacement(sync, True)

    def get_user_by_github_login(self, github_login: str) -> SabUserRecord | None:
        """Find an active user using GitHub's case-insensitive login.

        Args:
            github_login: Current GitHub login.

        Returns:
            Matching active user, or `None` when no match exists.
        """
        return self.session.scalar(
            select(SabUserRecord).where(
                func.lower(SabUserRecord.github_login) == github_login.lower(),
                SabUserRecord.is_active,
            )
        )

    def list_active_assignments(
        self, user_id: UUID
    ) -> tuple[AuthorizationAssignmentRecord, ...]:
        """Return an active user's current grants with their group ownership keys.

        Args:
            user_id: Identifier of the user whose grants are requested.

        Returns:
            Active assignments when the user is active, otherwise an empty tuple.
        """
        return tuple(
            self.session.scalars(
                select(AuthorizationAssignmentRecord)
                .join(AuthorizationAssignmentRecord.owner)
                .options(
                    joinedload(AuthorizationAssignmentRecord.group),
                )
                .where(
                    AuthorizationAssignmentRecord.user_id == user_id,
                    AuthorizationAssignmentRecord.is_active,
                    SabUserRecord.is_active,
                )
                .order_by(AuthorizationAssignmentRecord.assignment_id)
            ).all()
        )

    def get_active_assignment(
        self, user_id: UUID, assignment_id: UUID
    ) -> AuthorizationAssignmentRecord | None:
        """Find an active assignment belonging to an active user.

        Args:
            user_id: Identifier of the expected assignment owner.
            assignment_id: Identifier of the assignment to retrieve.

        Returns:
            Matching active assignment, or `None` when no match exists.
        """
        return self.session.scalar(
            select(AuthorizationAssignmentRecord)
            .join(AuthorizationAssignmentRecord.owner)
            .options(
                joinedload(AuthorizationAssignmentRecord.group),
                joinedload(AuthorizationAssignmentRecord.owner),
            )
            .where(
                AuthorizationAssignmentRecord.user_id == user_id,
                AuthorizationAssignmentRecord.assignment_id == assignment_id,
                AuthorizationAssignmentRecord.is_active,
                SabUserRecord.is_active,
            )
        )

    @_protect_credential_errors
    def create_token(
        self,
        *,
        user_id: UUID,
        assignment_id: UUID,
        name: str,
        digest: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> ApiTokenRecord:
        """Store a digest and the selected grant after service validation.

        Copy the selected role, scope, and external group key so subsequent grant
        changes cannot silently change an issued token's authority.

        Args:
            user_id: Identifier of the token owner.
            assignment_id: Active authorization selected for the token.
            name: User-supplied token name.
            digest: SHA-256 digest used to look up the credential.
            created_at: Time when the token was created.
            expires_at: Time after which the token cannot authenticate.

        Returns:
            Newly stored token record.

        Raises:
            CredentialPersistenceError: If the assignment is invalid or storage fails.
        """
        assignment = self.get_active_assignment(user_id, assignment_id)
        if assignment is None:
            raise CredentialPersistenceError
        token = ApiTokenRecord(
            user_id=user_id,
            assignment_id=assignment_id,
            name=name,
            digest=digest,
            created_at=created_at,
            expires_at=expires_at,
            selected_role=assignment.role,
            selected_scope=assignment.scope,
            selected_group_id=None
            if assignment.group is None
            else assignment.group.group_key,
        )
        self.session.add(token)
        self.session.flush([token])
        return token

    @_protect_credential_errors
    def get_active_token(self, digest: str, *, now: datetime) -> ApiTokenRecord | None:
        """Load a live token while its owner and original grant remain active.

        Args:
            digest: SHA-256 digest of the presented bearer credential.
            now: Current time used to enforce expiration.

        Returns:
            Matching usable token, or `None` when it cannot authenticate.

        Raises:
            CredentialPersistenceError: If credential storage fails.
        """
        return self.session.scalar(
            select(ApiTokenRecord)
            .join(ApiTokenRecord.owner)
            .join(ApiTokenRecord.assignment)
            .options(
                joinedload(ApiTokenRecord.owner),
                joinedload(ApiTokenRecord.assignment).joinedload(
                    AuthorizationAssignmentRecord.group
                ),
            )
            .where(
                ApiTokenRecord.digest == digest,
                ApiTokenRecord.revoked_at.is_(None),
                ApiTokenRecord.expires_at > now,
                SabUserRecord.is_active,
                AuthorizationAssignmentRecord.is_active,
            )
        )

    @_protect_credential_errors
    def record_token_use(self, token_id: UUID, *, used_at: datetime) -> None:
        """Record use without letting older requests move the timestamp backward.

        Args:
            token_id: Identifier of the token that authenticated.
            used_at: Successful authentication time.

        Raises:
            CredentialPersistenceError: If credential storage fails.
        """
        self.session.execute(
            update(ApiTokenRecord)
            .where(ApiTokenRecord.token_id == token_id)
            .values(last_used_at=func.greatest(ApiTokenRecord.last_used_at, used_at))
        )
        self.session.flush()

    def list_tokens(self, user_id: UUID) -> tuple[TokenMetadata, ...]:
        """List an owner's token history without loading token digests.

        Args:
            user_id: Identifier of the token owner.

        Returns:
            Safe token metadata ordered by creation time and identifier.
        """
        rows = self.session.execute(
            select(
                ApiTokenRecord.token_id,
                ApiTokenRecord.user_id,
                ApiTokenRecord.assignment_id,
                ApiTokenRecord.name,
                ApiTokenRecord.created_at,
                ApiTokenRecord.expires_at,
                ApiTokenRecord.last_used_at,
                ApiTokenRecord.revoked_at,
                ApiTokenRecord.selected_role,
                ApiTokenRecord.selected_scope,
                ApiTokenRecord.selected_group_id,
                AuthorizationAssignmentRecord.is_active,
            )
            .join(ApiTokenRecord.assignment)
            .where(ApiTokenRecord.user_id == user_id)
            .order_by(ApiTokenRecord.created_at, ApiTokenRecord.token_id)
        ).all()
        return tuple(
            TokenMetadata(
                token_id=row.token_id,
                user_id=row.user_id,
                assignment_id=row.assignment_id,
                name=row.name,
                created_at=row.created_at,
                expires_at=row.expires_at,
                last_used_at=row.last_used_at,
                revoked_at=row.revoked_at,
                role=AuthorizationRole(row.selected_role),
                scope=AuthorizationScope(row.selected_scope),
                group_id=row.selected_group_id,
                assignment_is_active=row.is_active,
            )
            for row in rows
        )

    @_protect_credential_errors
    def revoke_token(
        self, user_id: UUID, token_id: UUID, *, revoked_at: datetime
    ) -> bool:
        """Revoke an owned token, preserving the time of any earlier revocation.

        Args:
            user_id: Identifier of the expected token owner.
            token_id: Identifier of the token to revoke.
            revoked_at: Revocation time to store for an active token.

        Returns:
            Whether the owner has a token with this ID, including an already revoked one.

        Raises:
            CredentialPersistenceError: If credential storage fails.
        """
        found = self.session.scalar(
            update(ApiTokenRecord)
            .where(
                ApiTokenRecord.user_id == user_id, ApiTokenRecord.token_id == token_id
            )
            .values(revoked_at=func.coalesce(ApiTokenRecord.revoked_at, revoked_at))
            .returning(ApiTokenRecord.token_id)
        )
        self.session.flush()
        return found is not None

    @_protect_credential_errors
    def create_management_session(
        self,
        *,
        user_id: UUID,
        digest: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> TokenManagementSessionRecord:
        """Store a management session after the service validates its lifetime.

        Args:
            user_id: Identifier of the session owner.
            digest: SHA-256 digest used to look up the credential.
            created_at: Time when the session was created.
            expires_at: Time after which the session cannot authenticate.

        Returns:
            Newly stored management-session record.

        Raises:
            CredentialPersistenceError: If credential storage fails.
        """
        record = TokenManagementSessionRecord(
            user_id=user_id, digest=digest, created_at=created_at, expires_at=expires_at
        )
        self.session.add(record)
        self.session.flush([record])
        return record

    @_protect_credential_errors
    def get_active_management_session(
        self, digest: str, *, now: datetime
    ) -> TokenManagementSessionRecord | None:
        """Find a usable management session belonging to an active user.

        Args:
            digest: SHA-256 digest of the presented session credential.
            now: Current time used to enforce expiration.

        Returns:
            Matching session, or `None` when it cannot authenticate.

        Raises:
            CredentialPersistenceError: If credential storage fails.
        """
        return self.session.scalar(
            select(TokenManagementSessionRecord)
            .join(TokenManagementSessionRecord.owner)
            .options(joinedload(TokenManagementSessionRecord.owner))
            .where(
                TokenManagementSessionRecord.digest == digest,
                TokenManagementSessionRecord.revoked_at.is_(None),
                TokenManagementSessionRecord.expires_at > now,
                SabUserRecord.is_active,
            )
        )
