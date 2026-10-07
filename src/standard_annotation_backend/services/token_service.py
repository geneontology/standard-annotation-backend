"""Issue and revoke bearer tokens within narrowly scoped management sessions."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
)

from standard_annotation_backend.auth.github_oauth import GitHubOAuthClient
from standard_annotation_backend.auth.secrets import (
    API_TOKEN_PREFIX,
    TOKEN_MANAGEMENT_SESSION_PREFIX,
    digest_secret,
    generate_secret,
    secret_matches,
)
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
)
from standard_annotation_backend.domain.tokens import (
    TOKEN_MANAGEMENT_SESSION_TTL,
    InvalidExpirationError,
    InvalidTokenError,
    ManagementSessionRequiredError,
    OAuthCallbackError,
    OAuthIdentityNotAllowedError,
    OAuthStateError,
    TokenContextNotFoundError,
    TokenMetadata,
    TokenNotFoundError,
    resolve_expiration,
)
from standard_annotation_backend.domain.validation import (
    ValidationIssue,
    validation_issues,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
    credential_unit_of_work,
)
from standard_annotation_backend.services.audit_service import AuditService


class TokenCreateInput(BaseModel):
    """Validate an untrusted token name, context, and expiration timestamp."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    assignment_id: UUID
    expires_at: AwareDatetime


@dataclass(frozen=True, slots=True)
class ManagementSession:
    """Return a newly issued management credential only to the cookie boundary.

    Attributes:
        raw_secret: Credential to place in the management-session cookie.
        expires_at: Time after which the session cannot be used.
    """

    raw_secret: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TokenContext:
    """Describe one current authorization that the user may select for a token.

    Attributes:
        assignment_id: Stable identifier for the authorization assignment.
        role: Kind of action the assignment permits.
        scope: Ownership boundary for the assignment.
        group_id: Selected group, or `None` for global scope.
    """

    assignment_id: UUID
    role: AuthorizationRole
    scope: AuthorizationScope
    group_id: str | None


@dataclass(frozen=True, slots=True)
class CreatedToken:
    """Return the raw credential once, alongside safely listable metadata.

    Attributes:
        raw_token: Newly generated bearer credential.
        metadata: Token details that are safe to retrieve later.
    """

    raw_token: str = field(repr=False)
    metadata: TokenMetadata


def _utc_now() -> datetime:
    """Return the current UTC time used for credential lifetime checks."""
    return datetime.now(UTC)


class TokenService:
    """Keep token ownership, management authentication, and audit commits together."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def complete_login(
        self,
        *,
        github: GitHubOAuthClient,
        code: str | None,
        state: str | None,
        expected_state: str | None,
        denied: bool = False,
    ) -> ManagementSession:
        """Verify OAuth and issue a 15-minute credential for token management only.

        Neither GitHub credentials nor the raw session secret are persisted.
        State is compared through fixed-length digests in constant time before
        exchanging any code or looking up the synchronized user. The current
        GitHub login must match a synchronized active user.

        Args:
            github: Client used to exchange the code and retrieve the GitHub profile.
            code: Authorization code returned by GitHub, when supplied.
            state: State value returned by GitHub, when supplied.
            expected_state: State value stored in the initiating browser's cookie.
            denied: Whether GitHub returned an OAuth error instead of authorization.

        Returns:
            Newly issued management-session secret and its expiration time.

        Raises:
            OAuthStateError: If the callback is not bound to the initiating browser.
            OAuthCallbackError: If GitHub denied or omitted the authorization code.
            OAuthIdentityNotAllowedError: If the GitHub user is not active locally.
            OAuthUpstreamError: If GitHub cannot verify the user.
            CredentialPersistenceError: If credential storage fails.
        """
        if (
            not state
            or not expected_state
            or not secret_matches(state, digest_secret(expected_state))
        ):
            raise OAuthStateError
        if denied or not code:
            raise OAuthCallbackError
        identity = github.get_user(github.exchange_code(code))
        now = _utc_now()
        with credential_unit_of_work(self._unit_of_work_factory) as uow:
            owner = uow.auth.get_user_by_github_login(identity.github_login)
            if owner is None:
                raise OAuthIdentityNotAllowedError
            raw, digest = generate_secret(TOKEN_MANAGEMENT_SESSION_PREFIX)
            expires_at = now + TOKEN_MANAGEMENT_SESSION_TTL
            uow.auth.create_management_session(
                user_id=owner.user_id,
                digest=digest,
                created_at=now,
                expires_at=expires_at,
            )
            result = ManagementSession(raw, expires_at)
            uow.commit()
            return result

    @staticmethod
    def _user_id(
        uow: SqlAlchemyUnitOfWork,
        raw_session: str | None,
        now: datetime,
    ) -> UUID:
        """Authenticate a management session and return its active user.

        Args:
            uow: Active unit of work used for the credential lookup.
            raw_session: Untrusted cookie value, when supplied.
            now: Current time used to enforce expiration.

        Returns:
            Identifier of the active user who owns the session.

        Raises:
            ManagementSessionRequiredError: If the session cannot authenticate.
        """
        if not raw_session or not raw_session.startswith(
            TOKEN_MANAGEMENT_SESSION_PREFIX
        ):
            raise ManagementSessionRequiredError
        session = uow.auth.get_active_management_session(
            digest_secret(raw_session), now=now
        )
        if session is None:
            raise ManagementSessionRequiredError
        return session.user_id

    def current_contexts(self, raw_session: str | None) -> tuple[TokenContext, ...]:
        """List current assignments belonging to the session's active user.

        Args:
            raw_session: Untrusted management-session cookie value.

        Returns:
            Current authorization choices available for new tokens.

        Raises:
            ManagementSessionRequiredError: If the session cannot authenticate.
            CredentialPersistenceError: If credential storage fails.
        """
        with credential_unit_of_work(self._unit_of_work_factory) as uow:
            user_id = self._user_id(uow, raw_session, _utc_now())
            return tuple(
                TokenContext(
                    grant.assignment_id,
                    AuthorizationRole(grant.role),
                    AuthorizationScope(grant.scope),
                    None if grant.group is None else grant.group.group_key,
                )
                for grant in uow.auth.list_active_assignments(user_id)
            )

    def create_token(self, raw_session: str | None, payload: object) -> CreatedToken:
        """Create a token and commit its digest with its audit event atomically.

        Args:
            raw_session: Untrusted management-session cookie value.
            payload: Untrusted token name, assignment, and expiration values.

        Returns:
            Raw bearer token and its safe metadata. The raw value cannot be
            retrieved again.

        Raises:
            ManagementSessionRequiredError: If the session cannot authenticate.
            InvalidTokenError: If the creation values or expiration are invalid.
            TokenContextNotFoundError: If the selected assignment is not owned and
                active.
            CredentialPersistenceError: If credential or audit storage fails.
        """
        now = _utc_now()
        with credential_unit_of_work(self._unit_of_work_factory) as uow:
            user_id = self._user_id(uow, raw_session, now)
            try:
                data = TokenCreateInput.model_validate(payload)
            except ValidationError as error:
                raise InvalidTokenError(validation_issues(error)) from None
            try:
                expires_at = resolve_expiration(data.expires_at, now)
            except InvalidExpirationError:
                issue = ValidationIssue(
                    location=("expires_at",),
                    message="Token expiration is outside the allowed range",
                    type=InvalidTokenError.code,
                )
                raise InvalidTokenError((issue,)) from None
            assignment = uow.auth.get_active_assignment(user_id, data.assignment_id)
            if assignment is None:
                raise TokenContextNotFoundError
            raw, digest = generate_secret(API_TOKEN_PREFIX)
            record = uow.auth.create_token(
                user_id=user_id,
                assignment_id=assignment.assignment_id,
                name=data.name,
                digest=digest,
                created_at=now,
                expires_at=expires_at,
            )
            metadata = TokenMetadata(
                token_id=record.token_id,
                user_id=user_id,
                assignment_id=assignment.assignment_id,
                name=record.name,
                created_at=now,
                expires_at=expires_at,
                last_used_at=None,
                revoked_at=None,
                role=AuthorizationRole(assignment.role),
                scope=AuthorizationScope(assignment.scope),
                group_id=None
                if assignment.group is None
                else assignment.group.group_key,
                assignment_is_active=True,
            )
            AuditService(uow.audit).record_token_created(
                user_id=user_id,
                token_id=record.token_id,
                assignment_id=assignment.assignment_id,
                expires_at=expires_at,
            )
            uow.commit()
            return CreatedToken(raw, metadata)

    def list_tokens(self, raw_session: str | None) -> tuple[TokenMetadata, ...]:
        """List the session owner's token history without loading token digests.

        Args:
            raw_session: Untrusted management-session cookie value.

        Returns:
            Current and historical token metadata ordered by creation time.

        Raises:
            ManagementSessionRequiredError: If the session cannot authenticate.
            CredentialPersistenceError: If credential storage fails.
        """
        with credential_unit_of_work(self._unit_of_work_factory) as uow:
            user_id = self._user_id(uow, raw_session, _utc_now())
            return uow.auth.list_tokens(user_id)

    def revoke_token(self, raw_session: str | None, token_id: UUID) -> None:
        """Revoke an owned token and commit its audit event in the same transaction.

        Args:
            raw_session: Untrusted management-session cookie value.
            token_id: Identifier of the token to revoke.

        Raises:
            ManagementSessionRequiredError: If the session cannot authenticate.
            TokenNotFoundError: If the user does not own a token with the identifier.
            CredentialPersistenceError: If credential or audit storage fails.
        """
        now = _utc_now()
        with credential_unit_of_work(self._unit_of_work_factory) as uow:
            user_id = self._user_id(uow, raw_session, now)
            if not uow.auth.revoke_token(user_id, token_id, revoked_at=now):
                raise TokenNotFoundError
            AuditService(uow.audit).record_token_revoked(
                user_id=user_id, token_id=token_id
            )
            uow.commit()
