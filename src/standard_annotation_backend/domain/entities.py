"""Define the values used to refresh, store, and validate entity catalogs.

An entity is the subject an annotation describes, identified by `db_object_id`.
Each configured entity source supplies a complete catalog of the entities it
provides. A refresh loads every valid row of the source's GPI file and rejects
the rest. Two rejection rules apply only to catalogs and are defined here: a row
is rejected when another row of the file lists the same identifier
(`reject_repeated_identifiers`) or when another source has the identifier active
(`EntityCollision`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from go_standard_annotation_schema.datamodel import Entity
from go_standard_annotation_schema.io import Record
from pydantic import Field

from standard_annotation_backend.domain.refresh import (
    MAX_REJECTION_REASON_LENGTH,
    RejectionReport,
    RowRejection,
)
from standard_annotation_backend.domain.schema_artifacts import SCHEMA_PACKAGE_VERSION
from standard_annotation_backend.domain.stored_json import StoredJson

GPI_SOURCE_FORMAT = "gpi-2.0"
"""Format recorded with every entity catalog snapshot."""


@dataclass(frozen=True, slots=True)
class EntityCatalog:
    """Contain the entities of one source that a refresh stores as a snapshot.

    Attributes:
        source_format: Short identifier for the format and version that was read,
            `gpi-2.0`.
        source_metadata: The GPI header as JSON-compatible values, stored with the
            snapshot for reference. Validation and activation never read it.
        schema_version: The `go-standard-annotation-schema` version the rows were
            checked with.
        records: The source's valid entities with their line numbers, each
            identifier once, in source order.
    """

    source_format: str
    source_metadata: dict[str, object]
    schema_version: str
    records: tuple[Record[Entity], ...]


@dataclass(frozen=True, slots=True)
class EntitySnapshotDetail:
    """Describe how an entity catalog snapshot's file was read and activated.

    Attributes:
        schema_version: The `go-standard-annotation-schema` version the rows were
            checked with.
        rejected_collisions: Whether activation rejected any row as a collision.
    """

    schema_version: str
    rejected_collisions: bool

    def reusable(self) -> bool:
        """Return whether a refresh of the same file may keep this snapshot.

        A different schema package version can accept or reject different rows,
        and a collision depends on other sources' catalogs rather than the file,
        so either makes a refresh read the file again.
        """
        return (
            self.schema_version == SCHEMA_PACKAGE_VERSION
            and not self.rejected_collisions
        )


def reject_repeated_identifiers(
    records: Sequence[Record[Entity]],
) -> tuple[tuple[Record[Entity], ...], tuple[RowRejection, ...]]:
    """Keep the records whose identifier appears once and reject the rest.

    An entity catalog lists each `db_object_id` on one line. When several lines
    list the same identifier there is no way to tell which describes the entity,
    so every one of them is rejected with code `duplicate`, and none of them is
    loaded until the file is fixed. Each rejection names the first other line
    that lists the identifier.

    Args:
        records: Valid records in line order, one per line, so no two share a
            `line_number`. Rows rejected for other reasons are not included, so
            they never make a valid row a repeat.

    Returns:
        The records whose identifier appears once, in their original order, and a
        `duplicate` rejection for each other record, in line order.
    """
    lines: defaultdict[str, list[int]] = defaultdict(list)
    for record in records:
        lines[record.item.db_object_id].append(record.line_number)
    kept: list[Record[Entity]] = []
    rejections: list[RowRejection] = []
    for record in records:
        same = lines[record.item.db_object_id]
        if len(same) == 1:
            kept.append(record)
            continue
        other = next(line for line in same if line != record.line_number)
        rejections.append(
            RowRejection(
                record.line_number,
                "duplicate",
                f"db_object_id also appears on line {other}",
            )
        )
    return tuple(kept), tuple(rejections)


@dataclass(frozen=True, slots=True)
class EntityCollision:
    """Describe a new catalog row whose identifier another source has active.

    An identifier is active in at most one source, and the source that activated
    it first keeps it, so the row is rejected.

    Attributes:
        db_object_id: The identifier.
        line_number: One-based line number of the row in the new catalog's file.
        source_key: The source whose active catalog lists the identifier.
    """

    db_object_id: str
    line_number: int
    source_key: str

    def to_rejection(self) -> RowRejection:
        """Return the `collision` rejection reported for the row."""
        reason = f"db_object_id is active in source {self.source_key}"
        return RowRejection(
            self.line_number, "collision", reason[:MAX_REJECTION_REASON_LENGTH]
        )


@dataclass(frozen=True, slots=True)
class EntityRefreshResult:
    """Describe one committed catalog replacement.

    Attributes:
        snapshot_id: The activated snapshot.
        source_key: The configured entity source.
        source_type: `github` or `https`.
        source_locator: Where the file was fetched from.
        source_revision: The resolved commit, or `None` for HTTPS.
        source_checksum: SHA-256 of the fetched bytes.
        data_rows: Data rows in the file, including rejected ones.
        entity_count: Entities in the activated catalog.
        records_rejected: Rows that were not loaded.
        added_count: Identifiers the source's previous catalog did not list.
        retained_count: Identifiers both catalogs list.
        removed_count: Identifiers only the previous catalog listed.
        rejection_report: The rows that were not loaded.
    """

    snapshot_id: UUID
    source_key: str
    source_type: str
    source_locator: str
    source_revision: str | None
    source_checksum: str
    data_rows: Annotated[int, Field(ge=0)]
    entity_count: Annotated[int, Field(ge=0)]
    records_rejected: Annotated[int, Field(ge=0)]
    added_count: Annotated[int, Field(ge=0)]
    retained_count: Annotated[int, Field(ge=0)]
    removed_count: Annotated[int, Field(ge=0)]
    rejection_report: RejectionReport

    def to_job_result(self) -> dict[str, object]:
        """Return the result as a JSON-compatible dict for the job record."""
        return STORED_ENTITY_REFRESH_RESULT.dump(self)

    def to_progress(self) -> dict[str, int]:
        """Return the counts reported as job progress and in the audit event."""
        return {
            "data_rows": self.data_rows,
            "entity_count": self.entity_count,
            "records_rejected": self.records_rejected,
            "added_count": self.added_count,
            "retained_count": self.retained_count,
            "removed_count": self.removed_count,
        }


@dataclass(frozen=True, slots=True)
class EntityRefreshUnchangedResult:
    """Describe a refresh that was skipped because the active catalog holds its file.

    The active catalog was read from the same file with the same reader version,
    and `EntitySnapshotDetail.reusable` is true for it, so the file was not read
    again.

    Attributes:
        source_key: The configured entity source.
        snapshot_id: The active snapshot, which already holds the file.
        snapshot_job_id: The job that activated it. That job's result lists the
            file's rejected rows.
        source_checksum: SHA-256 of the fetched bytes.
    """

    source_key: str
    snapshot_id: UUID
    snapshot_job_id: UUID
    source_checksum: str

    def to_job_result(self) -> dict[str, object]:
        """Return the result as a JSON-compatible dict for the job record."""
        return STORED_ENTITY_UNCHANGED_RESULT.dump(self)


STORED_ENTITY_REFRESH_RESULT = StoredJson(
    EntityRefreshResult, label="entity refresh result"
)
"""The stored form of the result of a GPI refresh that activated a catalog."""

STORED_ENTITY_UNCHANGED_RESULT = StoredJson(
    EntityRefreshUnchangedResult, label="unchanged entity refresh result"
)
"""The stored form of the result of a GPI refresh skipped as unchanged."""
