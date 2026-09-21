"""Synchronize local authorizations from an immutable go-site revision."""

import sys
from typing import Annotated

import httpx2
from pydantic import BaseModel, StringConstraints, ValidationError
from pydantic_settings import SettingsError
from sqlalchemy.exc import SQLAlchemyError

from standard_annotation_backend.auth.users_yaml import InvalidUsersDocumentError
from standard_annotation_backend.config import get_settings
from standard_annotation_backend.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from standard_annotation_backend.persistence.unit_of_work import (
    create_unit_of_work_factory,
)
from standard_annotation_backend.services.authorization_sync_service import (
    AuthorizationSyncService,
)

SOURCE_REPOSITORY = "geneontology/go-site"
SOURCE_REF = "master"
SOURCE_PATH = "metadata/users.yaml"
GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 30


class AuthorizationSourceUnavailableError(Exception):
    """Report that the configured authorization source could not be retrieved."""


class _GitCommit(BaseModel):
    """Validate the immutable commit returned by GitHub."""

    sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


def fetch_users_yaml() -> tuple[str, str]:
    """Fetch `users.yaml` and the exact go-site commit that supplied it.

    Returns:
        The complete YAML document and its immutable source commit SHA.

    Raises:
        AuthorizationSourceUnavailableError: If GitHub cannot provide a valid
            commit or UTF-8 source document.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        commit_response = httpx2.get(
            f"{GITHUB_API}/repos/{SOURCE_REPOSITORY}/commits/{SOURCE_REF}",
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        if commit_response.status_code != 200:
            raise AuthorizationSourceUnavailableError
        commit = _GitCommit.model_validate(commit_response.json())
        source_response = httpx2.get(
            f"{GITHUB_API}/repos/{SOURCE_REPOSITORY}/contents/{SOURCE_PATH}",
            params={"ref": commit.sha},
            headers={**headers, "Accept": "application/vnd.github.raw+json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        if source_response.status_code != 200:
            raise AuthorizationSourceUnavailableError
        yaml_text = source_response.content.decode("utf-8")
    except (
        httpx2.RequestError,
        UnicodeDecodeError,
        ValueError,
        ValidationError,
        AuthorizationSourceUnavailableError,
    ):
        raise AuthorizationSourceUnavailableError(
            "go-site users.yaml is unavailable"
        ) from None
    return yaml_text, commit.sha


def synchronize() -> None:
    """Retrieve current go-site authorizations and apply them locally.

    Raises:
        AuthorizationSourceUnavailableError: If GitHub cannot provide the source.
        InvalidUsersDocumentError: If the source document is invalid.
        InvalidAuthorizationSyncSourceError: If source provenance is invalid.
        SQLAlchemyError: If database synchronization fails.
    """
    yaml_text, commit_sha = fetch_users_yaml()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    try:
        unit_of_work_factory = create_unit_of_work_factory(
            create_session_factory(engine)
        )
        result = AuthorizationSyncService(unit_of_work_factory).synchronize(
            yaml_text=yaml_text,
            source_repository=SOURCE_REPOSITORY,
            source_commit_sha=commit_sha,
        )
    finally:
        engine.dispose()
    print(f"Synchronized {result.source_repository}@{result.source_commit_sha}")
    print(f"Sync ID: {result.sync_id}")
    print(
        f"Active state: {result.user_count} users, "
        f"{result.group_count} groups, {result.assignment_count} assignments"
    )


def main() -> int:
    """Run manual synchronization with concise operator-facing failures.

    Returns:
        Zero after a successful sync, otherwise one.
    """
    try:
        synchronize()
    except AuthorizationSourceUnavailableError as error:
        print(str(error), file=sys.stderr)
        return 1
    except InvalidUsersDocumentError as error:
        print(
            f"go-site users.yaml failed validation with {len(error.errors)} issue(s)",
            file=sys.stderr,
        )
        return 1
    except (ValidationError, SettingsError):
        print("SAB configuration is invalid", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("Authorization synchronization failed in PostgreSQL", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
