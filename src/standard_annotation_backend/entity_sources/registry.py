"""Look up entity sources configured in the registry file."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from standard_annotation_backend.config import EntitySourceSettings
from standard_annotation_backend.domain.entities import UnknownEntitySourceError


@dataclass(frozen=True, slots=True)
class ConfiguredEntitySource:
    """Describe one configured entity source.

    Attributes:
        key: Stable source key stored with the source's catalogs and jobs.
        format: Serialization format used to parse the source.
        url: HTTPS URL that SAB retrieves.
    """

    key: str
    format: Literal["gpi"]
    url: str


class EntitySourceRegistry:
    """Provide configured entity sources by key."""

    def __init__(self, sources: Mapping[str, EntitySourceSettings]) -> None:
        self._sources = dict(sources)

    @property
    def keys(self) -> tuple[str, ...]:
        """Return the configured source keys in sorted order."""
        return tuple(sorted(self._sources))

    def source(self, key: str) -> ConfiguredEntitySource:
        """Return one configured source.

        Raises:
            UnknownEntitySourceError: If the key is not configured.
        """
        settings = self._sources.get(key)
        if settings is None:
            raise UnknownEntitySourceError(key)
        return ConfiguredEntitySource(
            key=key, format=settings.format, url=str(settings.url)
        )
