"""Expose the application version recorded in installed package metadata."""

from importlib.metadata import version as distribution_version

_DISTRIBUTION_NAME = "standard-annotation-backend"


def get_application_version() -> str:
    """Return the version assigned when the SAB package was built or installed."""
    return distribution_version(_DISTRIBUTION_NAME)
