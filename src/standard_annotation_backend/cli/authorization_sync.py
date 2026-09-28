"""Synchronize local authorizations from an immutable go-site revision."""

import sys

from pydantic import ValidationError
from pydantic_settings import SettingsError
from sqlalchemy.exc import SQLAlchemyError

from standard_annotation_backend.auth.authorization_source import (
    AuthorizationSourceClient,
    AuthorizationSourceUnavailableError,
)
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


def fetch_users_yaml() -> tuple[str, str]:
    """Fetch `users.yaml` and the exact go-site commit that supplied it.

    Returns:
        The complete YAML document and its immutable source commit SHA.

    Raises:
        AuthorizationSourceUnavailableError: If GitHub cannot provide a valid
            commit or UTF-8 source document.
    """
    settings = get_settings()
    document = AuthorizationSourceClient(
        repository=settings.authorization_source_repository,
        ref=settings.authorization_source_ref,
        path=settings.authorization_source_path,
    ).fetch()
    return document.yaml_text, document.source_commit_sha


def synchronize() -> None:
    """Retrieve current go-site authorizations and apply them locally.

    Raises:
        AuthorizationSourceUnavailableError: If GitHub cannot provide the source.
        InvalidUsersDocumentError: If the source document is invalid.
        InvalidAuthorizationSyncSourceError: If source provenance is invalid.
        SQLAlchemyError: If database synchronization fails.
    """
    settings = get_settings()
    document = AuthorizationSourceClient(
        repository=settings.authorization_source_repository,
        ref=settings.authorization_source_ref,
        path=settings.authorization_source_path,
    ).fetch()
    engine = create_database_engine(settings.database_url)
    try:
        unit_of_work_factory = create_unit_of_work_factory(
            create_session_factory(engine)
        )
        result = AuthorizationSyncService(unit_of_work_factory).synchronize(
            yaml_text=document.yaml_text,
            source_repository=document.source_repository,
            source_commit_sha=document.source_commit_sha,
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
