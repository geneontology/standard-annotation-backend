"""Store, activate, look up, prune, and retire snapshots of reference data.

A snapshot is one stored version of an ontology, of the go-site files that GO
Rules read, or of an entity catalog. Each is a `reference_snapshot` row, a row
in its kind's detail table, and rows in its kind's data tables. This module is
the one place that defines how snapshots behave:

- Identity: a snapshot belongs to one source: a kind (`ontology`, `rules`,
  or `entity`) and a configured source key. It records the job that loaded it,
  its source document's provenance, and the reader version (see
  `domain.snapshots.READER_VERSIONS`) of the code that read the document.
- Activation: a refresh job inserts a snapshot and activates it in the job's
  own transaction, which deactivates the source's previous snapshot. At most one
  snapshot per source is active. A refresh skips its source when the active
  snapshot already holds the same content (`SourceContent`): the same source
  type, locator, checksum, and reader version, and for an ontology the same
  identifier prefixes and closure predicates. An entity refresh still reads a
  document with the same content when the active snapshot's rows were checked
  with another schema package version or its activation rejected a row as a
  collision (see `EntitySnapshotDetail.reusable`).
- What stays readable: `resolve` locks the snapshots it returns with
  PostgreSQL's `FOR KEY SHARE` lock until the transaction ends. Activation does
  not conflict with that lock, so refreshes never wait for readers, but pruning
  does, so a transaction never loses data it is reading. A reader given snapshot
  IDs from another transaction calls `require_readable`, which takes the same
  lock and raises `SnapshotPrunedError` instead of reading nothing.
- Decoded rule data: `resolve` indexes a rules snapshot's rule data once per
  process and caches it by snapshot ID. Stored snapshot data never changes and
  snapshot IDs are never reused, so a cached entry is always current for its
  snapshot.
- Retention: For each source with an active snapshot, the active snapshot and
  the one it replaced keep their data. Activation records the replaced snapshot
  in `replaced_snapshot_id`, so retention never depends on activation times.
  Every refresh job of a kind prunes the kind's other snapshots: their data rows
  are deleted and `pruned_at` is set, skipping any snapshot another transaction
  has locked. Snapshot rows and detail rows are kept as provenance. The replaced
  snapshot is kept so that no snapshot is pruned in the transaction that
  deactivates it; a reader whose query began just before that commit can still
  lock and read it. A snapshot activated when its source had no active snapshot,
  such as after the source was retired, replaced nothing.
- Retirement: `retire` deactivates the active snapshots of sources that are
  no longer configured and records `retired_at`. The kind's next refresh job
  prunes their data.
- Provenance: snapshot rows and detail rows are kept when data is pruned, so
  `reference_versions` can report where any resolved snapshot came from, such
  as for a QC report.
"""

import threading
from collections import OrderedDict
from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, and_, delete, or_, select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.ontology import OntologyCatalog
from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
    SourceProvenance,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    RULES_REFERENCE_DATA,
    ActiveSnapshots,
    ReferenceDataUnavailableError,
)
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    STORED_RULE_DATA,
    RuleDataIndex,
)
from standard_annotation_backend.domain.snapshots import (
    EntityCatalogUse,
    OntologySnapshotUse,
    ReferenceDataVersions,
    SnapshotKind,
    SourceContent,
    current_reader_version,
)
from standard_annotation_backend.domain.stored_json import StoredDataError
from standard_annotation_backend.persistence.models import (
    OntologyAltIdRecord,
    OntologyClosureRecord,
    OntologySnapshotDetailRecord,
    OntologyTermRecord,
    ReferenceSnapshotRecord,
    RetractedPublicationRecord,
    RuleDataRecord,
    StoredEntityRecord,
)


class SnapshotPrunedError(RuntimeError):
    """Report a read of snapshot data that was pruned or never stored.

    Readers raise this instead of returning nothing, which would look like every
    term being unknown or no publication being retracted. A caller that resolves
    snapshots and reads them in one transaction never meets it, because the
    snapshots stay locked; it means a caller read snapshots it resolved in an
    earlier transaction.

    Attributes:
        snapshot_ids: The snapshots whose data is missing.
    """

    def __init__(self, snapshot_ids: Collection[UUID]) -> None:
        self.snapshot_ids = tuple(sorted(snapshot_ids))
        super().__init__("snapshot data was pruned or never stored")


class _RuleDataCache:
    """Keep the indexed rule data of the rules snapshots read most recently.

    Stored rule data never changes and snapshot IDs are never reused, so an
    entry never goes stale and needs no invalidation. One cache serves every
    repository in the process; a lock guards it because web requests run on
    several threads.
    """

    def __init__(self, max_entries: int) -> None:
        self._entries: OrderedDict[UUID, RuleDataIndex] = OrderedDict()
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def get(self, snapshot_id: UUID) -> RuleDataIndex | None:
        """Return the snapshot's indexed rule data, if it is cached."""
        with self._lock:
            index = self._entries.get(snapshot_id)
            if index is not None:
                self._entries.move_to_end(snapshot_id)
            return index

    def put(self, snapshot_id: UUID, index: RuleDataIndex) -> None:
        """Cache the snapshot's indexed rule data, dropping the oldest entry."""
        with self._lock:
            self._entries[snapshot_id] = index
            self._entries.move_to_end(snapshot_id)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


_RULE_DATA_CACHE = _RuleDataCache(max_entries=3)
"""Indexed rule data by snapshot ID: room for the active snapshot, its
predecessor, and one more, so an activation never forces the active one out."""


class SnapshotRepository:
    """Store and read reference data snapshots in the caller's transaction.

    Args:
        session: Session owned by the calling unit of work.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def active(
        self, kind: SnapshotKind, source_key: str
    ) -> ReferenceSnapshotRecord | None:
        """Return the source's active snapshot without locking it."""
        return self.session.scalar(_active(kind, source_key))

    def lock_active(
        self, kind: SnapshotKind, source_key: str
    ) -> ReferenceSnapshotRecord | None:
        """Return the source's active snapshot, locked against pruning.

        The lock is held until the transaction ends. Activating a newer snapshot
        does not wait for it.
        """
        return self.session.scalar(
            _active(kind, source_key).with_for_update(read=True, key_share=True)
        )

    def active_source_keys(self, kind: SnapshotKind) -> tuple[str, ...]:
        """Return the source keys of the kind that have an active snapshot."""
        return tuple(
            self.session.scalars(
                select(ReferenceSnapshotRecord.source_key)
                .where(
                    ReferenceSnapshotRecord.kind == kind,
                    ReferenceSnapshotRecord.active.is_(True),
                )
                .order_by(ReferenceSnapshotRecord.source_key)
            )
        )

    def matching_active(
        self, kind: SnapshotKind, source_key: str, content: SourceContent
    ) -> ReferenceSnapshotRecord | None:
        """Return the source's active snapshot if it holds `content`.

        A refresh skips a document when this returns a snapshot. An entity
        refresh skips it only if `EntitySnapshotDetail.reusable` is also true.
        """
        record = self.active(kind, source_key)
        if record is None:
            return None
        prefixes: tuple[str, ...] = ()
        predicates: tuple[str, ...] = ()
        if kind is SnapshotKind.ONTOLOGY:
            detail = self.session.get(OntologySnapshotDetailRecord, record.snapshot_id)
            if detail is not None:
                prefixes = tuple(sorted(detail.identifier_prefixes))
                predicates = tuple(sorted(detail.loaded_predicates))
        stored = SourceContent(
            source_type=record.source_type,
            source_locator=record.source_locator,
            source_checksum=record.source_checksum,
            reader_version=record.reader_version,
            identifier_prefixes=prefixes,
            closure_predicates=predicates,
        )
        return record if stored == content else None

    def create(
        self,
        kind: SnapshotKind,
        source_key: str,
        job_id: UUID,
        provenance: SourceProvenance,
    ) -> ReferenceSnapshotRecord:
        """Insert an inactive snapshot for a job, read with this release's reader.

        The kind's repository then stores the snapshot's detail and data rows,
        and `activate` makes it active, all in the same transaction.
        """
        record = ReferenceSnapshotRecord(
            kind=kind,
            source_key=source_key,
            job_id=job_id,
            source_type=provenance.source_type,
            source_locator=provenance.source_locator,
            source_revision=provenance.source_revision,
            source_checksum=provenance.source_checksum,
            fetched_at=provenance.fetched_at,
            reader_version=current_reader_version(RefreshKindName(kind.value)),
            active=False,
        )
        self.session.add(record)
        self.session.flush([record])
        return record

    def activate(
        self, record: ReferenceSnapshotRecord
    ) -> ReferenceSnapshotRecord | None:
        """Make `record` its source's active snapshot, recording the one it replaces.

        Returns:
            The snapshot it replaces, if any. It keeps its data until a later
            refresh job of the kind prunes it, and readers that locked it keep
            reading it.
        """
        previous = self.active(record.kind, record.source_key)
        if previous is not None:
            previous.active = False
            # Flushed first: the partial unique index allows one active snapshot.
            self.session.flush([previous])
        record.active = True
        record.activated_at = datetime.now(UTC)
        record.replaced_snapshot_id = None if previous is None else previous.snapshot_id
        self.session.flush([record])
        return previous

    def resolve(self, ontologies: OntologyCatalog) -> ActiveSnapshots:
        """Return and lock the active snapshots that validation reads.

        These are the active snapshots of each configured ontology, of the
        go-site rule data, and of every entity catalog. One query finds them and
        locks each with `FOR KEY SHARE` until the transaction ends, so pruning
        cannot delete their data while the caller reads it. Refreshes can still
        activate newer snapshots meanwhile; the caller keeps reading these. The
        rule data's index is cached by snapshot ID, so later calls that find the
        same active rules snapshot skip decoding it.

        Raises:
            ReferenceDataUnavailableError: If a configured ontology or the rule
                data has no active snapshot, or the active rule data was stored
                by an earlier reader version and can no longer be decoded.
            StoredDataError: If rule data stored by this release's reader
                version cannot be decoded.
        """
        rows = self.session.execute(
            select(
                ReferenceSnapshotRecord.kind,
                ReferenceSnapshotRecord.source_key,
                ReferenceSnapshotRecord.snapshot_id,
                ReferenceSnapshotRecord.reader_version,
            )
            .where(
                ReferenceSnapshotRecord.active.is_(True),
                or_(
                    and_(
                        ReferenceSnapshotRecord.kind == SnapshotKind.ONTOLOGY,
                        ReferenceSnapshotRecord.source_key.in_(ontologies.keys),
                    ),
                    ReferenceSnapshotRecord.kind.in_(
                        (SnapshotKind.RULES, SnapshotKind.ENTITY)
                    ),
                ),
            )
            .order_by(ReferenceSnapshotRecord.kind, ReferenceSnapshotRecord.source_key)
            .with_for_update(read=True, key_share=True)
        ).all()
        ontology_versions = {
            row.source_key: row.snapshot_id
            for row in rows
            if row.kind is SnapshotKind.ONTOLOGY
        }
        entity_catalogs = {
            row.source_key: row.snapshot_id
            for row in rows
            if row.kind is SnapshotKind.ENTITY
        }
        rules = [row for row in rows if row.kind is SnapshotKind.RULES]
        missing = set(ontologies.keys) - set(ontology_versions)
        if not rules:
            missing.add(RULES_REFERENCE_DATA)
        if missing:
            raise ReferenceDataUnavailableError(missing)
        (rules_row,) = rules
        return ActiveSnapshots(
            ontology_versions=ontology_versions,
            rules_snapshot_id=rules_row.snapshot_id,
            rule_data=self._rule_data(rules_row.snapshot_id, rules_row.reader_version),
            entity_catalogs=entity_catalogs,
        )

    def require_readable(self, snapshot_ids: Collection[UUID]) -> None:
        """Lock snapshots resolved in another transaction and require their data.

        Raises:
            SnapshotPrunedError: If any of them was pruned or does not exist.
        """
        wanted = set(snapshot_ids)
        found = set(
            self.session.scalars(
                select(ReferenceSnapshotRecord.snapshot_id)
                .where(
                    ReferenceSnapshotRecord.snapshot_id.in_(sorted(wanted)),
                    ReferenceSnapshotRecord.pruned_at.is_(None),
                )
                .with_for_update(read=True, key_share=True)
            )
        )
        if found != wanted:
            raise SnapshotPrunedError(wanted - found)

    def reference_versions(self, snapshots: ActiveSnapshots) -> ReferenceDataVersions:
        """Return where each of `snapshots` came from, as QC reports record it.

        Snapshot and detail rows are kept when a snapshot's data is pruned, so this
        works for any resolved snapshots.
        """
        ontology_rows = self.session.execute(
            select(
                ReferenceSnapshotRecord.source_key,
                ReferenceSnapshotRecord.snapshot_id,
                OntologySnapshotDetailRecord.document_version,
                ReferenceSnapshotRecord.source_revision,
            )
            .join(
                OntologySnapshotDetailRecord,
                OntologySnapshotDetailRecord.snapshot_id
                == ReferenceSnapshotRecord.snapshot_id,
            )
            .where(
                ReferenceSnapshotRecord.snapshot_id.in_(
                    sorted(snapshots.ontology_versions.values())
                )
            )
            .order_by(ReferenceSnapshotRecord.source_key)
        )
        go_site_revision = self.session.scalar(
            select(ReferenceSnapshotRecord.source_revision).where(
                ReferenceSnapshotRecord.snapshot_id == snapshots.rules_snapshot_id
            )
        )
        return ReferenceDataVersions(
            ontologies=tuple(
                OntologySnapshotUse(
                    ontology_key=key,
                    version_id=snapshot_id,
                    document_version=document_version,
                    source_revision=source_revision,
                )
                for key, snapshot_id, document_version, source_revision in (
                    ontology_rows
                )
            ),
            go_site_revision=go_site_revision,
            entity_catalogs=tuple(
                EntityCatalogUse(source_key=source_key, snapshot_id=snapshot_id)
                for source_key, snapshot_id in sorted(snapshots.entity_catalogs.items())
            ),
        )

    def prune(self, kind: SnapshotKind) -> tuple[UUID, ...]:
        """Delete the data of the kind's snapshots that are no longer kept.

        For each source with an active snapshot, the active snapshot and the
        snapshot it replaced keep their data. Every other
        snapshot of the kind, including every snapshot of a retired source, has
        its data rows deleted and `pruned_at` set. A snapshot another
        transaction has locked is skipped and pruned by a later job.

        Returns:
            The snapshots pruned by this call.
        """
        records = self.session.scalars(
            select(ReferenceSnapshotRecord).where(
                ReferenceSnapshotRecord.kind == kind,
                ReferenceSnapshotRecord.pruned_at.is_(None),
            )
        ).all()
        kept = {
            snapshot_id
            for record in records
            if record.active
            for snapshot_id in (record.snapshot_id, record.replaced_snapshot_id)
            if snapshot_id is not None
        }
        candidates = sorted(
            record.snapshot_id for record in records if record.snapshot_id not in kept
        )
        if not candidates:
            return ()
        locked = self.session.scalars(
            select(ReferenceSnapshotRecord)
            .where(
                ReferenceSnapshotRecord.snapshot_id.in_(candidates),
                ReferenceSnapshotRecord.active.is_(False),
                ReferenceSnapshotRecord.pruned_at.is_(None),
            )
            .order_by(ReferenceSnapshotRecord.snapshot_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).all()
        pruned = [record.snapshot_id for record in locked]
        if not pruned:
            return ()
        # Snapshot data is deleted, not versioned, to bound storage. The snapshot
        # and detail rows, job results, and audit events remain as provenance.
        _delete_data(self.session, kind, pruned)
        now = datetime.now(UTC)
        for record in locked:
            record.pruned_at = now
        self.session.flush(locked)
        return tuple(pruned)

    def retire(
        self, kind: SnapshotKind, source_keys: Collection[str]
    ) -> tuple[ReferenceSnapshotRecord, ...]:
        """Deactivate the active snapshots of sources that are no longer configured.

        Each records `retired_at`. Readers that already locked one keep reading
        it; the kind's next refresh job prunes its data.

        Returns:
            The snapshots retired, in source-key order.
        """
        if not source_keys:
            return ()
        records = self.session.scalars(
            select(ReferenceSnapshotRecord)
            .where(
                ReferenceSnapshotRecord.kind == kind,
                ReferenceSnapshotRecord.source_key.in_(sorted(source_keys)),
                ReferenceSnapshotRecord.active.is_(True),
            )
            .order_by(ReferenceSnapshotRecord.source_key)
        ).all()
        now = datetime.now(UTC)
        for record in records:
            record.active = False
            record.retired_at = now
        self.session.flush(records)
        return tuple(records)

    def _rule_data(self, snapshot_id: UUID, reader_version: int) -> RuleDataIndex:
        """Return a rules snapshot's rule data, indexed for the GO Rules.

        The index is cached by snapshot ID, so each snapshot's data is normally
        decoded once per process; concurrent first reads may each decode it.
        Failures are not cached. A cached snapshot's data is
        not read again; `resolve` has locked the active snapshot, and an active
        snapshot's data is never pruned.

        Raises:
            SnapshotPrunedError: If the snapshot has no rule data.
            ReferenceDataUnavailableError: If an earlier reader version stored
                rule data this release cannot decode; a rules refresh fixes it.
            StoredDataError: If this release's reader stored it and it cannot be
                decoded.
        """
        cached = _RULE_DATA_CACHE.get(snapshot_id)
        if cached is not None:
            return cached
        stored = self.session.scalar(
            select(RuleDataRecord.data).where(RuleDataRecord.snapshot_id == snapshot_id)
        )
        if stored is None:
            raise SnapshotPrunedError((snapshot_id,))
        try:
            data = STORED_RULE_DATA.load(stored)
        except StoredDataError:
            if reader_version < current_reader_version(RefreshKindName.RULES):
                raise ReferenceDataUnavailableError((RULES_REFERENCE_DATA,)) from None
            raise
        index = RuleDataIndex.of(data)
        _RULE_DATA_CACHE.put(snapshot_id, index)
        return index


def _active(
    kind: SnapshotKind, source_key: str
) -> Select[tuple[ReferenceSnapshotRecord]]:
    """Return the query for one source's active snapshot."""
    return select(ReferenceSnapshotRecord).where(
        ReferenceSnapshotRecord.kind == kind,
        ReferenceSnapshotRecord.source_key == source_key,
        ReferenceSnapshotRecord.active.is_(True),
    )


def _delete_data(
    session: Session, kind: SnapshotKind, snapshot_ids: Collection[UUID]
) -> None:
    """Delete the data rows of snapshots of one kind, child rows first."""
    ids = sorted(snapshot_ids)
    match kind:
        case SnapshotKind.ONTOLOGY:
            session.execute(
                delete(OntologyClosureRecord).where(
                    OntologyClosureRecord.snapshot_id.in_(ids)
                )
            )
            session.execute(
                delete(OntologyAltIdRecord).where(
                    OntologyAltIdRecord.snapshot_id.in_(ids)
                )
            )
            session.execute(
                delete(OntologyTermRecord).where(
                    OntologyTermRecord.snapshot_id.in_(ids)
                )
            )
        case SnapshotKind.RULES:
            session.execute(
                delete(RetractedPublicationRecord).where(
                    RetractedPublicationRecord.snapshot_id.in_(ids)
                )
            )
            session.execute(
                delete(RuleDataRecord).where(RuleDataRecord.snapshot_id.in_(ids))
            )
        case SnapshotKind.ENTITY:
            session.execute(
                delete(StoredEntityRecord).where(
                    StoredEntityRecord.snapshot_id.in_(ids)
                )
            )
