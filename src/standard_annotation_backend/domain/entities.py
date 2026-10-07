"""Define the values used to refresh, publish, and validate entity catalogs.

An entity is the subject an annotation describes, identified by `db_object_id`.
Each configured entity source supplies a complete catalog of the entities it
provides. These values do not depend on the file format the catalog came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from go_standard_annotation_schema.datamodel import Entity

from standard_annotation_backend.domain.errors import InvalidInputError
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    SourceProvenance,
    TerminalRefreshError,
)


@dataclass(frozen=True, slots=True)
class EntityRecord:
    """Hold one validated entity and the source line it came from.

    Attributes:
        line_number: One-based physical line number of the record in the
            source file. A source may repeat an identifier, so this number,
            not `entity.db_object_id`, distinguishes records.
        entity: The schema-validated entity.
    """

    line_number: int
    entity: Entity


@dataclass(frozen=True, slots=True)
class EntityCatalog:
    """Contain one source's complete, validated set of entity records.

    Attributes:
        source: The retrieved source that supplied the catalog.
        source_format: Short identifier for the serialization format and version
            that was parsed, such as `gpi-2.0`.
        source_metadata: JSON-compatible, format-specific metadata stored with
            the catalog for reference, such as the GPI header. Entity
            validation and publication never read it.
        records: Validated entity records in source order.
        warnings: Messages about problems that did not stop parsing.
    """

    source: SourceProvenance
    source_format: str
    source_metadata: dict[str, object]
    records: tuple[EntityRecord, ...]
    warnings: tuple[str, ...]


class UnknownDbObjectIdError(InvalidInputError):
    """Report a `db_object_id` that is not in the active entity catalog.

    Attributes:
        db_object_id: The identifier that is not in the catalog.
    """

    code = "unknown_db_object_id"
    message = "Annotation db_object_id is not in the active entity catalog"

    def __init__(self, db_object_id: str) -> None:
        self.db_object_id = db_object_id
        super().__init__()

    def issue_location(self) -> tuple[str | int, ...]:
        """Return the `db_object_id` field."""
        return ("db_object_id",)


class EntityCandidateConflictError(TerminalRefreshError):
    """Report staged catalog data that cannot be used for a job.

    Raised when a job's staged catalog differs from a new parse of its source, is
    incomplete or altered, is older than the active catalog, or belongs to a job
    that has already finished.
    """

    failure_code = RefreshFailureCode.CANDIDATE_CONFLICT

    def __init__(self) -> None:
        super().__init__(
            "Entity catalog candidate conflicts with its job or publication state"
        )


class EntityCatalogCollisionError(TerminalRefreshError):
    """Report exact identifiers already supplied by another source.

    The identifiers are available as `colliding_ids`. The exception message
    leaves them out, so logs stay short and contain no source data.
    """

    failure_code = RefreshFailureCode.CATALOG_COLLISION

    def __init__(self, colliding_ids: tuple[str, ...]) -> None:
        self.colliding_ids = tuple(sorted(set(colliding_ids)))
        super().__init__("Entity identifiers conflict with another source")


@dataclass(frozen=True, slots=True)
class EntityRemovalImpact:
    """Identify the existing annotations that reference a removed identifier."""

    db_object_id: str
    annotation_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class EntityRefreshResult:
    """Describe one committed catalog replacement and its removal impacts."""

    snapshot_id: UUID
    source_key: str
    source_type: str
    source_locator: str
    source_revision: str | None
    source_checksum: str
    source_record_count: int
    active_identifier_count: int
    added_count: int
    retained_count: int
    removed_count: int
    warnings: tuple[str, ...]
    removal_impacts: tuple[EntityRemovalImpact, ...]

    def to_job_result(self) -> dict[str, object]:
        """Return the result as a JSON-compatible dict for the job record."""
        return {
            "snapshot_id": str(self.snapshot_id),
            "source_key": self.source_key,
            "source_type": self.source_type,
            "source_locator": self.source_locator,
            "source_revision": self.source_revision,
            "source_checksum": self.source_checksum,
            "source_record_count": self.source_record_count,
            "active_identifier_count": self.active_identifier_count,
            "added_count": self.added_count,
            "retained_count": self.retained_count,
            "removed_count": self.removed_count,
            "warnings": list(self.warnings),
            "warning_count": len(self.warnings),
            "removal_impacts": [
                {
                    "db_object_id": impact.db_object_id,
                    "annotation_ids": [str(value) for value in impact.annotation_ids],
                }
                for impact in self.removal_impacts
            ],
        }

    @classmethod
    def from_job_result(cls, value: dict[str, object]) -> EntityRefreshResult:
        """Rebuild a result from the dict produced by `to_job_result`."""
        return cls(
            snapshot_id=UUID(cast(str, value["snapshot_id"])),
            source_key=cast(str, value["source_key"]),
            source_type=cast(str, value["source_type"]),
            source_locator=cast(str, value["source_locator"]),
            source_revision=cast(str | None, value.get("source_revision")),
            source_checksum=cast(str, value["source_checksum"]),
            source_record_count=cast(int, value["source_record_count"]),
            active_identifier_count=cast(int, value["active_identifier_count"]),
            added_count=cast(int, value["added_count"]),
            retained_count=cast(int, value["retained_count"]),
            removed_count=cast(int, value["removed_count"]),
            warnings=tuple(cast(list[str], value["warnings"])),
            removal_impacts=tuple(
                EntityRemovalImpact(
                    cast(str, impact["db_object_id"]),
                    tuple(
                        UUID(item) for item in cast(list[str], impact["annotation_ids"])
                    ),
                )
                for impact in cast(list[dict[str, object]], value["removal_impacts"])
            ),
        )


@dataclass(frozen=True, slots=True)
class EntityRefreshUnchangedResult:
    """Describe a refresh whose retrieved file matches the active catalog."""

    source_key: str
    snapshot_id: UUID
    source_checksum: str

    def to_job_result(self) -> dict[str, object]:
        """Return the result as a JSON-compatible dict for the job record."""
        return {
            "source_key": self.source_key,
            "snapshot_id": str(self.snapshot_id),
            "source_checksum": self.source_checksum,
        }


@dataclass(frozen=True, slots=True)
class EntityCatalogRetirementResult:
    """Describe the outcome of retiring one source's active catalog.

    Attributes:
        source_key: The retired source.
        retired: Whether an active catalog was retired.
        snapshot_id: The retired snapshot, when one was retired.
        removal_impacts: Every removed identifier with the active annotations
            that still reference it.
    """

    source_key: str
    retired: bool
    snapshot_id: UUID | None
    removal_impacts: tuple[EntityRemovalImpact, ...]

    def to_job_result(self) -> dict[str, object]:
        """Return the result as a JSON-compatible dict for the job record."""
        if not self.retired:
            return {"source_key": self.source_key, "retired": False}
        return {
            "source_key": self.source_key,
            "retired": True,
            "snapshot_id": str(self.snapshot_id),
            "removed_count": len(self.removal_impacts),
            "removal_impacts": [
                {
                    "db_object_id": impact.db_object_id,
                    "annotation_ids": [str(value) for value in impact.annotation_ids],
                }
                for impact in self.removal_impacts
            ],
        }

    @classmethod
    def from_job_result(cls, value: dict[str, object]) -> EntityCatalogRetirementResult:
        """Rebuild a result from the dict produced by `to_job_result`."""
        if value["retired"] is not True:
            return cls(cast(str, value["source_key"]), False, None, ())
        return cls(
            source_key=cast(str, value["source_key"]),
            retired=True,
            snapshot_id=UUID(cast(str, value["snapshot_id"])),
            removal_impacts=tuple(
                EntityRemovalImpact(
                    cast(str, impact["db_object_id"]),
                    tuple(
                        UUID(item) for item in cast(list[str], impact["annotation_ids"])
                    ),
                )
                for impact in cast(list[dict[str, object]], value["removal_impacts"])
            ),
        )
