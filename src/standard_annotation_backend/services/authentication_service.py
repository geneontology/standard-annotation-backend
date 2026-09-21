"""Authenticate opaque user tokens and record their use in an owned transaction."""

import re
from datetime import UTC, datetime

from standard_annotation_backend.auth.secrets import API_TOKEN_PREFIX, digest_secret
from standard_annotation_backend.domain.auth import (
    AuthenticationRequiredError,
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.persistence.unit_of_work import (
    UnitOfWorkFactory,
    credential_unit_of_work,
)

_TOKEN_PATTERN = re.compile(rf"{re.escape(API_TOKEN_PREFIX)}[A-Za-z0-9_-]{{43}}")


class AuthenticationService:
    """Resolve current user identity without retaining or disclosing the secret."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def authenticate(self, raw_token: str) -> RequestContext:
        """Validate a bearer token and commit its last-use timestamp before returning.

        The token's original selection must still match its active assignment.
        Authentication owns a separate transaction from the requested data operation;
        use remains recorded even when that subsequent operation fails. A storage
        failure prevents authentication and is translated by the credential boundary.

        Args:
            raw_token: Untrusted opaque token supplied by the caller.

        Returns:
            Immutable user identity and the token's selected authorization.

        Raises:
            AuthenticationRequiredError: If the credential cannot authenticate.
            CredentialPersistenceError: If credential lookup or recording use fails.
        """
        if _TOKEN_PATTERN.fullmatch(raw_token) is None:
            raise AuthenticationRequiredError
        now = datetime.now(UTC)
        with credential_unit_of_work(self._unit_of_work_factory) as uow:
            token = uow.auth.get_active_token(digest_secret(raw_token), now=now)
            if token is None:
                raise AuthenticationRequiredError
            assignment = token.assignment
            group_id = None if assignment.group is None else assignment.group.group_key
            if (
                token.selected_role != assignment.role
                or token.selected_scope != assignment.scope
                or token.selected_group_id != group_id
            ):
                raise AuthenticationRequiredError
            context = RequestContext(
                actor_id=str(token.user_id),
                token_id=token.token_id,
                token_name=token.name,
                role=AuthorizationRole(token.selected_role),
                scope=AuthorizationScope(token.selected_scope),
                group_id=token.selected_group_id,
            )
            uow.auth.record_token_use(token.token_id, used_at=now)
            uow.commit()
        return context
