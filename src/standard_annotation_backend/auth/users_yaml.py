"""Parse go-site users.yaml into validated SAB authorization entries."""

from typing import Annotated, Self

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from yaml.nodes import MappingNode, Node

from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
)
from standard_annotation_backend.domain.validation import (
    ValidationIssue,
    validation_issues,
)
from standard_annotation_backend.validation_types import TrimmedNonBlankString


def _require_yaml_sequence(value: object) -> list[object]:
    """Require a YAML sequence before Pydantic converts it to a tuple.

    Args:
        value: Parsed YAML value to validate.

    Returns:
        The original list when it is a YAML sequence.

    Raises:
        PydanticCustomError: If the value is not a YAML sequence.
    """
    if not isinstance(value, list):
        raise PydanticCustomError("sequence_required", "a YAML sequence is required")
    return value


class SabAuthorizationEntry(BaseModel):
    """Validate one explicit SAB role, scope, and optional ownership group.

    Self and group scopes require a nonblank group. Global scope prohibits the
    group key altogether, including an explicit null value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AuthorizationRole
    scope: AuthorizationScope
    group: TrimmedNonBlankString | None = None

    @model_validator(mode="after")
    def validate_group(self) -> Self:
        """Require the group's presence to agree with the selected scope."""
        if self.scope is AuthorizationScope.GLOBAL:
            if "group" in self.model_fields_set:
                raise PydanticCustomError(
                    "group_prohibited", "global scope prohibits a group"
                )
        elif self.group is None:
            raise PydanticCustomError(
                "group_required", "self and group scopes require a group"
            )
        return self


class UserAccounts(BaseModel):
    """Read the optional GitHub identity while ignoring unrelated accounts."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    github: TrimmedNonBlankString | None = None

    @field_validator("github")
    @classmethod
    def normalize_login(cls, value: str | None) -> str | None:
        """Match GitHub identities without regard to letter case."""
        return value.lower() if value is not None else None


class UserAuthorizations(BaseModel):
    """Read SAB grants independently of other applications' permissions."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    sab: Annotated[
        tuple[SabAuthorizationEntry, ...], BeforeValidator(_require_yaml_sequence)
    ] = ()

    @field_validator("sab")
    @classmethod
    def normalize_grants(
        cls, grants: tuple[SabAuthorizationEntry, ...]
    ) -> tuple[SabAuthorizationEntry, ...]:
        """Deduplicate exact grants and order them deterministically."""
        return tuple(
            sorted(
                set(grants),
                key=lambda grant: (grant.role, grant.scope, grant.group or ""),
            )
        )


class SabUserEntry(BaseModel):
    """Read one go-site person without interpreting legacy group membership."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    nickname: str | None = None
    accounts: UserAccounts = Field(default_factory=UserAccounts)
    authorizations: UserAuthorizations = Field(default_factory=UserAuthorizations)

    @model_validator(mode="after")
    def require_grant_identity(self) -> Self:
        """Require a GitHub identity for any person who has a SAB grant."""
        if self.authorizations.sab and self.accounts.github is None:
            raise PydanticCustomError(
                "github_login_required", "SAB authorizations require accounts.github"
            )
        return self


class UsersDocument(
    RootModel[
        Annotated[tuple[SabUserEntry, ...], BeforeValidator(_require_yaml_sequence)]
    ]
):
    """Validate the complete sequence of go-site people before synchronization."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def reject_duplicate_identities(self) -> Self:
        """Reject ambiguous repeated GitHub identities even with differing case."""
        seen: set[str] = set()
        for user in self.root:
            login = user.accounts.github
            if login is not None:
                if login in seen:
                    raise PydanticCustomError(
                        "duplicate_github_login", "GitHub logins must be unique"
                    )
                seen.add(login)
        return self


class InvalidUsersDocumentError(Exception):
    """Report validation issues without retaining the source document or inputs."""

    def __init__(self, errors: tuple[ValidationIssue, ...]) -> None:
        super().__init__("invalid users document")
        self.errors = errors


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Reject mapping keys that would otherwise silently overwrite earlier values."""

    def construct_mapping(self, node: Node, deep: bool = False) -> dict[object, object]:
        """Build a mapping after rejecting duplicate or unhashable keys.

        Args:
            node: YAML node representing the mapping.
            deep: Whether child objects should also be constructed deeply.

        Returns:
            Parsed mapping with unique keys.

        Raises:
            yaml.YAMLError: If a key is duplicated or cannot be compared.
        """
        if isinstance(node, MappingNode):
            self.flatten_mapping(node)
            seen: set[object] = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    if key in seen:
                        raise yaml.YAMLError("duplicate mapping key")
                    seen.add(key)
                except TypeError:
                    raise yaml.YAMLError("invalid mapping key") from None
        return super().construct_mapping(node, deep=deep)


def parse_users_yaml(yaml_text: str) -> UsersDocument:
    """Safely parse and validate an entire users.yaml document.

    Args:
        yaml_text: Complete UTF-8-decoded YAML document.

    Returns:
        A validated document with normalized GitHub logins and grants.

    Raises:
        InvalidUsersDocumentError: If YAML syntax, shape, or SAB entries are invalid.
            Issues contain stable codes and input paths, never source values.
    """
    try:
        payload = yaml.load(yaml_text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError:
        raise InvalidUsersDocumentError(
            (
                ValidationIssue(
                    location=(), message="invalid YAML document", type="invalid_yaml"
                ),
            )
        ) from None
    try:
        return UsersDocument.model_validate(payload)
    except ValidationError as error:
        raise InvalidUsersDocumentError(validation_issues(error)) from None
